"""Vigila los recordatorios y avisa cuando llega su hora (solo en modo servicio).

Cada segundo mira si hay alguno vencido; lo saca de la base de datos (se avisa una sola vez)
y llama a `on_due`. Si el portátil estaba apagado a esa hora, se avisa al arrancar como «atrasado».
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

LATE = timedelta(minutes=2)  # más tarde que esto (p. ej. ordenador apagado) = aviso «atrasado»


def reminder_message(reminder, now: datetime) -> str:
    if now - reminder.due_at > LATE:
        return f"Recordatorio atrasado, era para las {reminder.due_at:%H:%M}: {reminder.text}."
    return f"Recordatorio: {reminder.text}."


class ReminderScheduler:
    def __init__(self, store, on_due: Callable[[str], None], *, interval: float = 1.0,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.store = store
        self.on_due = on_due
        self.interval = interval
        self.now = now
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="reminders", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> None:
        now = self.now()
        for reminder in self.store.take_due_reminders(now):
            log.info("Recordatorio #%d", reminder.id)
            try:
                self.on_due(reminder_message(reminder, now))
            except Exception:
                log.exception("Fallo avisando de un recordatorio")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception:
                log.exception("Fallo revisando los recordatorios")
