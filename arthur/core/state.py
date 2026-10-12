"""Estados de ARTHUR y la máquina de estados que los controla.

Solo se permiten ciertas transiciones (por ejemplo, no se puede pasar de
ERROR a SPEAKING sin volver antes a IDLE). Cada cambio se publica en el bus
como evento `state_changed`, que es lo que más adelante animará el Orb.
"""

import enum
import threading

from .events import Event, EventBus


class State(enum.StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    ERROR = "error"


TRANSITIONS: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.LISTENING, State.THINKING, State.ERROR}),
    State.LISTENING: frozenset({State.THINKING, State.IDLE, State.ERROR}),
    State.THINKING: frozenset({State.EXECUTING, State.SPEAKING, State.IDLE, State.ERROR}),
    State.EXECUTING: frozenset({State.THINKING, State.SPEAKING, State.ERROR}),
    State.SPEAKING: frozenset({State.IDLE, State.LISTENING, State.ERROR}),
    State.ERROR: frozenset({State.IDLE}),
}


class InvalidTransition(Exception):
    pass


class StateMachine:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._state = State.IDLE
        self._lock = threading.Lock()

    @property
    def state(self) -> State:
        return self._state

    def transition(self, new: State) -> None:
        with self._lock:
            old = self._state
            if new == old:
                return
            if new not in TRANSITIONS[old]:
                raise InvalidTransition(f"{old} → {new} no está permitido")
            self._state = new
        self._bus.publish(Event("state_changed", {"from": old, "to": new}))
