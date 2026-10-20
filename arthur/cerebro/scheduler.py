"""El reloj del cerebro: cada 15 minutos (y al arrancar) pone el índice al día con cada fuente y calcula los
embeddings que falten.

Cada fuente va por su cuenta: si Apuntes está apagado o Syncthing aún no ha montado una bóveda, esa fuente se apunta
con su error en el estado (GET /api/estado) y las demás se indexan igual. Lo ya indexado de la fuente caída se
queda como estaba: se sigue pudiendo preguntar por ello.

Apuntes manda solo lo cambiado desde la última vez que fue bien (`since`). Se guarda la hora en que EMPEZÓ esa
pasada, no en la que acabó: un apunte que cambie mientras se descarga la lista sale otra vez en la siguiente
(repetirlo no cuesta nada, el índice compara por huella; perderlo dejaría el índice viejo). Mientras no haya ido
bien ninguna vez desde que arrancó el cerebro, se pide todo.

`RepasoClock` es el reloj del Repaso (mira la hora cada minuto, en hora de Madrid):
- 02:00: prepara las preguntas nuevas de la noche (repaso/generate.py). Solo entre las 02:00 y las 06:00: si el
  cerebro arranca a mediodía no se pone a gastar presupuesto, que de día es para las preguntas de Marc. Lo hecho
  cada noche se guarda en el almacén del Repaso (tabla `nights`), no en memoria: si el cerebro se reinicia a las
  03:00 no vuelve a generar (rompería el tope de 40), y cada intento pide solo lo que falta para 40 (contando las
  preguntas ya guardadas ese día, por si murió antes de apuntar el resultado). Si se paró por un fallo de Workers AI
  (un 429, un corte) se reintenta cada 30 minutos dentro de la ventana; con el tope, sin presupuesto o con la
  petición rechazada, la noche queda cerrada. El intento se apunta ANTES de llamar: un cerebro que muere a media
  generación tampoco la relanza en cada arranque.
- A la hora del aviso (ajustes, 20:00 por defecto): si la sesión de hoy no está hecha, hay preguntas y aún no se
  avisó, manda «N preguntas te esperan · racha R días» al móvil (núcleo → Apuntes en #repaso). Si el cerebro
  arranca tarde, avisa al arrancar, pero no a partir de las 23:00 (un aviso a medianoche no ayuda a nadie). Si el
  aviso no llega (el núcleo caído, o sin la dirección de Apuntes: 503), el día NO queda avisado y se reintenta
  cada 15 minutos; el motivo queda en el estado.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .chunks import Doc

log = logging.getLogger(__name__)

EVERY = 900  # 15 minutos
EMBED_LIMIT = 500  # trozos por pasada: el primer índice se reparte entre pasadas (y días, según el presupuesto)
MAX_ERROR = 200


@dataclass(frozen=True)
class Source:
    """Una fuente del índice. `name` es también su `source` en el índice (`obsidian:cerebro`, `apuntes`, `pdf`…).
    `read(since)` devuelve (documentos, refs vivas): refs vivas None = escaneo completo, lo que falte se borra."""

    name: str
    read: Callable[[str | None], tuple[Iterable[Doc], set[str] | None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Reindexer:
    def __init__(self, index, sources_fn: Callable[[], list[Source]], every: int = EVERY, *,
                 embedder: Callable[[], object | None] | None = None,
                 now: Callable[[], datetime] = _utc_now, embed_limit: int = EMBED_LIMIT) -> None:
        self.index = index
        self.sources_fn = sources_fn  # se pide en cada pasada: así una fuente nueva entra sin reiniciar
        self.every = every
        self.embedder = embedder or (lambda: None)
        self.now = now
        self.embed_limit = embed_limit
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self._last_run: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        """Una pasada entera. Nunca lanza: lo que falle queda en el estado y se reintenta en la siguiente."""
        try:
            sources = list(self.sources_fn())
        except Exception as e:  # noqa: BLE001
            log.exception("Cerebro: no se han podido preparar las fuentes")
            sources = []
            with self._lock:
                self._state.setdefault("_fuentes", {})["error"] = type(e).__name__
        for src in sources:
            self._sync(src)
        embedder = None
        try:
            embedder = self.embedder()
        except Exception:  # noqa: BLE001
            log.exception("Cerebro: no se ha podido preparar el embedder")
        if embedder is not None:
            try:
                done = self.index.embed_pending(embedder, limit=self.embed_limit)
                if done:
                    log.info("Cerebro: %d trozos nuevos con significado", done)
            except Exception:  # noqa: BLE001 — un fallo raro del índice no debe parar el reloj
                log.exception("Cerebro: los embeddings han fallado")
        with self._lock:
            self._last_run = self.now().isoformat()

    def _sync(self, src: Source) -> None:
        with self._lock:
            st = self._state.setdefault(src.name, {"ultima_ok": None, "error": None, "cambios": None})
            since = st["ultima_ok"]
        started = self.now().isoformat()
        try:
            docs, alive = src.read(since)
            counts = self.index.sync(list(docs), source=src.name, alive_refs=alive)
        except Exception as e:  # noqa: BLE001 — una fuente caída no para las demás
            # Solo el tipo en el registro; el mensaje (ya pensado para enseñar: ToolError, OSError) va al estado.
            log.warning("Cerebro: la fuente %s ha fallado (%s)", src.name, type(e).__name__)
            with self._lock:
                st["error"] = (str(e) or type(e).__name__)[:MAX_ERROR]
            return
        if any(counts.values()):
            log.info("Cerebro: %s → %d nuevos, %d cambiados, %d borrados", src.name,
                     counts["nuevos"], counts["cambiados"], counts["borrados"])
        with self._lock:
            st.update(ultima_ok=started, error=None, cambios=counts)

    def state(self) -> dict:
        with self._lock:
            return {"ultima_pasada": self._last_run,
                    "fuentes": {k: dict(v) for k, v in self._state.items() if not k.startswith("_")}}

    # ── Hilo ────────────────────────────────────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cerebro-reindexer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.every)


# ── Repaso ──────────────────────────────────────────────────────────────────────────────────────────────────────

MADRID = ZoneInfo("Europe/Madrid")
REPASO_EVERY = 60
GENERATE_FROM, GENERATE_UNTIL = time(2, 0), time(6, 0)
NOTICE_UNTIL = time(23, 0)
NOTICE_RETRY = timedelta(minutes=15)
GENERATE_RETRY = timedelta(minutes=30)
NIGHT_LIMIT = 40   # preguntas nuevas por noche (Global Constraints), sumando los reintentos


def _madrid_now() -> datetime:
    return datetime.now(MADRID)


def notice_text(pending: int, streak: int) -> str:
    text = f"{pending} preguntas te esperan" if pending != 1 else "1 pregunta te espera"
    if streak:
        text += f" · racha {streak} {'día' if streak == 1 else 'días'}"
    return text


class RepasoClock:
    def __init__(self, repaso, generate_fn: Callable[[datetime, int], dict], notifier, *,
                 every: int = REPASO_EVERY, now: Callable[[], datetime] = _madrid_now) -> None:
        self.repaso = repaso              # repaso.service.Repaso (ajustes, almacén, pendientes)
        self.generate_fn = generate_fn    # (ahora, límite) -> resultado de repaso.generate.generate
        self.notifier = notifier          # Notifier del núcleo (llave «cerebro»), o None sin avisos
        self.every = every
        self.now = now
        self._lock = threading.Lock()
        self._generation: dict | None = None
        self._notice: dict = {"dia": None, "enviado": None, "error": None}
        self._retry_at: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        """Una mirada al reloj. Nunca lanza: un fallo se apunta en el estado y el reloj sigue."""
        now = self.now().astimezone(MADRID)
        self._maybe_generate(now)
        try:
            self._maybe_notify(now)
        except Exception as e:  # noqa: BLE001
            log.exception("Repaso: el aviso ha fallado")
            with self._lock:
                self._notice["error"] = type(e).__name__

    def _maybe_generate(self, now: datetime) -> None:
        if not GENERATE_FROM <= now.time() < GENERATE_UNTIL:
            return
        store = self.repaso.store
        today = now.date()
        night = store.night(today)
        if night is not None and (not night["reintentar"] or now < night["intento"] + GENERATE_RETRY):
            return  # noche cerrada, o reintento aún no toca (cada minuto gastaría presupuesto)
        before = night["creadas"] if night else 0
        left = NIGHT_LIMIT - store.created_on(today)
        if left <= 0:
            store.save_night(today, attempt=now, created=max(before, NIGHT_LIMIT), stopped="tope de la noche",
                             retry=False)
            return
        # Se apunta antes de llamar y como «a reintentar»: si el proceso muere a medias, el próximo arranque
        # espera los 30 minutos en vez de relanzarla enseguida
        store.save_night(today, attempt=now, created=before, stopped="en marcha", retry=True)
        try:
            result = self.generate_fn(now, left)
        except Exception as e:  # noqa: BLE001
            log.exception("Repaso: preparar las preguntas ha fallado")
            result = {"error": type(e).__name__, "hecho_en": now.isoformat()}
        retry = bool(result.get("reintentar")) or "error" in result
        store.save_night(today, attempt=now, created=before + int(result.get("creadas") or 0),
                         stopped=result.get("parado") or result.get("error"), retry=retry)
        with self._lock:
            self._generation = result

    def _maybe_notify(self, now: datetime) -> None:
        notice = self.repaso.settings.notice
        # Si Marc lo pone a las 23:30, se respeta: el corte de las 23:00 es para los arranques tardíos
        until = NOTICE_UNTIL if notice < NOTICE_UNTIL else time(23, 59, 59)
        if not notice <= now.time() < until:
            return
        if self._retry_at is not None and now < self._retry_at:
            return
        store = self.repaso.store
        today = now.date()
        day = store.day(today)
        if day["done"] or day["notified"]:
            return
        pending = self.repaso.pending(now)
        if not pending:
            return
        with self._lock:
            self._notice = {"dia": today.isoformat(), "enviado": None, "error": None}
        if self.notifier is None:
            with self._lock:
                self._notice["error"] = "sin avisos configurados (ARTHUR_CEREBRO_AVISOS_URL)"
            self._retry_at = now + NOTICE_RETRY
            return
        from .repaso.session import streak
        if self.notifier.send(notice_text(pending, streak(store, today)), destination="apuntes"):
            store.mark_day(today, notified=True)
            self._retry_at = None
            with self._lock:
                self._notice["enviado"] = now.isoformat()
            log.info("Repaso: aviso enviado")
        else:
            # Notifier ya ha dejado el código en el registro; el día no se marca, así se vuelve a intentar
            self._retry_at = now + NOTICE_RETRY
            with self._lock:
                self._notice["error"] = "el aviso no ha llegado al núcleo; se reintenta en 15 minutos"

    def state(self) -> dict:
        with self._lock:
            generation = None if self._generation is None else dict(self._generation)
            notice = dict(self._notice)
        if generation is None:
            # Recién arrancado: lo último guardado de esta noche (si lo hay), para que el estado no salga vacío
            try:
                night = self.repaso.store.night(self.now().astimezone(MADRID).date())
            except Exception:  # noqa: BLE001 — el estado nunca debe romper la página
                night = None
            if night is not None:
                generation = {"creadas": night["creadas"], "parado": night["parado"],
                              "reintentar": night["reintentar"], "hecho_en": night["intento"].isoformat()}
        return {"generacion": generation, "aviso": notice}

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cerebro-repaso", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.every)
