"""Las órdenes del panel de vida (`arthur --panel-web`, `--panel-dispositivo-nuevo`, `--panel-google`…).

Separado de `__main__` para poder probar el montaje sin arrancar el servidor: `build_runtime` solo lee el entorno
y arma las piezas (fuentes, motores del resumen, avisos); `panel_command` las junta y arranca.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import panel_dir

DEFAULT_PORT = 8775
DEFAULT_VAULT = "/vault"
# El mismo token que el chat de la web y el cerebro (secreto workers_ai_token); la cuenta, ARTHUR_WEBCHAT_CF_ACCOUNT.
DEFAULT_CF_TOKEN = "/run/secrets/workers_ai_token"


def devices_file() -> Path:
    return panel_dir() / "devices.json"


def gemini_key_file() -> Path:
    return panel_dir() / "gemini-key"


def _now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid"))


@dataclass
class Runtime:
    host: str
    port: int
    vault: object            # ObsidianVault
    calendar: object | None  # None = Google sin autorizar: la tarjeta se salta
    gmail: object | None
    studio_link: object | None
    notifier: object | None
    engines: Callable[[], list]   # () -> motores del resumen, en orden (Workers AI si está, luego Ollama)
    cerebro: object | None = None  # ServiceLink con el segundo cerebro, o None (la caja dice «no disponible»)


def _workers_ai(env: Mapping[str, str], log: logging.Logger):
    """Workers AI para el resumen, como el chat de la web y el cerebro: la misma cuenta y el mismo token. None si
    falta algo o no vale (el aviso dice QUÉ falta, nunca el token): entonces escribe Ollama."""
    from ..notes.engines import EngineRefused
    from ..webchat.workers_ai import WorkersAIEngine

    account = (env.get("ARTHUR_WEBCHAT_CF_ACCOUNT") or "").strip()
    if not account:
        return None
    path = env.get("ARTHUR_PANEL_CF_TOKEN_FILE") or DEFAULT_CF_TOKEN
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("Panel: no puedo leer el token de Workers AI (%s, %s); el resumen lo escribe Ollama",
                    path, type(e).__name__)
        return None
    try:
        return WorkersAIEngine(account, token)
    except EngineRefused as e:  # su mensaje no lleva el token
        log.warning("Panel: Workers AI no se puede usar (%s); el resumen lo escribe Ollama", e)
        return None


def build_runtime(env: Mapping[str, str], settings, *, auth=None, key_file: Path | None = None) -> Runtime:
    """Arma las piezas desde las variables de entorno. No abre puertos ni hilos.
    `key_file` ya no se usa (era la clave de Gemini); se acepta para no romper a quien lo pase."""
    from ..integrations.google import CalendarClient, GmailClient, GoogleAuth
    from ..notes import engines as notes_engines
    from ..studio.notify import Notifier
    from ..tools.services import CEREBRO_TIMEOUT, ServiceLink
    from .sources.obsidian import ObsidianVault
    from .google_keyring import EncryptedFileKeyring

    log = logging.getLogger("arthur.panel")
    auth = auth or GoogleAuth(keyring=EncryptedFileKeyring())
    connected = auth.connected()
    if not connected:
        log.info("Panel: Google sin autorizar (arthur --panel-google): sin calendario ni correo")
    try:
        studio_link = ServiceLink.from_env(env, "Studio", "ARTHUR_PANEL_STUDIO", "panel_studio")
    except ValueError as e:
        log.error("Panel: Studio no se conecta: %s", e)
        studio_link = None
    try:
        # Llave del aparato «panel» en el cerebro (secreto cerebro_panel). Sin ARTHUR_CEREBRO_URL, no hay caja.
        cerebro = ServiceLink.from_env(env, "Cerebro", "ARTHUR_CEREBRO", "cerebro_panel", timeout=CEREBRO_TIMEOUT)
    except ValueError as e:
        log.error("Panel: el cerebro no se conecta: %s", e)
        cerebro = None
    notifier = Notifier.from_env(env, prefix="ARTHUR_PANEL_AVISOS", default_token_file="/run/secrets/panel_avisos")
    ollama = env.get("ARTHUR_OLLAMA_URL") or None
    # Sin Gemini: sus condiciones solo dejan usarlo en el EEE con plan de pago y Marc no paga (decisión 2026-10-17).
    # Workers AI se crea una sola vez: así recuerda si hoy ya se gastó la capa gratis y no vuelve a llamar.
    cloud = _workers_ai(env, log)

    def engines() -> list:
        return [*([cloud] if cloud is not None else []), notes_engines.LocalEngine(ollama)]

    return Runtime(
        host=env.get("ARTHUR_PANEL_HOST", "127.0.0.1"),
        port=int(env.get("ARTHUR_PANEL_PORT", str(DEFAULT_PORT))),
        # Las versiones nuevas de los ajustes delimitan qué notas se pueden leer y marcar.
        # El contrato anterior no tenía esa lista; así esta rama se puede probar antes de juntarla.
        vault=ObsidianVault(Path(env.get("ARTHUR_PANEL_VAULT") or settings.vault),
                            **({"files": settings.files} if hasattr(settings, "files") else {})),
        calendar=CalendarClient(auth) if connected else None,
        gmail=GmailClient(auth) if connected else None,
        studio_link=studio_link, notifier=notifier, engines=engines, cerebro=cerebro)


def make_briefing_fn(store, today_fn, engines, now: Callable[[], datetime] = _now,
                     extra_line=None) -> Callable[[], dict]:
    """Rehace el resumen de hoy y lo guarda SIN avisar (botón de la página). Conserva si ya se había avisado:
    si no, el reloj creería que falta el aviso y lo repetiría al reiniciar."""
    from .briefing import make_briefing, with_extra

    def redo() -> dict:
        day = now().date()
        text, engine = make_briefing(today_fn(), engines())
        text = with_extra(text, extra_line, day)
        previous = store.last_briefing(day)
        store.save_briefing(day, text, engine, bool(previous and previous["delivered"]))
        return {"date": day.isoformat(), "text": text, "engine": engine}

    return redo


def new_device(name: str, registry=None, out=print) -> int:
    from ..security.devices import DeviceError, DeviceRegistry

    try:
        key = (registry or DeviceRegistry(devices_file())).add(name)
    except DeviceError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    out(f"Llave de «{name}» para el panel de vida (se enseña SOLO esta vez; pégala al entrar):\n\n{key}\n")
    return 0


def connect_google(auth, ask: Callable[[str], str] = input) -> int:
    """Autoriza Google (solo lectura) pegando la dirección, como en el servidor: no hay navegador en el contenedor."""
    from ..integrations.google import GoogleError
    from .sources.google import PANEL_SCOPES

    def open_browser(url: str) -> None:
        print("\n1) Abre esta dirección en el navegador de tu ordenador o móvil y acepta:\n\n" + url + "\n")
        print("2) Al final, el navegador irá a una página de 127.0.0.1 que NO carga: es normal.\n"
              "   Copia la dirección completa de la barra y pégala aquí (lleva un código de un solo uso).\n")

    print("Permisos que se piden: SOLO LEER tu calendario y tu Gmail (no puede crear, enviar, borrar ni modificar nada).")
    try:
        auth.authorize(PANEL_SCOPES, open_browser, paste=ask)
    except GoogleError as e:
        print(f"⚠ {e}")
        return 1
    print("✔ Conectado. Reinicia el panel para que lo use.")
    return 0


def panel_command(args, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    if args.panel_clave_gemini:
        # Ya no pide ni guarda nada: el resumen no usa Gemini. Se deja la orden para que no falle a quien la recuerde.
        print("El resumen ya no usa Gemini: lo escribe Workers AI (la misma cuenta y token que el chat de la web) "
              "y, si no está o falla, Ollama. No hace falta ninguna clave.")
        return 0
    if args.panel_dispositivo_nuevo:
        return new_device(args.panel_dispositivo_nuevo)
    if args.panel_google:
        from ..integrations.google import GoogleAuth
        from .google_keyring import EncryptedFileKeyring
        from .store import PanelError

        keyring = EncryptedFileKeyring()
        try:
            keyring.ensure_ready()
        except PanelError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        return connect_google(GoogleAuth(keyring=keyring))
    if args.panel_web:
        return _serve(env)
    return 0


def _serve(env: Mapping[str, str]) -> int:
    from ..cerebro import relay
    from ..security.devices import DeviceRegistry
    from .briefing import send_briefing
    from .revision import Revision
    from .scheduler import BriefingClock, RevisionClock
    from .settings import load_settings
    from .store import PanelError, PanelStore
    from .today import build_today
    from .web import PanelWeb

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings()
        rt = build_runtime(env, settings)
    except PanelError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    now = lambda: datetime.now(settings.zone)  # el día del panel sigue la zona elegida en ajustes
    store = PanelStore(now=now)

    # La línea del Repaso enlaza a la pestaña de Apuntes (ARTHUR_APUNTES_PUBLIC_URL; sin ella, sin enlace)
    apuntes_url = env.get("ARTHUR_APUNTES_PUBLIC_URL", "").strip()
    # Con caché de un minuto: /api/today se pide en cada carga y un cerebro lento no debe retrasarlas todas
    repaso_fn = relay.RepasoLine(rt.cerebro, apuntes_url) if rt.cerebro is not None else None

    def today_fn() -> dict:
        # La revisión marca los bloques de estudio, apunta lo del Repaso y saca la foto del plan si ya es hora.
        return revision.decorate(build_today(store=store, vault=rt.vault, calendar=rt.calendar, gmail=rt.gmail,
                                             studio_link=rt.studio_link, now=now(), repaso_fn=repaso_fn))

    # La revisión nocturna y semanal (pieza 4): la misma cadena de motores y el mismo aviso que el resumen.
    revision = Revision(store, today_fn, now=now, engines=rt.engines, notifier=rt.notifier,
                        prefix=settings.study_prefix, photo_time=settings.photo_time)

    def run_briefing() -> None:
        send_briefing(store=store, today_fn=today_fn, engines=rt.engines(), notifier=rt.notifier, now=now(),
                      extra_line=revision.monday_line)

    clock = BriefingClock(settings, store, run_briefing, now=now)
    revision_clock = RevisionClock(revision, revision_time=settings.revision_time, photo_time=settings.photo_time,
                                   now=now)
    web = PanelWeb(store, DeviceRegistry(devices_file()), today_fn, rt.vault,
                   make_briefing_fn(store, today_fn, rt.engines, now, extra_line=revision.monday_line),
                   host=rt.host, port=rt.port, cerebro=rt.cerebro, revision=revision)
    clock.start()
    revision_clock.start()
    print(f"Panel de vida en {web.url} (Ctrl+C para parar)", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        clock.stop()
        revision_clock.stop()
        web.shutdown()
        store.close()
    return 0
