"""Piloto automático (paso 6): cada día, una historia nueva en los dos canales, lista para que Marc la revise.

Es un trabajo de la cola (jobs.py): si el servidor se reinicia a mitad, sigue desde el último paso terminado.

  idea        Gemini propone una historia real que no hayamos contado (con fuentes del catálogo de writer.py)
  guion       guion en español (Gemini escribe, Gemini revisa, el código comprueba) → proyectos/<fecha>-<título>/
  guion_en    la misma historia en inglés (mismas escenas, imágenes y fuentes) → proyectos/<…>-en/
  producir_es imágenes con licencia, voz de Dora, montaje, corto vertical → página de revisión (largo + corto)
  producir_en lo mismo con la voz de Heart → página de revisión

Publicar (decisión de Marc, 2026-10-13): a las 18:00 (ARTHUR_STUDIO_PUBLICAR_HORA) la página de revisión aprueba
SOLA los vídeos del piloto que siguen esperando, son de las últimas 24 h y NO tienen dudas del revisor. Los que tienen
dudas esperan al «Aprobar» de Marc. «Descartar» o «Pedir cambios» antes de esa hora lo paran. El modelo sigue sin
poder publicar nada: lo decide este código con reglas fijas.
Órdenes: arthur --studio-piloto-ahora (una vez, esperando) y --studio-piloto-diario (servicio studio-piloto).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from .assets import CommonsClient
from .gemini import GeminiClient, GeminiError, GeminiRefused, load_gemini_key
from .jobs import Step, StepContext, StepFailed
from .montage import MontageError, produce
from .project import ScriptError, load_script, prepare, project_dir, projects_dir
from .review import CHECK_FILE, Review, ReviewBook, ReviewError
from .shorts import produce_short
from .store import StudioStore
from .voice import VoiceError, engine_for, narrate, write_srt
from .writer import Writer, WriterError, slugify

log = logging.getLogger(__name__)

KIND = "piloto"
DEFAULT_HOUR = 6   # a las 6:00 empieza; Gemini, voces y montajes tardan, y a media mañana está en la página
PUBLISH_HOUR = 18  # si Marc no ha dicho nada, a esta hora se publica lo que no tenga dudas (buena hora en España)
AUTO_DEVICE = "piloto-automatico"


def running(store: StudioStore):
    """El trabajo del piloto que está en cola o en marcha, si hay alguno (para no lanzar dos a la vez)."""
    return next((j for j in store.list_jobs(limit=20) if j.kind == KIND and j.status in ("queued", "running")), None)


def started_today(store: StudioStore, today: datetime) -> bool:
    """¿Ya se lanzó hoy el piloto? Cuenta aunque fallara o se cancelara: así un error no lo repite en bucle
    (y no gasta las peticiones gratis del día). Para repetirlo a mano: --studio-piloto-ahora."""
    return any(j.kind == KIND and j.created_at.date() == today.date() for j in store.list_jobs(limit=100))


def daily_loop(store: StudioStore, submit: Callable[[], object], stop: threading.Event, *, hour: int = DEFAULT_HOUR,
               now: Callable[[], datetime] = datetime.now, poll: float = 60.0) -> None:
    """Cada minuto mira si toca. Si el servidor estaba apagado a esa hora, lo lanza al encender (ese mismo día)."""
    while not stop.is_set():
        current = now()
        try:
            if current.hour >= hour and not started_today(store, current):
                log.info("Studio piloto: lanzando el vídeo del día")
                submit()
        except Exception:
            log.exception("Studio piloto: no se pudo lanzar el vídeo del día")
        stop.wait(poll)


# Experimento del kit de marca (Business Lab): «Una carta de Séneca» los miércoles y los domingos, «La prueba»
# (una historia real) el resto. Por día de la semana y no al azar: así se sabe qué formato tiene cada vídeo
# y los dos salen con regularidad para poder compararlos.
CARTA_DAYS = (2, 6)  # datetime.weekday(): lunes = 0


def format_for(day: datetime) -> str:
    return "carta" if day.weekday() in CARTA_DAYS else "prueba"


def done_titles(store: StudioStore) -> list[str]:
    titles = [r.title for r in ReviewBook(store).list(200)]
    if projects_dir().exists():
        for path in sorted(projects_dir().glob("*/guion.toml")):
            try:
                titles.append(load_script(path).titulo)
            except ScriptError:
                pass
    return list(dict.fromkeys(titles))


def produce_language(store: StudioStore, name: str, progress: Callable[[str], None]) -> dict:
    folder = project_dir(name)
    preparation = prepare(name, store, CommonsClient(store))
    if not preparation.ready:
        raise StepFailed(f"{name}: imágenes sin licencia válida: " + ", ".join(a.title for a in preparation.blocked))
    script = preparation.script
    narration = narrate(script, folder, engine_for(script.voz, script.idioma))
    write_srt(narration, folder / "salida" / "subtitulos.srt")
    report = produce(script, store.assets(name), folder, progress=progress)
    if not report["ok"]:
        raise StepFailed(f"{name}: control de calidad: " + "; ".join(report["problemas"]))
    book = ReviewBook(store)
    result = {"largo": book.submit(name, "largo").id}
    try:
        short = produce_short(script, store.assets(name), folder, progress=progress)
        if short["ok"]:
            result["corto"] = book.submit(name, "corto").id
    except MontageError as e:
        result["corto_error"] = str(e)
    return result


def autopilot_steps(store: StudioStore, *, gemini: Callable[[], GeminiClient] | None = None,
                    commons: Callable[[], CommonsClient] | None = None, now: Callable[[], datetime] = datetime.now,
                    produce_fn=produce_language) -> tuple[Step, ...]:
    clients: dict[str, GeminiClient] = {}

    def make_gemini() -> GeminiClient:
        # Uno para todo el trabajo: así la lista de modelos se pide una vez, no en cada paso.
        if "gemini" not in clients:
            if gemini is not None:
                clients["gemini"] = gemini()
            else:
                key = load_gemini_key()
                if not key:
                    raise StepFailed("Falta la clave de Gemini: arthur --studio-clave-gemini")
                clients["gemini"] = GeminiClient(key, store)
        return clients["gemini"]

    def writer() -> Writer:
        return Writer(make_gemini(), commons() if commons else CommonsClient(store),
                      progress=lambda m: log.info("Studio piloto: %s", m))

    def guarded(run):
        def wrapper(ctx: StepContext) -> dict:
            try:
                return run(ctx)
            except GeminiRefused as e:
                raise StepFailed(str(e)) from None
            except GeminiError:
                raise  # pasajero: la cola lo reintenta
            except (WriterError, ScriptError, ReviewError, VoiceError, MontageError) as e:
                raise StepFailed(str(e)) from None
        return wrapper

    def idea(ctx):
        tema = str(ctx.job.params.get("tema", ""))[:200]  # si se pidió desde ARTHUR o la página («…sobre Séneca»)
        return {"idea": writer().idea(done_titles(store), formato=format_for(now()), tema=tema)}

    def guion(ctx):
        draft = writer().draft(ctx.outputs["idea"]["idea"])
        base = f"{now():%Y%m%d}-{slugify(draft.script.titulo, 40)}"
        name, n = base, 2
        while project_dir(name).exists():  # nunca pisar un proyecto que ya existe (dos ideas con el mismo título)
            name, n = f"{base}-{n}", n + 1
        folder = project_dir(name)
        folder.mkdir(parents=True)
        (folder / "guion.toml").write_text(draft.toml, encoding="utf-8")
        (folder / CHECK_FILE).write_text(json.dumps({"problemas": draft.problems}, ensure_ascii=False), encoding="utf-8")
        return {"proyecto": name, "titulo": draft.script.titulo, "avisos": len(draft.problems)}

    def guion_en(ctx):
        name = ctx.outputs["guion"]["proyecto"]
        text = writer().translate((project_dir(name) / "guion.toml").read_text(encoding="utf-8"))
        name_en = f"{name[:55]}-en"
        folder = project_dir(name_en)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "guion.toml").write_text(text, encoding="utf-8")
        check = project_dir(name) / CHECK_FILE
        if check.exists():
            (folder / CHECK_FILE).write_text(check.read_text(encoding="utf-8"), encoding="utf-8")
        return {"proyecto": name_en}

    def producir(key):
        return lambda ctx: produce_fn(store, ctx.outputs[key]["proyecto"], lambda m: log.info("Studio piloto: %s", m))

    return (Step("idea", guarded(idea)), Step("guion", guarded(guion)), Step("guion_en", guarded(guion_en)),
            Step("producir_es", guarded(producir("guion"))), Step("producir_en", guarded(producir("guion_en"))))


def job_notice(store: StudioStore, job, publish_hour: int = PUBLISH_HOUR) -> str | None:
    """El aviso por ARTHUR cuando un trabajo del piloto termina (None si no toca avisar)."""
    if job.kind != KIND or job.status not in ("done", "failed"):
        return None
    if job.status == "failed":
        return f"Studio: el piloto de hoy ha fallado ({(job.error or 'sin detalle')[:150]})."
    outputs = {s.name: s.output or {} for s in store.steps(job.id)}
    title = str(outputs.get("guion", {}).get("titulo", "sin título"))[:120]
    doubts = outputs.get("guion", {}).get("avisos", 0)
    if doubts:
        return (f"Studio: vídeo nuevo «{title}». Tiene {doubts} duda{'s' if doubts != 1 else ''} del revisor: "
                "no se publica solo. Revísalo en la página.")
    return (f"Studio: vídeo nuevo «{title}», sin dudas del revisor. Se publica solo a las {publish_hour}:00 "
            "si no lo descartas.")


def auto_approvable(book: ReviewBook, now: datetime) -> list[Review]:
    """Lo que se puede aprobar solo: esperando, del piloto, de las últimas 24 h y SIN ninguna duda del revisor."""
    chosen = []
    for review in book.list(200):
        if review.status != "pending":
            continue
        try:
            created = datetime.fromisoformat(review.created_at).replace(tzinfo=None)
            warnings = book.warnings(review.project)
        except (ValueError, ScriptError):
            continue
        if warnings == [] and timedelta(0) <= now - created <= timedelta(hours=24):
            chosen.append(review)
    return chosen


def auto_publish_loop(book: ReviewBook, enqueue: Callable[[int], None], stop: threading.Event, *,
                      hour: int = PUBLISH_HOUR, now: Callable[[], datetime] = datetime.now, poll: float = 60.0) -> None:
    """Corre en la página de revisión (siempre encendida, y es la que tiene el publicador)."""
    while not stop.is_set():
        current = now()
        if current.hour >= hour:
            for review in auto_approvable(book, current):
                try:
                    # La misma comprobación de huellas que al pulsar «Aprobar»: si algo cambió, no se aprueba.
                    book.approve(review.id, review.video_sha256, AUTO_DEVICE)
                except ReviewError as e:
                    log.warning("Studio piloto: el vídeo #%d no se publica solo: %s", review.id, e)
                    continue
                log.info("Studio piloto: vídeo #%d aprobado solo (sin dudas del revisor)", review.id)
                enqueue(review.id)
        stop.wait(poll)
