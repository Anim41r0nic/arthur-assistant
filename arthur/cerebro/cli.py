"""Las órdenes del cerebro (`arthur --cerebro-web`, `--cerebro-dispositivo-nuevo NOMBRE`).

Separado de `__main__` para poder probar el montaje sin arrancar el servidor: `build_runtime` solo lee el entorno
y los ajustes y arma las piezas (motores, embedder, enlace con Apuntes, fuentes); `_serve` las junta y arranca.

Variables (en el contenedor las pone compose; los secretos llegan como ficheros en /run/secrets):
  ARTHUR_CEREBRO_HOST / ARTHUR_CEREBRO_PORT   dónde escucha (127.0.0.1:8778; en Docker, 0.0.0.0 sin publicar)
  ARTHUR_APUNTES_URL                          Apuntes dentro de la red de Docker (http://apuntes-web:8774)
  ARTHUR_CEREBRO_APUNTES_KEY_FILE             la llave del aparato «cerebro» en Apuntes (secreto cerebro_apuntes)
  ARTHUR_APUNTES_PUBLIC_URL                   la dirección de Apuntes que abre Marc (para los enlaces de las citas)
  ARTHUR_WEBCHAT_CF_ACCOUNT                   cuenta de Cloudflare (la misma que el chat de la web)
  ARTHUR_CEREBRO_CF_TOKEN_FILE                token de Workers AI (el mismo secreto workers_ai_token)
  ARTHUR_OLLAMA_URL / ARTHUR_OLLAMA_KEEP_ALIVE   Ollama en casa (los lee LocalEngine)
  ARTHUR_CEREBRO_AVISOS_URL                   el núcleo (http://core:8770) para el aviso diario del Repaso
  ARTHUR_CEREBRO_AVISOS_TOKEN_FILE            la llave del aparato «cerebro» en el núcleo (secreto cerebro_avisos)

Si falta algo, el cerebro arranca igual con lo que tenga: sin Workers AI contesta Ollama y solo busca por
palabras; sin Apuntes indexa solo las bóvedas. Los avisos dicen QUÉ falta, nunca una llave ni un token.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .settings import CerebroError, Settings, cerebro_dir

log = logging.getLogger("arthur.cerebro")

DEFAULT_PORT = 8778
DEFAULT_CF_TOKEN = "/run/secrets/workers_ai_token"
DEFAULT_APUNTES_KEY = "/run/secrets/cerebro_apuntes"
DEFAULT_AVISOS_KEY = "/run/secrets/cerebro_avisos"
DIARIO_MAX_TOKENS = 600  # tres apartados de hasta 12 viñetas
# Entre los dos, por debajo de los 90 s que espera el panel (y el portátil detrás): si no, el portátil daría el día
# por fallido aunque la nota llegara a hacerse.
DIARIO_CLOUD_TIMEOUT = 25
DIARIO_LOCAL_TIMEOUT = 55


def devices_file() -> Path:
    return cerebro_dir() / "devices.json"


@dataclass
class Runtime:
    host: str
    port: int
    budget: object             # Budget
    cloud: object | None       # WorkersAIEngine, o None sin cuenta o token válidos
    embedder: object | None    # Embedder (misma cuenta y token), o None: entonces solo se busca por palabras
    local: object              # LocalEngine (Ollama)
    apuntes: object | None     # ServiceLink con Apuntes, o None
    links: object              # answer.Links
    sources: Callable[[], list]   # () -> [scheduler.Source]


def _secret(path: str, what: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        log.warning("Cerebro: falta %s (%s)", what, path)
    except OSError as e:
        # Un secreto que el contenedor no puede leer (permisos del anfitrión) no debe tumbar el cerebro entero.
        log.warning("Cerebro: no se puede leer %s (%s)", what, type(e).__name__)
    return None


def build_runtime(env: Mapping[str, str], settings: Settings, *, data: Path | None = None) -> Runtime:
    """Arma las piezas desde el entorno y los ajustes. No abre puertos, hilos ni la base de datos."""
    from ..notes.engines import EngineRefused, LocalEngine
    from ..tools.services import ServiceLink
    from ..webchat.workers_ai import WorkersAIEngine
    from . import scheduler
    from .answer import Links
    from .budget import Budget
    from .embed import Embedder
    from .sources import apuntes as apuntes_src
    from .sources import obsidian, pdf

    data = data or cerebro_dir()
    budget = Budget(data / "presupuesto.json", daily=settings.budget)

    cloud = embedder = None
    account = (env.get("ARTHUR_WEBCHAT_CF_ACCOUNT") or "").strip()
    if not account:
        log.warning("Cerebro: sin ARTHUR_WEBCHAT_CF_ACCOUNT; contesta Ollama y se busca solo por palabras")
    else:
        token = _secret(env.get("ARTHUR_CEREBRO_CF_TOKEN_FILE") or DEFAULT_CF_TOKEN, "el token de Workers AI")
        if token:
            try:
                cloud = WorkersAIEngine(account, token)
                embedder = Embedder(account, token, budget=budget)
            except EngineRefused as e:  # su mensaje no lleva el token
                log.warning("Cerebro: Workers AI no se puede usar (%s)", e)
                cloud = embedder = None

    link = None
    url = (env.get("ARTHUR_APUNTES_URL") or "").strip()
    if url:
        key = _secret(env.get("ARTHUR_CEREBRO_APUNTES_KEY_FILE") or DEFAULT_APUNTES_KEY, "la llave de Apuntes")
        if key:
            link = ServiceLink("Apuntes", url, key)
    else:
        log.info("Cerebro: sin ARTHUR_APUNTES_URL; no se indexan los apuntes")

    def read_apuntes(since: str | None):
        alive = apuntes_src.ids(link)  # primero lo vivo: si falla, no se toca nada
        return apuntes_src.fetch(link, since), alive

    def sources() -> list:
        out = [scheduler.Source("obsidian:cerebro", lambda since: (list(obsidian.scan(settings.vault_cerebro,
                                                                                       "cerebro")), None)),
               scheduler.Source("obsidian:business-lab",
                                lambda since: (list(obsidian.scan(settings.vault_business, "business-lab")), None)),
               scheduler.Source("pdf", lambda since: (list(pdf.scan(settings.pdf)), None))]
        if link is not None:
            out.append(scheduler.Source("apuntes", read_apuntes))
        return out

    return Runtime(host=env.get("ARTHUR_CEREBRO_HOST", "127.0.0.1"),
                   port=int(env.get("ARTHUR_CEREBRO_PORT", str(DEFAULT_PORT))),
                   budget=budget, cloud=cloud, embedder=embedder, local=LocalEngine(env.get("ARTHUR_OLLAMA_URL") or None),
                   apuntes=link, links=Links((env.get("ARTHUR_APUNTES_PUBLIC_URL") or "").strip()), sources=sources)


def diario_engines(rt: Runtime) -> list:
    """Los motores de la nota diaria, en orden. Workers AI se carga a la parte más baja del presupuesto («generate»):
    la nota diaria puede esperar; las preguntas de Marc, no."""
    from .budget import approx_tokens, estimate

    engines = []
    if rt.cloud is not None:
        def cloud(prompt: str) -> str:
            if not rt.budget.allows("generate", estimate("chat", approx_tokens(prompt), DIARIO_MAX_TOKENS)):
                raise RuntimeError("sin presupuesto")
            text = rt.cloud.ask(prompt, max_tokens=DIARIO_MAX_TOKENS, timeout=DIARIO_CLOUD_TIMEOUT)
            rt.budget.spend("generate", estimate("chat", approx_tokens(prompt), approx_tokens(text)))
            return text
        engines.append(("workers-ai", cloud))
    engines.append(("local", lambda prompt: rt.local.ask(prompt, num_predict=DIARIO_MAX_TOKENS,
                                                         timeout=DIARIO_LOCAL_TIMEOUT)))
    return engines


def apunte_link(index, links, chunk_id: int) -> str:
    """«Ver en el apunte» de una pregunta: el apunte de Apuntes del que sale su trozo ("" si ya no está)."""
    chunk = index.chunk(chunk_id)
    ref = (chunk or {}).get("ref") or ""
    if not chunk or chunk.get("source") != "apuntes" or not ref.startswith("apunte:") or not ref[7:].isdigit() \
            or not links.apuntes_url:
        return ""
    return f"{links.apuntes_url}#n{int(ref[7:])}"


def new_device(name: str, registry=None, out=print) -> int:
    from ..security.devices import DeviceError, DeviceRegistry
    from .web import DEVICES

    if name not in DEVICES:
        # Otro nombre se podría dar de alta, pero la API no lo dejaría entrar: mejor decirlo ahora.
        print(f"✗ El cerebro solo atiende a estos aparatos: {', '.join(DEVICES)}", file=sys.stderr)
        return 1
    try:
        key = (registry or DeviceRegistry(devices_file())).add(name)
    except DeviceError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    out(f"Llave de «{name}» para el cerebro (se enseña SOLO esta vez; guárdala como secreto):\n\n{key}\n")
    return 0


def cerebro_command(args, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    if args.cerebro_dispositivo_nuevo:
        return new_device(args.cerebro_dispositivo_nuevo)
    if args.cerebro_web:
        return _serve(env)
    return 0


def _serve(env: Mapping[str, str]) -> int:
    from ..security.devices import DeviceRegistry
    from ..studio.notify import Notifier
    from . import answer, diario
    from .index import Index
    from .repaso.generate import SOURCE as REPASO_SOURCE
    from .repaso.generate import generate, reconcile
    from .repaso.service import Repaso
    from .repaso.store import RepasoStore
    from .scheduler import RepasoClock, Reindexer
    from .settings import load_settings, settings_path
    from .web import CerebroWeb, build_estado

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings()
    except CerebroError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    rt = build_runtime(env, settings)
    index = Index(cerebro_dir() / "indice.sqlite3")
    reindexer = Reindexer(index, rt.sources, embedder=lambda: rt.embedder)
    store = RepasoStore(cerebro_dir() / "repaso.sqlite3")
    # Al cambiar un apunte, sus preguntas pasan al trozo nuevo (o se retiran) en seguida, no a las 02:00
    index.on_change(lambda source: reconcile(index, store) if source == REPASO_SOURCE else None)
    repaso = Repaso(store, settings_path(), link_fn=lambda chunk_id: apunte_link(index, rt.links, chunk_id),
                    subjects_fn=lambda: index.subjects("apuntes"))
    notifier = Notifier.from_env(env, prefix="ARTHUR_CEREBRO_AVISOS", default_token_file=DEFAULT_AVISOS_KEY)
    if notifier is None:
        log.info("Cerebro: sin ARTHUR_CEREBRO_AVISOS_URL; el Repaso no avisa al móvil")
    clock = RepasoClock(repaso, lambda now, limit: generate(index, store, rt.cloud, rt.budget,
                                                            set(repaso.settings.subjects), limit=limit, now=now),
                        notifier)

    def ask_fn(q: str, sources):
        return answer.ask(q, index=index, embedder=rt.embedder, cloud=rt.cloud, local=rt.local, budget=rt.budget,
                          sources=sources, links=rt.links)

    web = CerebroWeb(DeviceRegistry(devices_file()), ask_fn, lambda: build_estado(index, rt.budget, reindexer, clock),
                     lambda material: diario.summarize(material, diario_engines(rt)), repaso=repaso,
                     host=rt.host, port=rt.port)
    reindexer.start()
    clock.start()
    print(f"Cerebro en {web.url} (Ctrl+C para parar)", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        clock.stop()
        reindexer.stop()
        web.shutdown()
        store.close()
        index.close()
    return 0
