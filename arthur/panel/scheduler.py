"""Relojes del panel: el del resumen de la mañana (`BriefingClock`) y el de la revisión (`RevisionClock`)."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Callable

from .settings import Settings
from .store import PanelStore, _madrid_now

CATCH_UP_LIMIT = time(13, 0)   # pasada esta hora ya no tiene sentido un «buenos días»
log = logging.getLogger(__name__)
MAX_WAIT = 60.0                # esperas cortas para notar un cambio de hora del reloj


def _at(settings: Settings, day: date) -> datetime:
    # combine con tzinfo: la hora de pared es la de Madrid aunque ese día cambie el horario
    return datetime.combine(day, settings.times[day.weekday()], tzinfo=settings.zone)


def next_run(settings: Settings, after: datetime) -> datetime:
    """Primer momento del resumen estrictamente posterior a `after`."""
    local = after.astimezone(settings.zone)
    for offset in range(8):
        candidate = _at(settings, local.date() + timedelta(days=offset))
        if candidate > after:
            return candidate
    raise AssertionError("inalcanzable: siempre hay una hora en los próximos 8 días")


def should_catch_up(settings: Settings, now: datetime, sent_today: bool) -> bool:
    local = now.astimezone(settings.zone)
    return (not sent_today and local >= _at(settings, local.date())
            and local.timetz().replace(tzinfo=None) < CATCH_UP_LIMIT)


class BriefingClock:
    def __init__(self, settings: Settings, store: PanelStore, run: Callable[[], None], *,
                 now: Callable[[], datetime] = _madrid_now,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._settings, self._store, self._run, self._now = settings, store, run, now
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._thread: threading.Thread | None = None
        self._attempted: date | None = None   # día en que ya se intentó: sin reintentos cada minuto
        self._due: datetime | None = None

    def tick(self) -> float:
        """Una vuelta: avisa si toca y devuelve los segundos a esperar."""
        now = self._now().astimezone(self._settings.zone)
        today = now.date()
        if self._due is None:   # primera vuelta (arranque): ¿hay que recuperar el aviso de hoy?
            self._due = next_run(self._settings, now)
            sent = self._attempted == today or self._store.briefing_sent(today)
            if should_catch_up(self._settings, now, sent):
                self._attempt(today)
        elif now >= self._due:
            self._due = next_run(self._settings, now)
            # Si el equipo estuvo dormido y despierta tarde, no se avisa pasadas las 13:00 ni dos veces
            sent = self._attempted == today or self._store.briefing_sent(today)
            if not sent and now.timetz().replace(tzinfo=None) < CATCH_UP_LIMIT:
                self._attempt(today)
        return max(1.0, min(MAX_WAIT, (self._due - now).total_seconds()))

    def _attempt(self, day: date) -> None:
        # Se anota antes de llamar: si falla, solo reintenta un reinicio del servicio (Ruling 5)
        self._attempted = day
        try:
            self._run()
        except Exception:   # el hilo no debe morir por un fallo del resumen
            log.exception("Panel: el resumen de la mañana falló")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.tick()
            except Exception:   # un fallo puntual (sqlite, reloj) no debe matar el hilo en silencio
                log.exception("Panel: fallo en el reloj del resumen")
                wait = MAX_WAIT
            self._sleep(wait)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="panel-briefing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class RevisionClock:
    """Reloj de la revisión (pieza 4). En cada vuelta (como mucho cada minuto):

    - cierra los días anteriores que sigan abiertos (cuentas finales tras la medianoche, o al arrancar);
    - desde la hora de la foto (08:00), saca la foto del plan de hoy si falta;
    - desde la hora de la revisión (21:30) y hasta medianoche, hace la de hoy si no está guardada: la semanal los
      domingos, la nocturna los demás días. Si el servidor arranca más tarde, se recupera ese mismo día.

    Cada cosa se intenta una vez por día: si falla, se apunta en el registro y no se reintenta cada minuto.
    """

    def __init__(self, revision, *, revision_time: time, photo_time: time, now: Callable[[], datetime] = _madrid_now,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._rev, self._at, self._photo_at, self._now = revision, revision_time, photo_time, now
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._thread: threading.Thread | None = None
        self._tried: dict[str, date] = {}

    def _once(self, what: str, today: date, fn: Callable[[], object]) -> None:
        if self._tried.get(what) == today:
            return
        self._tried[what] = today   # antes de llamar: un fallo no se repite cada minuto
        try:
            fn()
        except Exception:  # noqa: BLE001 - el hilo no debe morir por un fallo de la revisión
            log.exception("Panel: la revisión (%s) falló", what)

    def tick(self) -> float:
        now = self._now()
        today, clock = now.date(), now.timetz().replace(tzinfo=None)
        try:
            self._rev.close_days()
        except Exception:  # noqa: BLE001
            log.exception("Panel: no se han podido cerrar los días anteriores")
        if clock >= self._photo_at:
            self._once("foto", today, self._rev.ensure_photo)
        if clock >= self._at:
            kind = "semana" if today.weekday() == 6 else "noche"
            if not self._rev.done(today, kind):
                self._once(kind, today, lambda: self._rev.run(kind))
        return MAX_WAIT

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.tick()
            except Exception:  # noqa: BLE001
                log.exception("Panel: fallo en el reloj de la revisión")
                wait = MAX_WAIT
            self._sleep(wait)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="panel-revision", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
