"""La orden del asistente de la web (`arthur --webchat-web`).

Separado de `__main__` para poder probar el montaje sin arrancar el servidor: `build_runtime` solo lee el entorno
y arma las piezas; `webchat_command` las junta y arranca. Los contactos ya no se listan ni se borran aquí (antes
`--webchat-contactos` y `--webchat-borrar`): viven en el registro de clientes y se gestionan en su página.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_PORT = 8776
DEFAULT_ORIGIN = "https://ejemplo.com"
DEFAULT_KNOWLEDGE = "/conocimiento/conocimiento.md"
DEFAULT_CF_TOKEN = "/run/secrets/workers_ai_token"
DEFAULT_TURNSTILE = "/run/secrets/turnstile_secret"
DEFAULT_CLIENTES_TOKEN = "/run/secrets/clientes_webchat"
RETRY_EVERY = 60   # cada cuánto se reintenta entregar la cola de contactos a clientes

log = logging.getLogger("arthur.webchat")


def _now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid"))


def webchat_dir(env: Mapping[str, str]) -> Path:
    # Se lee del `env` recibido (no de os.environ) para poder probarlo sin tocar el entorno real.
    base = env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "arthur" / "webchat"


def contacts_file(env: Mapping[str, str]) -> Path:
    # La base VIEJA de contactos (hasta 0.62): solo se lee una vez, para migrarla a clientes, y se borra.
    return webchat_dir(env) / "contactos.sqlite3"


def queue_dir(env: Mapping[str, str]) -> Path:
    # En el volumen del chat: si el contenedor se reinicia con contactos en cola, siguen ahí.
    return webchat_dir(env) / "cola"


@dataclass
class Runtime:
    host: str
    port: int
    origin: str
    knowledge: object
    limits: object
    limiter: object
    passes: object
    turnstile: object
    clientes: object | None     # ClientesLink, o None sin ARTHUR_WEBCHAT_CLIENTES_URL (el contacto contesta 503)
    notifier: object | None
    workers_ai: object | None   # None = sin cuenta o token válidos: se contesta con el modelo local
    local: object
    answerer: object


def _read_secret(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _workers_ai(env: Mapping[str, str]):
    """El motor de Workers AI, o None si falta algo. Nunca tumba la web: sin él contesta el modelo local.
    Los avisos dicen QUÉ falta, nunca el token (ni un trozo)."""
    from ..notes.engines import EngineRefused
    from .workers_ai import WorkersAIEngine

    account = (env.get("ARTHUR_WEBCHAT_CF_ACCOUNT") or "").strip()
    if not account:
        log.warning("Webchat: sin ARTHUR_WEBCHAT_CF_ACCOUNT; solo modelo local")
        return None
    token_file = Path(env.get("ARTHUR_WEBCHAT_CF_TOKEN_FILE") or DEFAULT_CF_TOKEN)
    try:
        token = token_file.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        token = None
    except OSError as e:
        # Un secreto de compose que el contenedor no puede leer (permisos del anfitrión) tumbaría la web entera.
        # Mejor arrancar y contestar con el modelo local.
        log.warning("Webchat: no se puede leer el token de Workers AI (%s); solo modelo local", type(e).__name__)
        return None
    if token is None:
        log.warning("Webchat: sin token de Workers AI; solo modelo local")
        return None
    try:
        return WorkersAIEngine(account, token, (env.get("ARTHUR_WEBCHAT_CF_MODEL") or "").strip() or None)
    except EngineRefused as e:
        # El mensaje de EngineRefused no lleva el token (ver workers_ai.py).
        log.warning("Webchat: Workers AI no se puede usar (%s); solo modelo local", e)
        return None


def _clientes(env: Mapping[str, str]):
    """El enlace con el registro de clientes, o None si falta algo. Los avisos dicen QUÉ falta, nunca la llave."""
    from .clientes_link import ClientesLink

    url = (env.get("ARTHUR_WEBCHAT_CLIENTES_URL") or "").strip()
    if not url:
        log.warning("Webchat: sin ARTHUR_WEBCHAT_CLIENTES_URL; el formulario de contacto contestará «no disponible»")
        return None
    token_file = Path(env.get("ARTHUR_WEBCHAT_CLIENTES_TOKEN_FILE") or DEFAULT_CLIENTES_TOKEN)
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("Webchat: no se puede leer la llave de clientes (%s); sin formulario de contacto",
                    type(e).__name__)
        return None
    try:
        return ClientesLink(url, token, queue_dir(env))
    except ValueError as e:
        # El mensaje de ClientesLink no lleva la llave.
        log.warning("Webchat: no se puede hablar con clientes (%s); sin formulario de contacto", e)
        return None


def build_runtime(env: Mapping[str, str], now: Callable[[], datetime] = _now) -> Runtime:
    """Arma las piezas desde las variables de entorno. No abre puertos ni hilos."""
    from ..notes import engines as notes_engines
    from ..studio.notify import Notifier
    from .answer import Answerer
    from .knowledge import Knowledge
    from .limits import Limits, RateLimiter
    from .session import Passes, Turnstile

    limits = Limits.from_env(env)
    limiter = RateLimiter(limits, now)
    knowledge = Knowledge(Path(env.get("ARTHUR_WEBCHAT_CONOCIMIENTO") or DEFAULT_KNOWLEDGE))

    workers_ai = _workers_ai(env)
    local = notes_engines.LocalEngine(env.get("ARTHUR_OLLAMA_URL") or None)

    turnstile = Turnstile(_read_secret(env.get("ARTHUR_WEBCHAT_TURNSTILE_FILE") or DEFAULT_TURNSTILE))
    # Clave aleatoria en cada arranque: al reiniciar los pases caducan y no hay nada que guardar ni filtrar.
    # Los pases llevan su caducidad en segundos desde la época: por eso `time.time`.
    passes = Passes(secrets.token_bytes(32), time.time)
    notifier = Notifier.from_env(env, prefix="ARTHUR_WEBCHAT_AVISOS", default_token_file="/run/secrets/webchat_avisos")
    return Runtime(
        host=env.get("ARTHUR_WEBCHAT_HOST", "127.0.0.1"),
        port=int(env.get("ARTHUR_WEBCHAT_PORT", str(DEFAULT_PORT))),
        origin=env.get("ARTHUR_WEBCHAT_ORIGIN") or DEFAULT_ORIGIN,
        knowledge=knowledge, limits=limits, limiter=limiter, passes=passes, turnstile=turnstile,
        clientes=_clientes(env), notifier=notifier, workers_ai=workers_ai, local=local,
        answerer=Answerer(knowledge, limiter, workers_ai, local))


def webchat_command(args, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    if args.webchat_web:
        return _serve(env)
    return 0


def migrate_and_flush(link, notifier, env: Mapping[str, str]) -> int:
    """Al arrancar: pasa a la cola los contactos de la base vieja (una sola vez) y entrega la cola. Devuelve cuántos
    han llegado a clientes."""
    from .clientes_link import migrate_old_contacts, notify

    migrate_old_contacts(contacts_file(env), link)
    return link.flush(lambda client_id, nuevo: notify(notifier, (client_id, nuevo)))


def _retry_loop(link, notifier, stop) -> None:
    # Los contactos que quedaron en cola (clientes caído o lento) se reintentan cada minuto; al llegar, aviso al móvil
    # con su número, como si hubieran llegado en el momento.
    from .clientes_link import notify

    while True:
        try:
            n = link.flush(lambda client_id, nuevo: notify(notifier, (client_id, nuevo)))
            if n:
                log.info("Webchat: %d contactos de la cola entregados a clientes", n)
        except Exception as e:  # noqa: BLE001 — un fallo del reintento no puede parar el hilo ni la web
            log.error("Webchat: el reintento de la cola ha fallado (%s)", type(e).__name__)
        if stop.wait(RETRY_EVERY):
            return


def _serve(env: Mapping[str, str]) -> int:
    from .web import WebchatWeb

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rt = build_runtime(env)
    if rt.knowledge.text() is None:
        log.error("Webchat: falta o no se puede leer %s: se contestará con el mensaje fijo", rt.knowledge.path)
    web = WebchatWeb(rt.answerer, rt.limiter, rt.passes, rt.turnstile, rt.clientes, rt.notifier,
                     origin=rt.origin, host=rt.host, port=rt.port)
    stop = threading.Event()
    if rt.clientes is not None:
        # La migración solo con clientes configurado: sin él, la base vieja se queda donde está hasta que lo esté.
        try:
            migrate_and_flush(rt.clientes, rt.notifier, env)
        except Exception as e:  # noqa: BLE001 — una base vieja rota no puede impedir que arranque la web
            log.error("Webchat: la migración de contactos antiguos ha fallado (%s)", type(e).__name__)
        threading.Thread(target=_retry_loop, args=(rt.clientes, rt.notifier, stop), daemon=True).start()
    print(f"Asistente de la web en {web.url} (Ctrl+C para parar)", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        web.shutdown()
    return 0
