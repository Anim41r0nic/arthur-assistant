"""ARTHUR en el servidor (arthur-core) y el cliente de texto para hablarle desde otro aparato.

Servidor (`arthur --core`, lo arranca el contenedor Docker):
  el mismo Assistant de siempre, sin escritorio ni voz todavía, con las herramientas de servidor
  (memoria, recordatorios, calendario, correo) y la puerta de la API (api/core_api.py) para los aparatos.

Cliente (`arthur --cliente`): chat de texto contra ese servidor, y además las «manos» de este aparato (fase S3):
le anuncia al servidor lo que sabe hacer aquí (apps, ventanas, volumen, música…) y ejecuta lo que le pida, tras
volver a validarlo con SUS reglas (doble control). `arthur --emparejar URL` guarda la dirección y
la llave de este aparato (la da `arthur --dispositivo-nuevo NOMBRE` en el servidor) en un fichero 600.
Diseño completo: docs/servidor.md.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import signal
import sys
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ..app import App

log = logging.getLogger(__name__)

ASK_TIMEOUT = 600  # pensar, cargar el modelo y esperar una confirmación puede llevar minutos


# --- servidor ---------------------------------------------------------------

def run_core(app: App, *, devices, host: str, port: int, confirmer=None, voice=None, push=None,
             stop: threading.Event | None = None) -> int:
    """`confirmer`: el RoutingConfirmer del router; se le conecta el broker para preguntar en los aparatos.
    `voice`: el enlace con arthur-voz (integrations/voz.py) o None (servidor sin voz).
    `push`: los avisos al móvil (integrations/push.py, PushNotifier) o None (fase S6)."""
    from ..api.core_api import CoreServer
    from ..core.events import Event
    from ..security.broker import ConfirmationBroker

    from ..core.remote_tools import RemoteToolHub

    from ..app import announce_when_free

    from ..push.destination import check_notice_url

    panel_public_url = os.environ.get("ARTHUR_PANEL_PUBLIC_URL", "").strip()
    if panel_public_url:
        panel_public_url = check_notice_url(panel_public_url)
    # La página de clientes: el aviso de un contacto de la web abre su ficha (la raíz + «#c12», la pone el núcleo).
    clientes_public_url = os.environ.get("ARTHUR_CLIENTES_PUBLIC_URL", "").strip()
    if clientes_public_url:
        clientes_public_url = check_notice_url(clientes_public_url)
    # La página de Studio: el aviso de un vídeo lo abre en su canal (la raíz + «#v12-frutinovelas», la pone el núcleo).
    studio_public_url = os.environ.get("ARTHUR_STUDIO_PUBLIC_URL", "").strip()
    if studio_public_url:
        studio_public_url = check_notice_url(studio_public_url)
    # La página de Apuntes: el aviso diario del Repaso la abre en esa pestaña (la raíz + «#repaso», la pone el núcleo).
    # La variable ya existía para las herramientas de Apuntes: si no es una raíz válida, el núcleo arranca igual y
    # contesta 503 al destino «apuntes», como si faltara (mejor que dejar a ARTHUR parado por esto).
    apuntes_public_url = os.environ.get("ARTHUR_APUNTES_PUBLIC_URL", "").strip()
    if apuntes_public_url:
        try:
            apuntes_public_url = check_notice_url(apuntes_public_url)
        except ValueError as e:
            log.warning("Núcleo: el aviso del Repaso irá sin enlace (ARTHUR_APUNTES_PUBLIC_URL: %s)", e)
            apuntes_public_url = ""

    def deliver(text: str, *, target_url: str | None = None) -> None:
        """Un aviso para Marc: la web abierta lo enseña al momento, el móvil lo recibe aunque esté cerrada y el
        portátil lo dice en voz alta cuando ARTHUR quede libre. Lo usan los recordatorios y Creator Studio."""
        app.bus.publish(Event("reminder_due", {"text": text}))
        if push is not None:
            push.notify_later(app.config.assistant.name, text, url=target_url)
        announce_when_free(app.assistant, text)

    broker = ConfirmationBroker(app.bus)
    hub = RemoteToolHub(app.router.registry)
    server = CoreServer(bus=app.bus, devices=devices, broker=broker, hub=hub, voice=voice, host=host, port=port,
                        handle_text=lambda text, images=(), on_speech=None: app.assistant.handle_text(
                            text, speak=False, images=images, on_speech=on_speech),
                        assistant_name=app.config.assistant.name, get_state=lambda: app.assistant.state,
                        vision=app.config.brain.vision, get_history=app.assistant.history, push=push,
                        on_notice=deliver, panel_public_url=panel_public_url or None,
                        clientes_public_url=clientes_public_url or None,
                        studio_public_url=studio_public_url or None,
                        apuntes_public_url=apuntes_public_url or None)
    # Sin aparatos conectados a los eventos no hay a quién preguntar: lo que pida confirmación se rechaza.
    broker.has_clients = lambda: server.stream_count() > 0
    if confirmer is not None:
        confirmer.broker = broker

    reminders = None
    if app.memory is not None:
        from ..core.reminders import ReminderScheduler

        reminders = ReminderScheduler(app.memory, deliver)
        reminders.check()
        reminders.start()

    server.start()
    if hasattr(app.brain, "warm_up"):
        app.brain.warm_up()
        hub.on_change = app.brain.warm_up  # con herramientas nuevas, lo precargado ya no sirve
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())  # docker stop / Ctrl+C
    log.info("ARTHUR en marcha en modo servidor (%d aparatos dados de alta)", len(devices.names()))
    try:
        stop.wait()
    finally:
        if reminders:
            reminders.stop()
        server.stop()
    return 0


# --- cliente ----------------------------------------------------------------

class RemoteError(Exception):
    pass


def client_file() -> Path:
    from ..core.config import default_config_path

    return default_config_path().parent / "cliente.toml"


def check_url(url: str) -> str:
    """Acepta https a cualquier sitio, o http solo dentro de casa o de la red privada (Tailscale): la llave del
    aparato nunca debe viajar sin cifrar por internet."""
    parts = urllib.parse.urlsplit(url.strip().rstrip("/"))
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/"):
        raise RemoteError("La dirección debe ser como http://100.x.y.z:8770 o https://arthur.tudominio.com")
    if parts.scheme == "http":
        host = parts.hostname
        private = host.endswith(".ts.net") or host == "localhost"
        try:
            ip = ipaddress.ip_address(host)
            private = ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")
        except ValueError:
            pass
        if not private:
            raise RemoteError("Sin https solo se admite una dirección de casa o de Tailscale (la llave iría sin cifrar)")
    return f"{parts.scheme}://{parts.netloc}"


def web_file() -> Path:
    from ..core.config import default_config_path

    return default_config_path().parent / "web.txt"


def save_web_url(url: str, path: Path | None = None) -> str:
    """Guarda la dirección de la web de ARTHUR (fase S5) para que `arthur --web` la abra sin escribirla.
    No es secreta (sin la llave del navegador no se entra), pero se comprueba como la del servidor."""
    url = check_url(url)
    path = path or web_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(url + "\n", encoding="utf-8")
    return url


def load_web_url(path: Path | None = None) -> str | None:
    try:
        return check_url((path or web_file()).read_text(encoding="utf-8").strip())
    except (OSError, RemoteError):
        return None


def _toml_string(value: str) -> str:
    return json.dumps(value)  # una cadena JSON es una cadena TOML válida (comillas y escapes iguales)


def save_client(url: str, token: str, path: Path | None = None) -> Path:
    path = path or client_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# ARTHUR: a qué servidor se conecta este aparato. La llave es secreta: no la compartas.\n")
        f.write(f"url = {_toml_string(url)}\ntoken = {_toml_string(token)}\n")
    os.replace(tmp, path)
    return path


def load_client(path: Path | None = None) -> tuple[str, str]:
    path = path or client_file()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RemoteError("Este aparato aún no está emparejado: arthur --emparejar http://SERVIDOR:8770") from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RemoteError(f"No puedo leer {path}: {e}") from e
    url, token = raw.get("url"), raw.get("token")
    if not isinstance(url, str) or not isinstance(token, str) or not token:
        raise RemoteError(f"{path} está incompleto: vuelve a emparejar el aparato")
    return check_url(url), token


class RemoteCore:
    """Habla con la API del servidor (api/core_api.py) con la llave de este aparato."""

    def __init__(self, url: str, token: str, *, opener=None) -> None:
        self.url = check_url(url)
        self._token = token
        # Sin proxies del sistema: la conversación va directa al servidor, como con Ollama.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, body: dict | bytes | None = None, timeout: float = 15,
                 content_type: str = "application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:  # el motivo que da el servidor (p. ej. «la voz no está activada»), nunca datos tuyos
                detail = json.loads(e.read()).get("error", "")
            except (ValueError, AttributeError, OSError):
                detail = ""
            e.close()
            if e.code == 401:
                raise RemoteError("El servidor no acepta la llave de este aparato (¿revocada?). Vuelve a emparejarlo.") from None
            raise RemoteError(f"El servidor respondió {e.code}" + (f": {detail}" if detail else "")) from None
        except (urllib.error.URLError, OSError) as e:
            raise RemoteError(f"No puedo conectar con el servidor de ARTHUR ({self.url}): {getattr(e, 'reason', e)}") from None

    def whoami(self) -> dict:
        with self._request("GET", "/api/v1/whoami") as r:
            return json.loads(r.read())

    def ask(self, text: str) -> str:
        with self._request("POST", "/api/v1/message", {"text": text}, timeout=ASK_TIMEOUT) as r:
            return json.loads(r.read())["reply"]

    def voice(self, wav: bytes) -> dict:
        """Lo que has dicho (WAV 16 kHz mono) → {"heard": texto, "reply": respuesta o None si no se te oyó}."""
        with self._request("POST", "/api/v1/voice", wav, timeout=ASK_TIMEOUT, content_type="audio/wav") as r:
            return json.loads(r.read())

    def voice_stream(self, wav: bytes):
        """Como voice(), pero va dando cada línea según llega (voz a trozos, 0.56.0): {"heard"}, luego
        {"say", "audio" (WAV en base64)} por frase y al final {"reply"}. Cerrar el generador corta la conexión."""
        with self._request("POST", "/api/v1/voice/stream", wav, timeout=ASK_TIMEOUT, content_type="audio/wav") as r:
            for raw in r:
                if raw.strip():
                    try:
                        yield json.loads(raw)
                    except ValueError:
                        continue

    def speech(self, text: str) -> bytes:
        """La voz de ARTHUR para ese texto, en WAV, para reproducirla en este aparato."""
        with self._request("POST", "/api/v1/speech", {"text": text}, timeout=60) as r:
            return r.read()

    def confirm(self, pending_id: str, answer: bool) -> bool:
        try:
            with self._request("POST", "/api/v1/confirm", {"id": pending_id, "answer": answer}) as r:
                return bool(json.loads(r.read()).get("ok"))
        except RemoteError:
            return False  # caducada (409) o el servidor ya no está: cuenta como no contestada

    def register(self, specs: list[dict]) -> dict:
        with self._request("POST", "/api/v1/capabilities", {"tools": specs}) as r:
            return json.loads(r.read())

    def tool_result(self, call_id: str, status: str, output: str) -> bool:
        try:
            with self._request("POST", "/api/v1/tool_result", {"id": call_id, "status": status,
                                                                "output": output[:8000]}) as r:
                return bool(json.loads(r.read()).get("ok"))
        except RemoteError:
            return False

    def events(self):
        """Va dando los eventos del servidor (dict) mientras la conexión siga abierta."""
        with self._request("GET", "/api/v1/events", timeout=60) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except ValueError:
                        continue


# Herramientas cuyo riesgo depende de los argumentos: se anuncia el MÁS alto y en qué caso baja (lower_if), para
# que el servidor pida la misma confirmación que pediría el portátil (core/remote_tools.py).
VARIABLE_RISK = {
    "recent_messages": ("private", {"param": "include_text", "values": [False], "missing": True, "risk": "read"}),
    "set_gestures": ("private", {"param": "enabled", "values": [False], "risk": "low"}),
}


def capability_specs(registry, trusted_apps=()) -> list[dict]:
    """Lo que este aparato le anuncia al servidor. Lo PELIGROSO nunca se anuncia (y el servidor tampoco lo
    aceptaría). Abrir una app de confianza no pide confirmación, igual que en el portátil."""
    from ..tools.base import Risk

    specs = []
    for tool in registry.all():
        if tool.risk is Risk.DANGEROUS:
            continue
        spec = {**tool.json_schema(), "risk": str(tool.risk)}
        if tool.name in VARIABLE_RISK:
            spec["risk"], spec["lower_if"] = VARIABLE_RISK[tool.name]
        if tool.name == "open_application" and trusted_apps:
            spec["low_if"] = {"param": "name", "values": list(trusted_apps)}
        specs.append(spec)
    return specs


class LocalExecutor:
    """Ejecuta aquí lo que pide el servidor, con las reglas de AQUÍ: herramienta conocida, argumentos válidos,
    su comprobación propia (rutas, app instalada, ventana real…) y el modo seguro local. La confirmación ya se
    hizo en el servidor (y te salió en este aparato). Todo queda en la auditoría local."""

    def __init__(self, registry, policy, audit) -> None:
        self.registry = registry
        self.policy = policy
        self.audit = audit

    def run(self, name: object, arguments: object) -> tuple[str, str]:
        from ..security.policy import Decision
        from ..tools.base import Risk, ToolArgumentError, ToolDenied, ToolError

        tool = self.registry.get(name) if isinstance(name, str) else None
        if tool is None or tool.risk is Risk.DANGEROUS:
            return self._done(str(name)[:64], {}, "denied", "Este aparato no tiene esa herramienta.")
        if not isinstance(arguments, dict):
            return self._done(tool.name, {}, "error", "Argumentos no válidos.")
        try:
            args = tool.validate(arguments)
            if self.policy.decide(tool.effective_risk(args)).decision is Decision.DENY:
                return self._done(tool.name, args, "denied", "El modo seguro de este aparato no lo permite.")
            if tool.check:
                tool.check(args)
            return self._done(tool.name, args, "ok", tool.run(args))
        except ToolDenied as e:
            return self._done(tool.name, arguments, "denied", str(e))
        except (ToolArgumentError, ToolError) as e:
            return self._done(tool.name, arguments, "error", str(e))
        except Exception:
            log.exception("Fallo ejecutando %s a petición del servidor", tool.name)
            return self._done(tool.name, arguments, "error", "Error interno en este aparato.")

    def _done(self, name: str, arguments: dict, status: str, output: str) -> tuple[str, str]:
        self.audit.record(tool=name, arguments=arguments, decision="servidor", status=status)
        return status, output


def pair(url: str, *, ask_secret: Callable[[str], str], say: Callable[[str], None], path: Path | None = None) -> int:
    try:
        url = check_url(url)
    except RemoteError as e:
        say(f"✘ {e}")
        return 2
    token = ask_secret("Pega la llave del aparato (no se verá al escribir): ").strip()
    if not token:
        say("✘ Sin llave no se puede emparejar.")
        return 2
    try:
        info = RemoteCore(url, token).whoami()
    except RemoteError as e:
        say(f"✘ {e}")
        return 1
    where = save_client(url, token, path)
    say(f"✔ Emparejado con {info.get('name', 'ARTHUR')} como «{info.get('device')}». Guardado en {where} (600).")
    say("  Ya puedes hablarle: arthur --cliente")
    return 0


class CoreEventLink:
    """Lo que el servidor le cuenta a este aparato (GET /api/v1/events), con reconexión: le ofrece sus «manos»
    al conectar, ejecuta las herramientas que pide, pregunta las confirmaciones y da los avisos. Lo usan el chat
    de la terminal y el servicio del portátil en modo cliente (interfaces/voz_cliente.py)."""

    RETRY_SECONDS = 5

    def __init__(self, core: RemoteCore, *, executor: LocalExecutor | None, specs: list[dict] | None,
                 ask_confirm: Callable[[str], bool], announce: Callable[[str], None],
                 say: Callable[[str], None], on_event: Callable[[dict], None] | None = None,
                 confirm_details: Callable[[dict], bool] | None = None) -> None:
        """`on_event`: ve todos los eventos (el Orb del aparato enseña las herramientas que usa el servidor);
        `confirm_details`: si está, pregunta con todo lo que manda el servidor (riesgo incluido) en vez de
        `ask_confirm(resumen)`."""
        self.on_event = on_event
        self.confirm_details = confirm_details
        self.core = core
        self.executor = executor
        self.specs = specs
        self.ask_confirm = ask_confirm
        self.announce = announce
        self.say = say
        self.stopping = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="core-events", daemon=True).start()

    def stop(self) -> None:
        self.stopping.set()

    def _loop(self) -> None:
        while not self.stopping.is_set():
            try:
                for event in self.core.events():
                    if self.stopping.is_set():
                        return
                    self.handle(event)
            except RemoteError:
                pass
            except Exception:
                log.debug("La escucha de eventos se cortó", exc_info=True)
            self.stopping.wait(self.RETRY_SECONDS)  # se cortó: reintentar en unos segundos

    def handle(self, event: dict) -> None:
        kind, data = event.get("type"), event.get("data") or {}
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                log.exception("Fallo enseñando un evento del servidor")
        if kind == "hello" and self.executor is not None and self.specs:
            # Cada vez que (re)conectamos: el servidor olvida las manos de un aparato que se va.
            try:
                added = self.core.register(self.specs).get("registered", [])
                log.info("Este aparato aporta %d herramientas al servidor", len(added))
            except RemoteError as e:
                self.say(f"  ⚠ No he podido ofrecer las herramientas de este aparato: {e}")
        elif kind == "tool_call" and self.executor is not None:
            def work(d=data):
                status, out = self.executor.run(d.get("name"), d.get("arguments"))
                self.core.tool_result(str(d.get("id", "")), status, out)
            threading.Thread(target=work, name="tool-call", daemon=True).start()
        elif kind == "confirmation_pending":
            def ask(d=data):
                answer = (self.confirm_details(d) if self.confirm_details is not None
                          else self.ask_confirm(str(d.get("summary", ""))))
                ok = self.core.confirm(str(d.get("id", "")), bool(answer))
                self.say("  ✔ Respuesta enviada" if ok else "  ✘ La pregunta ya había caducado")
            # En su propio hilo: mientras esperas a contestar, siguen llegando eventos (p. ej. la herramienta).
            threading.Thread(target=ask, name="confirm", daemon=True).start()
        elif kind == "assistant_message" and data.get("announcement") and data.get("text"):
            self.announce(str(data["text"]))


def run_remote_client(core: RemoteCore, *, input_fn: Callable[[str], str] = input, output=sys.stdout,
                      listen_events: bool = True, executor: LocalExecutor | None = None,
                      specs: list[dict] | None = None, voice=None) -> int:
    """Chat con el ARTHUR del servidor. Las confirmaciones y los avisos llegan por los eventos.
    `voice` (RemoteVoice, interfaces/voz_cliente.py): Enter con la línea vacía para hablarle (fase S4)."""
    def say(text: str) -> None:
        print(text, file=output, flush=True)

    try:
        info = core.whoami()
    except RemoteError as e:
        say(f"✘ {e}")
        return 1
    say(f"Conectado a {info.get('name', 'ARTHUR')} en {core.url} como «{info.get('device')}». /salir para terminar.")
    if voice is not None:
        say("  🎙 Voz activada: pulsa Enter con la línea vacía para hablar (oye y responde el servidor)")

    def ask_confirm(summary: str) -> bool:
        # La petición está esperando en el hilo principal (bloqueado en la red, no en el teclado):
        # aquí se puede preguntar sin pisarse.
        answer = input_fn(f"\n  ⚠ ¿Confirmas? {summary} [s/N] ").strip().lower()
        return answer in ("s", "si", "sí", "y", "yes")

    def announce(text: str) -> None:
        say(f"\n🔔 {text}")
        if voice is not None:
            voice.speak_in_background(text)  # el recordatorio, también en voz alta

    if listen_events:
        CoreEventLink(core, executor=executor, specs=specs, ask_confirm=ask_confirm, announce=announce,
                      say=say).start()
    while True:
        try:
            line = input_fn("tú › ").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 0
        if not line:
            if voice is not None:
                voice.push_to_talk(input_fn)
            continue
        if line in ("/salir", "/exit", "/quit"):
            if voice is not None:
                voice.player.stop()
            return 0
        say("  … pensando")
        try:
            say(f"arthur › {core.ask(line)}")
        except RemoteError as e:
            say(f"  ⚠ {e}")
