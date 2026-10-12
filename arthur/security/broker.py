"""Confirmaciones a distancia (desde el Orb) y elección del canal adecuado.

Regla: se confirma donde estás tú.
  - Petición escrita en la terminal  → se pregunta en la terminal.
  - Petición por el atajo de teclado → se pregunta en el Orb (botones Sí / No).
  - Si no hay ningún Orb abierto     → NO (no hay a quién preguntar).
  - Sin respuesta en `timeout` s      → NO.

El Orb solo puede responder a una pregunta pendiente, identificada por un número
aleatorio imposible de adivinar. No puede iniciar acciones ni enviar texto.
"""

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.events import Event, EventBus
from .confirm import ConfirmationRequest, Confirmer


@dataclass
class Pending:
    id: str
    request: ConfirmationRequest
    done: threading.Event = field(default_factory=threading.Event)
    answer: bool = False


class ConfirmationBroker:
    def __init__(self, bus: EventBus, *, timeout: float = 60, has_clients: Callable[[], bool] = lambda: False) -> None:
        self.bus = bus
        self.timeout = timeout
        self.has_clients = has_clients  # ¿hay algún Orb conectado?
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()

    def ask(self, request: ConfirmationRequest) -> bool | None:
        """Pregunta en el Orb y espera. Devuelve None si no hay ningún Orb a quien preguntar."""
        if not self.has_clients():
            return None
        item = Pending(secrets.token_urlsafe(16), request)
        with self._lock:
            self._pending[item.id] = item
        self.bus.publish(Event("confirmation_pending", self._describe(item)))
        item.done.wait(self.timeout)
        with self._lock:
            # Mirar si está contestada y retirarla es UNA sola operación bajo el candado, igual que en answer():
            # o la respuesta llegó antes (y cuenta, aunque la espera acabara justo entonces: el Orb ya recibió «ok»)
            # o llega después y ya no existe (el Orb recibe «caducada»). Nunca «ok» para el Orb y «No» para ARTHUR.
            answered = item.done.is_set()
            self._pending.pop(item.id, None)
        by = "orb" if answered else "timeout"
        self.bus.publish(Event("confirmation_resolved", {"id": item.id, "answer": item.answer, "by": by}))
        return item.answer if answered else False

    def answer(self, pending_id: str, value: bool) -> bool:
        """Respuesta desde el Orb. Devuelve False si esa pregunta no existe (o ya caducó)."""
        with self._lock:
            # Comprobar que sigue abierta y contestarla, bajo el mismo candado: si no, dos respuestas a la vez
            # (dos peticiones HTTP del Orb) podían pasar la comprobación y la segunda pisaba a la primera.
            item = self._pending.get(pending_id)
            if item is None or item.done.is_set():
                return False
            item.answer = value is True  # cualquier cosa que no sea exactamente True es NO
            item.done.set()
            return True

    def pending(self) -> list[dict]:
        """Preguntas abiertas, para un Orb que se conecta (o recarga) mientras esperan."""
        with self._lock:
            return [self._describe(item) for item in self._pending.values()]

    def _describe(self, item: Pending) -> dict:
        r = item.request
        return {"id": item.id, "tool": r.tool, "risk": str(r.risk), "summary": r.summary, "timeout": self.timeout}


class RoutingConfirmer:
    """Elige dónde preguntar según desde dónde llegó la petición."""

    def __init__(
        self,
        terminal: Confirmer,
        *,
        say: Callable[[str], None],
        notify: Callable[[str], None] | None = None,
        broker: ConfirmationBroker | None = None,
    ) -> None:
        self.terminal = terminal
        self.say = say
        self.notify = notify or (lambda text: None)
        self.broker = broker  # se asigna al abrir el Orb

    def confirm(self, request: ConfirmationRequest) -> bool:
        if threading.current_thread() is threading.main_thread():
            return self.terminal.confirm(request)  # la pediste escribiendo: se pregunta en la terminal
        if self.broker:
            self.notify(f"Necesito tu confirmación en el Orb: {request.summary}")
            self.say(f"  ⚠ Esperando tu confirmación en el Orb: {request.summary}")
            answer = self.broker.ask(request)
            if answer is not None:
                self.say(f"  {'✔ Confirmado' if answer else '✘ Rechazado'} desde el Orb")
                return answer
        self.say(f"  ⚠ «{request.summary}» necesita tu confirmación: abre el Orb (--orb) o pídemelo desde la terminal.")
        return False
