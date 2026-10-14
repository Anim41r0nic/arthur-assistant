"""Cola de trabajos de Creator Studio: hace las tareas largas por pasos y sobrevive a los cortes.

Un trabajo (p. ej. «producir el vídeo 7») es una lista fija de pasos: investigar, guion, voz, montaje…
Cada paso guarda su resultado en studio.sqlite3 al terminar. Si el servidor se reinicia a mitad, al
arrancar se retoma desde el primer paso sin terminar: lo hecho no se repite (ni se vuelve a gastar).

- Un trabajo a la vez (decidido con Marc: el servidor tiene poca RAM libre con Ollama cargado).
- Fallo pasajero (red, Ollama ocupado…): se reintenta más tarde, esperando cada vez el doble.
- Fallo definitivo (`StepFailed`: «la fuente no existe», «falta la licencia de este recurso»…): no se
  reintenta, porque repetirlo daría lo mismo.
- Cancelar: si está en cola, al momento; si está en marcha, al acabar el paso actual (cortar un ffmpeg a
  medias dejaría ficheros rotos).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .store import Job, StudioStore, StudioStoreError

log = logging.getLogger(__name__)


class StepFailed(Exception):
    """Error definitivo de un paso: el trabajo falla sin reintentar. Su mensaje se le enseña a Marc."""


@dataclass
class StepContext:
    job: Job
    outputs: dict[str, dict[str, Any]]  # lo que devolvieron los pasos anteriores, por nombre
    cost_eur: float | None = None       # el paso lo rellena si sabe lo que ha costado (None = no se sabe)
    _cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)

    def cancelled(self) -> bool:
        """Para pasos largos que pueden pararse limpiamente entre trozos."""
        return self._cancelled()


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[StepContext], dict[str, Any]]


class JobRunner:
    def __init__(self, store: StudioStore, kinds: Mapping[str, tuple[Step, ...]], *,
                 on_change: Callable[[Job], None] | None = None, max_attempts: int = 3,
                 backoff: timedelta = timedelta(seconds=30), max_backoff: timedelta = timedelta(minutes=15),
                 poll: float = 1.0, now: Callable[[], datetime] = datetime.now,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        for kind, steps in kinds.items():
            names = [s.name for s in steps]
            if not steps or len(set(names)) != len(names):
                raise ValueError(f"El trabajo {kind!r} necesita pasos con nombres distintos")
        self.store = store
        self.kinds = dict(kinds)
        self.on_change = on_change
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.poll = poll
        self.now = now
        self.monotonic = monotonic
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lo que usan la web y las herramientas ------------------------------

    def submit(self, kind: str, params: dict[str, Any]) -> Job:
        if kind not in self.kinds:
            raise StudioStoreError(f"Tipo de trabajo desconocido: {kind!r}")
        job = self.store.create_job(kind, params, now=self.now())
        log.info("Studio: trabajo #%d (%s) en cola", job.id, kind)
        self._notify(job)
        self._wake.set()
        return job

    def cancel(self, job_id: int) -> bool:
        ok = self.store.request_cancel(job_id, now=self.now())
        if ok:
            log.info("Studio: cancelación pedida para el trabajo #%d", job_id)
            self._notify_id(job_id)
        return ok

    # --- el hilo de trabajo -------------------------------------------------

    def start(self) -> None:
        recovered = self.store.recover_interrupted(now=self.now())
        if recovered:
            log.warning("Studio: retomando trabajos interrumpidos: %s", ", ".join(f"#{i}" for i in recovered))
        self._thread = threading.Thread(target=self._loop, name="studio-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:
                log.exception("Studio: fallo inesperado en la cola")
                busy = False
            if not busy:
                self._wake.wait(self.poll)
                self._wake.clear()

    def run_once(self) -> bool:
        """Hace UN trabajo pendiente (todos sus pasos que falten). False si no había ninguno."""
        job = self.store.claim_next(self.now())
        if job is None:
            return False
        self._notify(job)
        try:
            self._run(job)
        finally:
            self._notify_id(job.id)
        return True

    def _run(self, job: Job) -> None:
        steps = self.kinds.get(job.kind)
        if steps is None:
            # P. ej. un trabajo de una versión más nueva que se quitó: no se puede hacer.
            self.store.finish_job(job.id, "failed", f"Tipo de trabajo desconocido: {job.kind}", now=self.now())
            return
        outputs = {s.name: s.output or {} for s in self.store.steps(job.id) if s.status == "done"}
        for step in steps:
            if step.name in outputs:
                continue  # ya hecho en un intento anterior (o antes de un corte)
            if self.store.cancel_requested(job.id):
                self.store.finish_job(job.id, "cancelled", now=self.now())
                log.info("Studio: trabajo #%d cancelado antes de «%s»", job.id, step.name)
                return
            ctx = StepContext(job, dict(outputs), _cancelled=lambda: self.store.cancel_requested(job.id))
            self.store.start_step(job.id, step.name, now=self.now())
            self._notify_id(job.id)
            started = self.monotonic()
            try:
                output = step.run(ctx)
                self.store.finish_step(job.id, step.name, output, self.monotonic() - started, ctx.cost_eur,
                                       now=self.now())
            except (StepFailed, StudioStoreError) as error:
                # StudioStoreError aquí = la salida del paso no se puede guardar: repetirlo daría lo mismo.
                self._give_up(job, step, str(error), self.monotonic() - started)
                return
            except Exception as error:
                self._retry_or_give_up(job, step, f"{type(error).__name__}: {error}", self.monotonic() - started)
                return
            outputs[step.name] = output
            log.info("Studio: trabajo #%d, paso «%s» hecho en %.1f s", job.id, step.name, self.monotonic() - started)
        self.store.finish_job(job.id, "done", now=self.now())
        log.info("Studio: trabajo #%d terminado", job.id)

    def _give_up(self, job: Job, step: Step, error: str, seconds: float) -> None:
        self.store.fail_step(job.id, step.name, error, seconds, now=self.now())
        self.store.finish_job(job.id, "failed", f"«{step.name}»: {error}", now=self.now())
        log.warning("Studio: trabajo #%d falló en «%s»", job.id, step.name)

    def _retry_or_give_up(self, job: Job, step: Step, error: str, seconds: float) -> None:
        attempt = job.attempts + 1  # intentos hechos contando este
        if attempt >= self.max_attempts:
            self._give_up(job, step, f"{error} (tras {attempt} intentos)", seconds)
            return
        self.store.fail_step(job.id, step.name, error, seconds, now=self.now())
        wait = min(self.backoff * (2 ** job.attempts), self.max_backoff)
        self.store.retry_later(job.id, f"«{step.name}»: {error}", self.now() + wait, now=self.now())
        log.warning("Studio: trabajo #%d, «%s» falló; reintento en %d s", job.id, step.name, wait.total_seconds())

    # --- avisos (la web los recibirá por el EventBus) ------------------------

    def _notify_id(self, job_id: int) -> None:
        job = self.store.get_job(job_id)
        if job:
            self._notify(job)

    def _notify(self, job: Job) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(job)
        except Exception:
            # Un fallo pintando la web nunca debe parar la producción de un vídeo.
            log.exception("Studio: fallo avisando del cambio del trabajo #%d", job.id)
