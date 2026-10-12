"""Herramientas remotas (fase S3): las «manos» de cada aparato, usadas desde el ARTHUR del servidor.

Al conectarse, un aparato (p. ej. el CachyOS) anuncia lo que sabe hacer EN ÉL: mover ventanas, abrir apps,
volumen, música… El servidor las da de alta en su catálogo como herramientas normales; el router hace TODAS sus
comprobaciones (argumentos, política, confirmación en el aparato, auditoría) y después la centralita envía la
llamada al aparato y espera el resultado. Si el aparato se desconecta, sus herramientas siguen apuntadas un rato
(`GRACE_SECONDS`) y, si no vuelve, desaparecen.

Por qué ese rato: la lista de herramientas va dentro de lo que el cerebro tiene precargado. Antes, cada corte del
portátil (reiniciar el servicio, suspenderlo, un salto de Tailscale) quitaba sus herramientas, el servidor volvía a
precargar SIN ellas y, al volver el portátil segundos después, otra vez CON ellas: dos precargas de 18-64 s cada
una (medido el 2026-10-12) para acabar donde estaba. Ahora, si vuelve con las mismas, no se toca nada.

Seguridad:
  - Nada PELIGROSO por esta vía (órdenes, instalar…): se rechaza al darlo de alta.
  - El aparato vuelve a validar con sus propias reglas antes de ejecutar (doble control): un servidor comprometido
    no puede hacer en el aparato nada que el aparato no permita por sí mismo.
  - Un aparato no puede pisar una herramienta del servidor (p. ej. «remember»). Si dos aparatos saben lo mismo,
    se usa el que te habló por última vez.
  - Un resultado solo se acepta del aparato al que se le pidió, y con el número de la llamada.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ..tools.base import Param, Risk, Tool, ToolDenied, ToolError
from ..tools.registry import NAME_PATTERN, ToolRegistry

log = logging.getLogger(__name__)

MAX_TOOLS_PER_DEVICE = 40
MAX_DESCRIPTION = 400
MAX_PARAMS = 8
MAX_OUTPUT = 8_000
CALL_TIMEOUT = 60
GRACE_SECONDS = 600  # un aparato desconectado conserva sus herramientas 10 min por si vuelve
TYPES = {"string": str, "integer": int, "boolean": bool, "array": list}
REMOTE_RISKS = {Risk.READ, Risk.LOW, Risk.WRITE, Risk.PRIVATE}  # PELIGROSO nunca por la red
RISK_ORDER = [Risk.READ, Risk.LOW, Risk.WRITE, Risk.PRIVATE, Risk.DANGEROUS]  # de menos a más


class SpecError(ValueError):
    pass


@dataclass
class _Call:
    device: str
    done: threading.Event = field(default_factory=threading.Event)
    status: str = "error"
    output: str = ""


def _params(schema: object) -> tuple[Param, ...]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise SpecError("input_schema debe ser un objeto JSON Schema")
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    if not isinstance(props, dict) or not isinstance(required, list) or len(props) > MAX_PARAMS:
        raise SpecError("parámetros no válidos")
    params = []
    for name, spec in props.items():
        if not isinstance(name, str) or not NAME_PATTERN.match(name) or not isinstance(spec, dict):
            raise SpecError(f"parámetro no válido: {name!r}")
        kind = TYPES.get(spec.get("type"))
        if kind is None:
            raise SpecError(f"tipo no admitido en {name}")
        description = spec.get("description", "")
        params.append(Param(name, kind, description if isinstance(description, str) else "",
                            required=name in required, max_length=500 if kind in (str, list) else None))
    return tuple(params)


def build_remote_tool(spec: object, run: Callable[[dict], str]) -> Tool:
    """De lo que anuncia el aparato a una Tool del servidor (validando todo: viene de fuera)."""
    if not isinstance(spec, dict):
        raise SpecError("cada herramienta debe ser un objeto")
    name, description = spec.get("name"), spec.get("description")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise SpecError(f"nombre no válido: {name!r}")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        raise SpecError(f"{name}: descripción vacía o demasiado larga")
    try:
        risk = Risk(spec.get("risk"))
    except ValueError:
        raise SpecError(f"{name}: riesgo desconocido") from None
    if risk not in REMOTE_RISKS:
        raise SpecError(f"{name}: lo peligroso no se puede usar a distancia")
    params = _params(spec.get("input_schema"))
    risk_for = None
    low_if = spec.get("low_if")
    if low_if is not None:
        # «De confianza»: p. ej. abrir Firefox no pide confirmación, abrir otra app sí (como en el portátil).
        param, values = (low_if.get("param"), low_if.get("values")) if isinstance(low_if, dict) else (None, None)
        if (param not in {p.name for p in params} or not isinstance(values, list) or len(values) > 50
                or not all(isinstance(v, str) for v in values)):
            raise SpecError(f"{name}: low_if no válido")
        trusted = frozenset(values)
        risk_for = lambda a, _p=param, _r=risk: Risk.LOW if a.get(_p) in trusted else _r  # noqa: E731
    lower_if = spec.get("lower_if")
    if lower_if is not None:
        # El aparato anuncia el riesgo MÁS alto de la herramienta y en qué caso es menor: p. ej. «quién me ha
        # escrito» es de solo mirar, pero con el texto de los mensajes es PRIVADO (pide confirmación). Sin esto,
        # el riesgo que depende de los argumentos se perdía por el camino. Solo puede BAJAR, nunca subir.
        param, values = (lower_if.get("param"), lower_if.get("values")) if isinstance(lower_if, dict) else (None, None)
        try:
            lower = Risk(lower_if.get("risk")) if isinstance(lower_if, dict) else None
        except ValueError:
            lower = None
        if (param not in {p.name for p in params} or not isinstance(values, list) or not 0 < len(values) <= 50
                or not all(isinstance(v, (str, bool, int)) for v in values) or lower is None
                or RISK_ORDER.index(lower) >= RISK_ORDER.index(risk)):
            raise SpecError(f"{name}: lower_if no válido")
        when = tuple(values)
        missing = lower_if.get("missing") is True  # sin ese argumento vale lo que el aparato tiene por defecto
        before = risk_for or (lambda a, _r=risk: _r)

        def risk_for(a, _p=param, _w=when, _l=lower, _b=before, _m=missing):
            if a.get(_p) is None:  # no lo ha puesto (validate deja None en los opcionales sin valor)
                return _l if _m else _b(a)
            return _l if any(a[_p] == v and type(a[_p]) is type(v) for v in _w) else _b(a)
    return Tool(name=name, description=description.strip(), risk=risk, params=params, run=run, risk_for=risk_for)


class RemoteToolHub:
    def __init__(self, registry: ToolRegistry, *, timeout: float = CALL_TIMEOUT, grace: float = GRACE_SECONDS,
                 timer: Callable[[float, Callable[[], None]], object] | None = None) -> None:
        """`timer` solo se cambia en los tests: crea y arranca la cuenta atrás de un aparato que se ha ido."""
        self.registry = registry
        self.timeout = timeout
        self.grace = grace
        self._timer = timer or self._start_timer
        self._specs: dict[str, str] = {}          # aparato → lo que anunció (para saber si vuelve con lo mismo)
        self._offline: dict[str, object] = {}     # aparato desconectado → su cuenta atrás
        self.send: Callable[[str, dict], bool] = lambda device, payload: False  # lo conecta la API
        # Cambió la lista de herramientas (un aparato llega o se va): el cerebro vuelve a precargar sus
        # instrucciones, porque la lista va dentro y lo que tenía precargado ya no le sirve (run_core).
        self.on_change: Callable[[], None] = lambda: None
        self._owners: dict[str, list[str]] = {}   # herramienta → aparatos que la tienen (el último, al final)
        self._remote: set[str] = set()            # nombres que son remotos (para no tocar los del servidor)
        self._calls: dict[str, _Call] = {}
        self._active: str | None = None
        self._lock = threading.Lock()

    # --- alta y baja ---
    def register(self, device: str, specs: object) -> tuple[list[str], list[str]]:
        """Da de alta las herramientas del aparato. Devuelve (dadas de alta, rechazadas con el motivo)."""
        if not isinstance(specs, list) or len(specs) > MAX_TOOLS_PER_DEVICE:
            raise SpecError(f"se espera una lista de hasta {MAX_TOOLS_PER_DEVICE} herramientas")
        signature = json.dumps(specs, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock:
            same = self._specs.get(device) == signature
            if same:
                self._offline.pop(device, None)  # su cuenta atrás ya no hará nada (ver _expire)
                names = [name for name, owners in self._owners.items() if device in owners]
        if same:
            # Vuelve con las MISMAS herramientas: la lista del cerebro no cambia y lo precargado sigue valiendo.
            log.info("remote-tools: «%s» vuelve con las mismas %d herramientas", device, len(names))
            return names, []
        self.unregister_device(device, notify=False)  # un aparato que se reconecta sustituye lo que tenía
        added, rejected = [], []
        for spec in specs:
            try:
                tool = build_remote_tool(spec, run=lambda a, n=spec.get("name"): self.call(n, a))
            except SpecError as e:
                rejected.append(str(e))
                continue
            with self._lock:
                if tool.name not in self._remote and self.registry.get(tool.name) is not None:
                    rejected.append(f"{tool.name}: ya es una herramienta del servidor")
                    continue
                owners = self._owners.setdefault(tool.name, [])
                if not owners:
                    self.registry.register(tool)
                    self._remote.add(tool.name)
                owners.append(device)
            added.append(tool.name)
        with self._lock:
            self._specs[device] = signature
        log.info("remote-tools: «%s» aporta %d herramientas (%d rechazadas)", device, len(added), len(rejected))
        self.on_change()
        return added, rejected

    def disconnect(self, device: str) -> None:
        """El aparato ha cortado la conexión. Sus herramientas siguen `grace` s por si vuelve (mientras, llamarlas
        da «no está conectado»); si no vuelve, se quitan y el cerebro precarga la lista nueva."""
        if self.grace <= 0:
            self.unregister_device(device)
            return
        with self._lock:
            if device not in self._specs:
                return  # no había anunciado nada
            token = object()
            self._offline[device] = token

        def expire() -> None:
            with self._lock:
                if self._offline.get(device) is not token:
                    return  # volvió (o se fue y volvió otra vez): esta cuenta atrás ya no vale
            log.info("remote-tools: «%s» no ha vuelto en %.0f s: quito sus herramientas", device, self.grace)
            self.unregister_device(device)

        self._timer(self.grace, expire)

    @staticmethod
    def _start_timer(seconds: float, action: Callable[[], None]) -> object:
        timer = threading.Timer(seconds, action)
        timer.daemon = True
        timer.start()
        return timer

    def unregister_device(self, device: str, *, notify: bool = True) -> None:
        removed = False
        with self._lock:
            self._specs.pop(device, None)
            self._offline.pop(device, None)
            for name, owners in list(self._owners.items()):
                if device in owners:
                    owners.remove(device)
                if not owners:
                    del self._owners[name]
                    self._remote.discard(name)
                    self.registry.unregister(name)
                    removed = True
            for call in self._calls.values():
                if call.device == device and not call.done.is_set():
                    call.status, call.output = "error", "El aparato se ha desconectado."
                    call.done.set()
        if removed and notify:
            self.on_change()

    def set_active(self, device: str) -> None:
        """El aparato que te habló por última vez: si dos saben lo mismo, se usa este."""
        with self._lock:
            self._active = device

    def devices_for(self, name: str) -> list[str]:
        with self._lock:
            return list(self._owners.get(name, []))

    # --- llamadas ---
    def call(self, name: str, arguments: dict) -> str:
        with self._lock:
            all_owners = self._owners.get(name) or []
            owners = [d for d in all_owners if d not in self._offline]
            if not owners:
                if all_owners:
                    raise ToolError(f"«{all_owners[-1]}» no está conectado ahora mismo.")
                raise ToolError("Ese aparato ya no está conectado.")
            device = self._active if self._active in owners else owners[-1]
            call_id = secrets.token_urlsafe(12)
            call = self._calls[call_id] = _Call(device)
        try:
            if not self.send(device, {"type": "tool_call", "data": {"id": call_id, "name": name,
                                                                    "arguments": arguments}}):
                raise ToolError(f"No puedo hablar con «{device}» ahora mismo.")
            if not call.done.wait(self.timeout):
                raise ToolError(f"«{device}» no ha respondido a tiempo.")
        finally:
            with self._lock:
                self._calls.pop(call_id, None)
        if call.status == "denied":
            raise ToolDenied(call.output or f"«{device}» no lo permite.")
        if call.status != "ok":
            raise ToolError(call.output or f"Ha fallado en «{device}».")
        return call.output

    def resolve(self, device: str, call_id: object, status: object, output: object) -> bool:
        """El resultado que manda un aparato. Solo vale el del aparato al que se le pidió."""
        if not isinstance(call_id, str) or status not in ("ok", "denied", "error") or not isinstance(output, str):
            return False
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or call.device != device or call.done.is_set():
                return False
            call.status, call.output = status, output[:MAX_OUTPUT]
            call.done.set()
            return True
