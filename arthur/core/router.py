"""Tool Router: el único camino para ejecutar una herramienta.

El cerebro (reglas o LLM) solo PROPONE llamadas. El router decide, siempre
con los mismos pasos y en este orden:

  1. ¿Existe la herramienta?             no → error
  2. ¿Los argumentos son válidos?        no → error
  3. ¿Pasa la comprobación propia?       no → denegado   (rutas, comandos…)
  4. ¿Qué dice la política?              DENY → denegado
  5. Si hace falta, ¿lo confirmas tú?    no → cancelado
  6. Ejecutar, capturando cualquier fallo.
  7. Registrar en auditoría y publicar el evento.
"""

import logging
import time

from ..security.audit import AuditLog
from ..security.confirm import ConfirmationRequest, Confirmer, visible_text
from ..security.policy import Decision, Policy
from ..tools.base import Tool, ToolArgumentError, ToolCall, ToolDenied, ToolError, ToolResult
from ..tools.registry import ToolRegistry
from .events import Event, EventBus

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 8_000


class ToolRouter:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: Policy,
        confirmer: Confirmer,
        audit: AuditLog,
        bus: EventBus,
    ) -> None:
        self.registry = registry
        self._policy = policy
        self._confirmer = confirmer
        self._audit = audit
        self._bus = bus

    def execute(self, call: ToolCall) -> ToolResult:
        start = time.monotonic()

        risk = None
        summary = None  # lo que se te enseñó al pedir confirmación (queda en la auditoría)

        def finish(status, output, decision, tool: Tool | None = None, args=None) -> ToolResult:
            if len(output) > MAX_RESULT_CHARS:
                output = output[:MAX_RESULT_CHARS] + "\n… (resultado recortado)"
            result = ToolResult(call.id, call.name, status, output)
            self._audit.record(
                call_id=call.id,
                tool=call.name,
                risk=risk or (tool.risk if tool else None),
                arguments=args if args is not None else dict(call.arguments),
                **({"summary": summary} if summary else {}),
                decision=decision,
                status=status,
                detail=output if status != "ok" else None,
                duration_ms=round((time.monotonic() - start) * 1000),
            )
            self._bus.publish(Event("tool_finished", {"call_id": call.id, "tool": call.name, "status": status}))
            log.info("Herramienta %s → %s (%s)", call.name, status, decision)
            return result

        tool = self.registry.get(call.name)
        if tool is None:
            return finish("error", f"La herramienta '{call.name}' no existe", "unknown")

        try:
            args = tool.validate(call.arguments)
        except ToolArgumentError as e:
            return finish("error", f"Argumentos no válidos: {e}", "invalid", tool)

        try:
            if tool.check:
                tool.check(args)
            risk = tool.effective_risk(args)
        except ToolDenied as e:
            return finish("denied", str(e), "deny", tool, args)
        except ToolError as e:
            return finish("error", str(e), "invalid", tool, args)
        except Exception as e:
            # Un fallo inesperado de la comprobación (p. ej. un ValueError por un enlace raro que escribió el
            # modelo) no puede tumbar la petición ni quedar fuera de la auditoría: la herramienta no se ejecuta.
            log.exception("Fallo comprobando %s", tool.name)
            return finish("error", f"Fallo al comprobar la petición: {e}", "invalid", tool, args)

        verdict = self._policy.decide(risk)
        if verdict.decision is Decision.DENY:
            return finish("denied", verdict.reason, "deny", tool, args)
        if verdict.decision is Decision.CONFIRM:
            try:
                request = ConfirmationRequest(tool.name, risk, visible_text(tool.summary(args)))
                summary = request.summary
                self._bus.publish(Event("confirmation_requested", {"tool": tool.name, "summary": request.summary}))
                confirmed = self._confirmer.confirm(request)
            except Exception as e:
                # Si no se sabe describir la acción o no se puede preguntar, es como un NO: nunca se ejecuta
                # algo que el usuario no ha podido ver y aceptar.
                log.exception("Fallo pidiendo la confirmación de %s", tool.name)
                return finish("error", f"No he podido pedirte la confirmación ({e}): no se ha hecho nada",
                              "rejected", tool, args)
            if not confirmed:
                return finish("cancelled", "El usuario ha respondido que NO: no quiere que se haga. No lo vuelvas a intentar", "rejected", tool, args)

        decision = "confirmed" if verdict.decision is Decision.CONFIRM else "allow"
        self._bus.publish(Event("tool_started", {"call_id": call.id, "tool": tool.name}))
        try:
            output = tool.run(args)
        except ToolDenied as e:
            return finish("denied", str(e), "deny", tool, args)
        except ToolError as e:
            return finish("error", str(e), decision, tool, args)
        except Exception as e:
            log.exception("Fallo ejecutando %s", tool.name)
            return finish("error", f"Fallo al ejecutar: {e}", decision, tool, args)
        return finish("ok", output, decision, tool, args)
