"""Bus de eventos interno.

El núcleo publica eventos (cambios de estado, mensajes, errores) y cualquier
otra parte del sistema puede suscribirse a ellos sin que el núcleo sepa que
existe. Así la interfaz (CLI hoy, Orb mañana) queda desacoplada del cerebro.
"""

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Tipo especial de suscripción: recibe todos los eventos.
ALL = "*"


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, handler: Handler) -> Callable[[], None]:
        """Registra `handler` y devuelve una función para darse de baja."""
        with self._lock:
            self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers[event_type]:
                    self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        # Copiamos la lista para no llamar a nadie con el candado cogido.
        with self._lock:
            handlers = [*self._handlers[event.type], *self._handlers[ALL]]
        for handler in handlers:
            # Un suscriptor roto (p. ej. la interfaz) nunca debe tumbar el núcleo.
            try:
                handler(event)
            except Exception:
                log.exception("Fallo en un suscriptor del evento %r", event.type)
