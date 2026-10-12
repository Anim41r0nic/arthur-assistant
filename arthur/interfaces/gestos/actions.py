"""GestureActions: qué hace cada gesto. Una tabla FIJA en el código (docs/gestos.md).

Regla de partida (decisión de diseño): un gesto NUNCA hace algo peligroso. La cámara ve cosas que no son órdenes
(un saludo en una videollamada, un vídeo con un pulgar arriba…), así que cada gesto hace algo pequeño y reversible:

    palma ✋            → «calla»: corta la voz de ARTHUR y pausa la música (por el router: queda en la auditoría)
    pulgar_abajo 👎, puno ✊ → «No» a las confirmaciones abiertas en el Orb (sin pregunta abierta, nada)
    pulgar_arriba 👍, circulo 👌 → «Sí», CON LÍMITES: solo si hay UNA confirmación abierta y es de riesgo bajo o de
                         escritura (crear un evento, guardar un contacto). Nunca lo PRIVADO ni lo PELIGROSO: ahí
                         sigue el clic. El vigilante además pide mantenerlo más (`EXTRA_HOLD`)
    indice_arriba ☝️    → «repite»: vuelve a decir la última respuesta de ARTHUR (la que hay en memoria; no piensa).
                         Si ARTHUR está ocupado (aún hablando, escuchando…), la repite en cuanto quede libre
    dos_dedos ✌️        → pausa / play de la música (por el router)
    saludo 👋           → ARTHUR te saluda en voz alta (un texto fijo según la hora; no piensa)

El barrido (`mover_lateral` → ocultar el Orb) está fuera de la tabla desde la 0.38.1: la cámara no ve una mano que
cruza deprisa. La acción `oculta_orb` sigue aquí, como `orb`, por si se le asigna otro gesto fiable.

🤙 (traer el Orb) está fuera de la tabla desde la 0.37.2: con la mano de una persona real se confunde con un 👍 de lado. La acción
`orb` sigue aquí por si se le asigna otro gesto fiable.

Los gestos NUNCA pasan por el modelo de lenguaje: nada de lo que ve la cámara se convierte en texto ni llega al
prompt; este módulo no conoce al cerebro. Cada gesto queda en `audit.jsonl` con lo que se hizo.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from ...core.events import Event, EventBus
from ...core.router import ToolRouter
from ...security.audit import AuditLog
from ...tools.base import Risk, ToolCall
from .names import Gesture, from_wire

log = logging.getLogger(__name__)

# La tabla: gesto → nombre de la acción. Cualquier otro nombre se ignora (y se apunta que se ignoró).
ACTIONS = {
    Gesture.PALM: "calla",
    Gesture.THUMB_DOWN: "no",
    Gesture.FIST: "no",
    Gesture.THUMB_UP: "si",
    Gesture.OK: "si",
    Gesture.INDEX_UP: "repite",
    Gesture.TWO_FINGERS: "pausa_play",
    Gesture.WAVE: "hola",
}
# Lo único que se puede aceptar con un gesto (docs/gestos.md). Lo privado y lo peligroso, solo con el clic.
YES_RISKS = frozenset({str(Risk.LOW), str(Risk.WRITE)})
# «Repite» con ARTHUR ocupado: se espera a que quede libre como mucho esto (s), mirando cada REPEAT_POLL s.
# Medido en una prueba real (0.37.3): hizo ☝️ mientras ARTHUR aún le leía el calendario, y antes no pasaba nada.
REPEAT_WAIT = 30.0
REPEAT_POLL = 0.5


class GestureActions:
    def __init__(
        self,
        bus: EventBus,
        *,
        router: ToolRouter,
        audit: AuditLog,
        stop_speaking: Callable[[], None],
        broker: Callable[[], object | None] = lambda: None,
        repeat: Callable[[str], bool] | None = None,
        show_orb: Callable[[], None] | None = None,
        hide_orb: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`stop_speaking`: corta la voz al momento. `broker`: el ConfirmationBroker del Orb (None si no hay Orb).
        `repeat`: dice un texto en voz alta si ARTHUR está libre (assistant.announce). `show_orb`: trae el Orb."""
        self.bus = bus
        self.router = router
        self.audit = audit
        self.stop_speaking = stop_speaking
        self.broker = broker
        self.repeat = repeat
        self.show_orb = show_orb
        self.hide_orb = hide_orb
        self.clock = clock
        self.sleep = sleep
        self._repeat_waiting = threading.Lock()  # una sola repetición pendiente: varios ☝️ seguidos no se acumulan
        self._last_reply: str | None = None  # solo en memoria: lo último que dijo ARTHUR, para «repite»
        self._run = {"calla": self._quiet, "no": self._reject, "si": self._accept, "repite": self._repeat,
                     "pausa_play": self._toggle_music, "orb": self._orb, "hola": self._hello,
                     "oculta_orb": self._hide_orb}
        bus.subscribe("gesture", self._on_gesture)
        bus.subscribe("assistant_message", self._remember_reply)

    def _remember_reply(self, event: Event) -> None:
        text = event.data.get("text")
        if isinstance(text, str) and text.strip():
            self._last_reply = text

    def _on_gesture(self, event: Event) -> None:
        gesture = from_wire(event.data.get("name"))
        action = ACTIONS.get(gesture) if gesture is not None else None
        if action is None:
            log.warning("gestures: gesto desconocido ignorado")
            return
        try:
            status, detail = "ok", self._run[action]()
        except Exception as e:
            log.exception("gestures: fallo haciendo «%s»", action)
            status, detail = "error", f"Fallo: {e}"
        log.info("gestures: %s → %s (%s)", gesture.value, action, status)
        self.audit.record(tool="gesture", risk="low", arguments={"gesture": gesture.value, "action": action},
                          decision="gesture", status=status, detail=detail)
        # Al Orb: qué gesto ha visto y qué ha hecho (o por qué no). Sin esto, un 👋 con ARTHUR ocupado parecía
        # un gesto no detectado (prueba real, 0.54.0). `detail` son los textos fijos de arriba, no datos tuyos.
        self.bus.publish(Event("gesture_action", {"gesture": gesture.value, "action": action, "status": status,
                                                  "detail": detail}))

    def _quiet(self) -> str:
        self.stop_speaking()
        # La música va por el router, como si se lo hubieras pedido: misma validación y su propia línea en
        # audit.jsonl. Sin reproductor abierto, el router lo apunta como error y no pasa nada más.
        result = self.router.execute(ToolCall("media", {"action": "pause"}))
        return f"Voz cortada; música: {result.status}"

    def _pending(self) -> tuple[object | None, list[dict]]:
        broker = self.broker()
        return broker, (broker.pending() if broker is not None else [])

    def _reject(self) -> str:
        broker, pending = self._pending()
        if not pending:
            return "Sin confirmación abierta: no se ha hecho nada"
        # «No» a todas las abiertas: cancelar siempre es seguro (lo peor que pasa es que no se haga algo).
        rejected = sum(1 for item in pending if broker.answer(item["id"], False))
        return f"Confirmación rechazada ({rejected})"

    def _accept(self) -> str:
        broker, pending = self._pending()
        if not pending:
            return "Sin confirmación abierta: no se ha hecho nada"
        if len(pending) > 1:
            return "Hay varias confirmaciones abiertas: el «sí» con gesto no elige, usa el Orb"
        item = pending[0]
        if item.get("risk") not in YES_RISKS:
            return f"Riesgo {item.get('risk')}: el «sí» con gesto no vale, usa el clic del Orb"
        return "Confirmación aceptada" if broker.answer(item["id"], True) else "La confirmación ya no estaba abierta"

    def _repeat(self) -> str:
        if self.repeat is None or self._last_reply is None:
            return "Nada que repetir"
        text = self._last_reply
        if self.repeat(text):
            return "Repetido"
        if not self._repeat_waiting.acquire(blocking=False):
            return "Ya hay una repetición esperando"
        threading.Thread(target=self._repeat_when_free, args=(text,), name="gestures-repeat", daemon=True).start()
        return "ARTHUR está ocupado: lo repetirá al quedar libre"

    def _repeat_when_free(self, text: str) -> None:
        try:
            for _ in range(int(REPEAT_WAIT / REPEAT_POLL)):
                self.sleep(REPEAT_POLL)
                if self.repeat(text):
                    log.info("gestures: repetido al quedar libre")
                    return
            log.info("gestures: no se ha repetido (ARTHUR siguió ocupado %d s)", REPEAT_WAIT)
        except Exception:
            log.exception("gestures: fallo repitiendo")
        finally:
            self._repeat_waiting.release()

    def _toggle_music(self) -> str:
        result = self.router.execute(ToolCall("media", {"action": "toggle"}))
        return f"Música: {result.status}"

    def _orb(self) -> str:
        if self.show_orb is None:
            return "Sin Orb: no se ha hecho nada"
        self.show_orb()
        return "Orb a la vista"

    def _hello(self) -> str:
        if self.repeat is None:
            return "Sin voz: no se ha hecho nada"
        hour = self.clock().hour
        greeting = "Buenos días" if 6 <= hour < 14 else "Buenas tardes" if 14 <= hour < 21 else "Buenas noches"
        # announce: si ARTHUR está ocupado no interrumpe (un saludo no merece esperar como «repite»)
        return "Saludado" if self.repeat(f"¡{greeting}! Aquí estoy.") else "ARTHUR está ocupado: no saluda"

    def _hide_orb(self) -> str:
        if self.hide_orb is None:
            return "Sin Orb: no se ha hecho nada"
        self.hide_orb()
        return "Orb ocultado"
