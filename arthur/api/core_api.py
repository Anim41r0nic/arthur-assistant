"""La puerta del ARTHUR del servidor (arthur-core): por aquí le hablan los aparatos (CachyOS, Windows, iPhone…).

API (JSON, siempre con `Authorization: Bearer <llave del aparato>` salvo /health):
  GET  /api/v1/health   → {"ok": true}            (sin llave: solo dice que está vivo)
  POST /api/v1/message  {"text": "…"}             → {"reply": "…"}
                        {"text": "…", "image": "<JPEG/PNG/WebP en base64>"}  (fase S5: si el cerebro ve fotos)
  GET  /api/v1/whoami   → {"device": "cachyos"}   (para comprobar la llave al emparejar)
  GET  /api/v1/events   eventos en directo (SSE): estados, respuestas, confirmaciones pendientes…
  GET  /api/v1/history  → {"messages": [{"role": "user"|"assistant", "text", "photo"?}]}  la conversación (S5)
  POST /api/v1/confirm  {"id": "…", "answer": true|false}
  POST /api/v1/notice   {"text": "…", "destination": "panel"?}  aviso de Studio o el panel; solo el panel puede
                        pedir su destino fijo y el núcleo lo traduce a ARTHUR_PANEL_PUBLIC_URL
                        {"text": "…", "destination": "clientes", "client": 12}  solo el chat de la web: abre la ficha
                        del cliente (ARTHUR_CLIENTES_PUBLIC_URL + "#c12")
                        {"text": "…", "destination": "studio", "review": 12, "profile": "frutinovelas"}  solo Studio:
                        abre ese vídeo en su canal (ARTHUR_STUDIO_PUBLIC_URL + "#v12-frutinovelas")
                        {"text": "…", "destination": "apuntes"}  solo el cerebro: el aviso diario del Repaso abre
                        Apuntes en esa pestaña (ARTHUR_APUNTES_PUBLIC_URL + "#repaso")
  POST /api/v1/capabilities {"tools": [...]}       lo que este aparato sabe hacer (fase S3, core/remote_tools.py)
  POST /api/v1/tool_result  {"id", "status", "output"}   resultado de una herramienta que el servidor le pidió
  POST /api/v1/voice    cuerpo audio/wav (16 kHz, mono) → {"heard": "lo que has dicho", "reply": "…" | null}
  POST /api/v1/speech   {"text": "…"}                   → audio/wav     (fase S4, integrations/voz.py)
  POST /api/v1/voice/stream  cuerpo audio/wav → una línea JSON por evento, según van saliendo (voz a trozos, 0.56.0):
                        {"heard": "…"}, luego {"say": "frase", "audio": "<WAV en base64>"} por cada frase en cuanto
                        tiene voz (ARTHUR empieza a hablar sin esperar a la respuesta entera) y al final {"reply": "…"}
  GET  /api/v1/push/key         → {"key": "B…"}   la llave pública para suscribirse a los avisos (fase S6)
  POST /api/v1/push/subscribe   {"subscription": {...}}  avisos al móvil para este aparato (integrations/push.py)
  POST /api/v1/push/unsubscribe                          ya no quiere avisos

La web de chat (fase S5, ficheros en api/web/): GET / la sirve sin llave (no lleva nada secreto). Para entrar,
pegas UNA vez la llave de un aparato dado de alta para ese navegador (`--dispositivo-nuevo movil`):
  POST /api/v1/login  {"key": "arthur_…"} → la guarda en una cookie HttpOnly (JavaScript no puede leerla)
  POST /api/v1/logout                      → la borra
Con la cookie, la web usa las mismas rutas de arriba. Una petición que cambia algo (POST) con cookie exige además
la cabecera X-Arthur (otra web no puede ponerla sin un permiso CORS que nunca damos) y, si llega Origin, que sea
este mismo servidor: así ninguna página ajena puede usar tu sesión.

Seguridad:
  - Cada aparato tiene su llave (security/devices.py); en el servidor solo hay huellas. Revocable por aparato.
  - Pensado para escuchar en una red privada o detrás de una puerta de acceso segura: NUNCA abierto a internet
    sin otra puerta delante. Va sin TLS propio porque esa capa (WireGuard / Cloudflare) ya cifra.
  - Cuerpos pequeños (16 KB; el audio, hasta 60 s), solo JSON o WAV, tiempo límite para enviar la petición, número de conexiones limitado.
  - Una llave mala espera un poco antes de contestar (frena a quien intente adivinarlas) y queda en el registro.
  - El registro dice qué aparato pidió qué ruta y cómo acabó; nunca lo que escribiste.
"""

from __future__ import annotations

import base64
import binascii
import http.cookies
import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core.events import ALL, Event, EventBus

log = logging.getLogger(__name__)

FORWARDED_EVENTS = {
    "state_changed", "user_message", "assistant_message", "error",
    "tool_started", "tool_finished", "confirmation_pending", "confirmation_resolved", "reminder_due",
}
MAX_BODY = 16 * 1024
# Los únicos aparatos que pueden mandar avisos (POST /api/v1/notice). «webchat» es el asistente de la web: avisa de un
# contacto con texto plano y, como mucho, el número del cliente; el destino «panel» lo sigue reservando
# notice_destination solo al panel, el de «clientes» solo al chat y el de «studio» solo a Studio. «cerebro» manda el
# aviso diario del Repaso, y solo él puede pedir el destino «apuntes».
NOTICE_DEVICES = ("studio", "panel", "webchat", "cerebro")
MAX_CLIENT = 999_999_999   # el mismo tope que el «#c<número>» que aceptan push y el navegador (9 cifras)


def notice_destination(device: str, body: dict, panel_url: str | None,
                       clientes_url: str | None = None, studio_url: str | None = None,
                       apuntes_url: str | None = None) -> str | None:
    """Un aparato solo solicita un destino FIJO («panel», «clientes» + número, «studio» + vídeo y canal o «apuntes»);
    la dirección real la pone el núcleo. Así un aparato con la llave robada no puede hacer que el aviso abra una web cualquiera."""
    requested = body.get("destination")
    client = body.get("client")
    if requested != "studio" and ("review" in body or "profile" in body):
        raise ValueError("«review» y «profile» solo valen con el destino «studio»")
    if requested is None:
        if client is not None:
            raise ValueError("«client» solo vale con el destino «clientes»")
        return None
    if requested == "studio":
        return _studio_destination(device, body, studio_url)
    if requested == "panel":
        if client is not None:
            raise ValueError("«client» solo vale con el destino «clientes»")
        if device != "panel":
            raise PermissionError("solo el panel puede pedir abrir el panel")
        if panel_url is None:
            raise LookupError("falta ARTHUR_PANEL_PUBLIC_URL en el núcleo")
        return panel_url
    if requested == "clientes":
        if device != "webchat":
            raise PermissionError("solo el chat de la web puede pedir abrir una ficha de cliente")
        # `bool` es un `int` en Python: True no puede colarse como cliente 1.
        if isinstance(client, bool) or not isinstance(client, int) or not 1 <= client <= MAX_CLIENT:
            raise ValueError("«client» debe ser el número del cliente")
        if clientes_url is None:
            raise LookupError("falta ARTHUR_CLIENTES_PUBLIC_URL en el núcleo")
        return f"{clientes_url}#c{client}"
    if requested == "apuntes":
        # El Repaso: siempre la misma pestaña, sin números que el aparato pueda elegir.
        if client is not None:
            raise ValueError("«client» solo vale con el destino «clientes»")
        if device != "cerebro":
            raise PermissionError("solo el cerebro puede pedir abrir el Repaso de Apuntes")
        if apuntes_url is None:
            raise LookupError("falta ARTHUR_APUNTES_PUBLIC_URL en el núcleo")
        return f"{apuntes_url}#repaso"
    raise ValueError("destino de aviso desconocido")


def _studio_destination(device: str, body: dict, studio_url: str | None) -> str:
    """El vídeo de un aviso de Studio, en su canal: la página lo abre ya en ese canal («#v12-frutinovelas»). El perfil
    se comprueba contra la lista fija (push/destination.py), nunca se copia sin mirar."""
    from ..push.destination import STUDIO_PROFILES

    if device != "studio":
        raise PermissionError("solo Studio puede pedir abrir un vídeo de Studio")
    if body.get("client") is not None:
        raise ValueError("«client» solo vale con el destino «clientes»")
    review, profile = body.get("review"), body.get("profile")
    if isinstance(review, bool) or not isinstance(review, int) or not 1 <= review <= MAX_CLIENT:
        raise ValueError("«review» debe ser el número del vídeo")
    if profile not in STUDIO_PROFILES:
        raise ValueError("«profile» debe ser un canal de Studio")
    if studio_url is None:
        raise LookupError("falta ARTHUR_STUDIO_PUBLIC_URL en el núcleo")
    return f"{studio_url}#v{review}-{profile}"
MAX_NOTICE = 300
MAX_AUDIO = 60 * 16_000 * 2 + 1024   # 60 s de audio a 16 kHz, 16 bits, mono (+ la cabecera WAV)
MAX_TEXT = 4_000
MAX_IMAGE = 4 * 1024 * 1024               # una foto, ya decodificada (la web la reduce antes a ~300 KB)
MAX_IMAGE_BODY = MAX_IMAGE * 4 // 3 + MAX_BODY   # el JSON con la foto en base64 (ocupa un tercio más)
# Los primeros bytes de cada formato: así se comprueba que es una imagen de verdad y no otra cosa disfrazada.
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")   # JPEG, PNG (WebP aparte: RIFF....WEBP)
MAX_STREAMS = 16
HEARTBEAT_SECONDS = 15
REQUEST_TIMEOUT = 15   # para ENVIAR la petición; esperar la respuesta de ARTHUR puede durar más
BAD_KEY_DELAY = 0.5

# La web de chat: solo estos ficheros, con nombre fijo (nada de rutas que vengan de la petición).
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/chat.js": ("chat.js", "text/javascript; charset=utf-8"),
    "/chat.css": ("chat.css", "text/css; charset=utf-8"),
    "/mic.js": ("mic.js", "text/javascript; charset=utf-8"),   # el micrófono (AudioWorklet), fase S5 paso 2
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),     # el que enseña los avisos con la web cerrada (S6)
    # Para instalarla como app (pantalla de inicio del iPhone, Chrome/Edge en Windows), fase S5 paso 5.
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-180.png": ("icon-180.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
# La página solo puede cargar y hablar con este mismo servidor; nadie puede meterla en un marco.
WEB_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
                                "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # El micrófono solo para esta página (nunca para algo incrustado); cámara y ubicación, para nadie.
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Cache-Control": "no-cache",
}
COOKIE = "arthur_llave"
COOKIE_DAYS = 180   # como en ChatGPT: entras una vez por navegador; si lo pierdes, se revoca su llave


class CoreServer:
    clientes_public_url: str | None = None   # sin página de clientes configurada (lo cambia __init__)
    studio_public_url: str | None = None     # sin página de Studio configurada (lo cambia __init__)
    apuntes_public_url: str | None = None    # sin página de Apuntes configurada (lo cambia __init__)

    def __init__(self, *, bus: EventBus, devices, handle_text, broker=None, hub=None, voice=None,
                 host: str = "127.0.0.1", port: int = 8770, assistant_name: str = "ARTHUR",
                 get_state=lambda: "idle", vision: bool = False, get_history=lambda: [], push=None,
                 on_notice=None, panel_public_url: str | None = None,
                 clientes_public_url: str | None = None, studio_public_url: str | None = None,
                 apuntes_public_url: str | None = None) -> None:
        """`handle_text(texto) -> respuesta`: lo hace Assistant.handle_text. `broker`: las confirmaciones.
        `hub`: la centralita de herramientas remotas (RemoteToolHub) o None.
        `voice`: el enlace con arthur-voz (integrations/voz.py, VoiceLink) o None si este servidor no tiene voz.
        `push`: los avisos al móvil (integrations/push.py, PushNotifier) o None si no hay arthur-push.
        `on_notice(texto, *, target_url=None)`: entrega un aviso; el destino del panel sale de la configuración
        del núcleo, nunca de la petición. Puede ser None si no se aceptan avisos.
        `clientes_public_url`: la raíz de la página de clientes (ARTHUR_CLIENTES_PUBLIC_URL), a la que el aviso de un
        contacto añade la ficha («#c12»); validada como la del panel.
        `studio_public_url`: la raíz de la página de Studio (ARTHUR_STUDIO_PUBLIC_URL), a la que el aviso de un vídeo
        añade el vídeo y su canal («#v12-frutinovelas»); validada como la del panel.
        `apuntes_public_url`: la raíz de Apuntes (ARTHUR_APUNTES_PUBLIC_URL), a la que el aviso del Repaso añade
        «#repaso»; validada como la del panel."""
        self.bus = bus
        self.push = push
        self.on_notice = on_notice
        if panel_public_url is not None:
            from ..push.destination import check_notice_url

            panel_public_url = check_notice_url(panel_public_url)
        self.panel_public_url = panel_public_url
        if clientes_public_url is not None:
            from ..push.destination import check_notice_url

            clientes_public_url = check_notice_url(clientes_public_url)
        self.clientes_public_url = clientes_public_url
        if studio_public_url is not None:
            from ..push.destination import check_notice_url

            studio_public_url = check_notice_url(studio_public_url)
        self.studio_public_url = studio_public_url
        if apuntes_public_url is not None:
            from ..push.destination import check_notice_url

            apuntes_public_url = check_notice_url(apuntes_public_url)
        self.apuntes_public_url = apuntes_public_url
        self.voice = voice
        self.get_history = get_history  # la conversación que recuerda ARTHUR (Assistant.history)
        self.vision = vision  # el cerebro ve fotos: /message acepta «image» (handle_text(texto, images=…))
        self.devices = devices
        self.handle_text = handle_text
        self.broker = broker
        self.hub = hub
        if hub is not None:
            hub.send = self.send_to
        self._name = assistant_name
        self._get_state = get_state
        self._streams: dict[queue.Queue, str] = {}  # cada conexión de eventos → de qué aparato es
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._unsubscribe = bus.subscribe(ALL, self._on_event)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="core-api", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("API de aparatos en el puerto %d", self.port)

    def stop(self) -> None:
        self._stopping.set()
        self._unsubscribe()
        with self._lock:
            for q in self._streams:
                try:
                    q.put_nowait(None)  # despierta cada conexión de eventos para que se cierre YA
                except queue.Full:
                    pass
        self._httpd.shutdown()
        self._httpd.server_close()

    def stream_count(self) -> int:
        with self._lock:
            return len(self._streams)

    def send_to(self, device: str, payload: dict) -> bool:
        """Mensaje solo para un aparato (p. ej. «ejecuta esta herramienta»). False si no está conectado."""
        message = json.dumps(payload, ensure_ascii=False, default=str)
        sent = False
        with self._lock:
            for q, owner in self._streams.items():
                if owner == device:
                    try:
                        q.put_nowait(message)
                        sent = True
                    except queue.Full:
                        pass
        return sent

    def _on_event(self, event: Event) -> None:
        if event.type not in FORWARDED_EVENTS:
            return
        message = json.dumps({"type": event.type, "data": event.data, "ts": event.timestamp},
                             ensure_ascii=False, default=str)
        with self._lock:
            for q in self._streams:
                try:
                    q.put_nowait(message)
                except queue.Full:
                    pass  # un aparato que no lee no frena a los demás

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT
            server_version = "arthur-core"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # sin el registro por defecto (incluye la ruta completa); se anota a mano y sin contenido

            # --- autenticación ---
            def _device(self, changes: bool = False) -> str | None:
                """De qué aparato es la petición: llave en `Authorization` (programas) o cookie (la web).
                `changes`: la petición hace algo (POST); con cookie se exige además que venga de la propia web."""
                header = self.headers.get("Authorization", "")
                token = header[7:].strip() if header.startswith("Bearer ") else None
                if token is None:
                    token = self._cookie_key()
                    if token is not None and changes and not self._from_our_page():
                        log.warning("core-api: petición con cookie desde fuera de la web (%s)", self.path[:40])
                        return None
                    if token is None:
                        return None  # sin llave ninguna (p. ej. la web antes de entrar): ni espera ni aviso
                name = server.devices.check(token)
                if name is None:
                    log.warning("core-api: llave no válida desde %s (%s)", self.client_address[0], self.path[:40])
                    time.sleep(BAD_KEY_DELAY)
                return name

            def _cookie_key(self) -> str | None:
                try:
                    jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                except http.cookies.CookieError:
                    return None
                return jar[COOKIE].value if COOKIE in jar else None

            def _from_our_page(self) -> bool:
                # X-Arthur: una web ajena solo podría añadirla pidiendo permiso CORS (OPTIONS), que no damos.
                # Origin: si el navegador lo manda, tiene que ser este mismo servidor (mismo Host).
                if self.headers.get("X-Arthur") != "1":
                    return False
                origin = self.headers.get("Origin")
                host = self.headers.get("Host", "")
                return origin is None or origin in (f"http://{host}", f"https://{host}")

            def _raw_body(self, content_type: str, limit: int) -> bytes | None:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != content_type:
                    self._reply(415, {"error": f"se espera {content_type}"})
                    return None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= limit:
                    self._reply(413, {"error": "cuerpo vacío o demasiado grande"})
                    return None
                return self.rfile.read(length)

            def _json_body(self, limit: int = MAX_BODY) -> dict | None:
                raw = self._raw_body("application/json", limit)
                if raw is None:
                    return None
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict):
                    self._reply(400, {"error": "JSON no válido"})
                    return None
                return body

            # --- rutas ---
            def do_GET(self):
                if self.path == "/api/v1/health":
                    return self._reply(200, {"ok": True})
                if self.path in WEB_FILES:
                    return self._web_file(*WEB_FILES[self.path])
                device = self._device()
                if device is None:
                    return self._reply(401, {"error": "llave no válida"})
                if self.path == "/api/v1/events":
                    return self._events(device)
                if self.path == "/api/v1/history":
                    return self._reply(200, {"messages": server.get_history()})
                if self.path == "/api/v1/whoami":
                    return self._reply(200, self._who(device))
                if self.path == "/api/v1/push/key" and server.push is not None:
                    return self._push_key()
                self._reply(404, {"error": "no existe"})

            def do_POST(self):
                if self.path == "/api/v1/login":
                    return self._login()
                if self.path == "/api/v1/logout":
                    if not self._from_our_page():
                        return self._reply(403, {"error": "solo desde la web de ARTHUR"})
                    return self._set_cookie(200, "", 0)
                device = self._device(changes=True)
                if device is None:
                    return self._reply(401, {"error": "llave no válida"})
                if self.path == "/api/v1/message":
                    return self._message(device)
                if self.path == "/api/v1/confirm":
                    return self._confirm(device)
                if self.path == "/api/v1/capabilities" and server.hub is not None:
                    return self._capabilities(device)
                if self.path == "/api/v1/tool_result" and server.hub is not None:
                    return self._tool_result(device)
                if self.path in ("/api/v1/voice", "/api/v1/speech", "/api/v1/voice/stream"):
                    if server.voice is None:
                        return self._reply(503, {"error": "la voz no está activada en este servidor"})
                    if self.path == "/api/v1/voice/stream":
                        return self._voice_stream(device)
                    return self._voice(device) if self.path == "/api/v1/voice" else self._speech(device)
                if self.path == "/api/v1/notice" and server.on_notice is not None:
                    return self._notice(device)
                if self.path in ("/api/v1/push/subscribe", "/api/v1/push/unsubscribe"):
                    if server.push is None:
                        return self._reply(503, {"error": "los avisos al móvil no están activados en este servidor"})
                    return self._subscribe(device) if self.path.endswith("/subscribe") else self._unsubscribe(device)
                self._reply(404, {"error": "no existe"})

            def _notice(self, device: str):
                # Solo la llave de los aparatos «studio» (Creator Studio), «panel» (el panel de vida), «webchat» (el
                # asistente de la web, para los contactos) y «cerebro» (el Repaso): un móvil robado no puede
                # hacer sonar avisos en el portátil. El texto no pasa por el cerebro: se entrega tal cual.
                if device not in NOTICE_DEVICES:
                    return self._reply(403, {"error": "solo Studio, el panel, el asistente web y el cerebro mandan avisos"})
                body = self._json_body()
                if body is None:
                    return
                text = body.get("text")
                text = " ".join("".join(c if c.isprintable() else " " for c in text).split()) \
                    if isinstance(text, str) else ""
                if not text or len(text) > MAX_NOTICE:
                    return self._reply(400, {"error": f"falta el texto o pasa de {MAX_NOTICE} caracteres"})
                try:
                    destination = notice_destination(device, body, server.panel_public_url,
                                                     server.clientes_public_url, server.studio_public_url,
                                                     server.apuntes_public_url)
                except PermissionError as e:
                    return self._reply(403, {"error": str(e)})
                except ValueError as e:
                    return self._reply(400, {"error": str(e)})
                except LookupError as e:
                    return self._reply(503, {"error": str(e)})
                if destination:
                    server.on_notice(text, target_url=destination)
                else:
                    server.on_notice(text)
                return self._reply(200, {"ok": True})

            def _message(self, device: str):
                body = self._json_body(MAX_IMAGE_BODY if server.vision else MAX_BODY)
                if body is None:
                    return
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                    return self._reply(400, {"error": f"«text» debe ser un texto de 1 a {MAX_TEXT} caracteres"})
                image = None
                if body.get("image") is not None:
                    if not server.vision:
                        return self._reply(400, {"error": "el cerebro de este servidor no ve fotos (brain.vision)"})
                    image = check_image(body["image"])
                    if image is None:
                        return self._reply(400, {"error": "«image» debe ser una foto JPEG, PNG o WebP "
                                                          f"en base64 de como mucho {MAX_IMAGE // 1024 // 1024} MB"})
                # Sin el texto ni la foto: solo quién, y el tamaño (para entender lo que tarda).
                log.info("core-api: mensaje de «%s»%s", device,
                         f" con foto ({len(image) * 3 // 4 // 1024} KB)" if image else "")
                if server.hub is not None:
                    server.hub.set_active(device)  # si dos aparatos saben lo mismo, manda el que te habla
                self.connection.settimeout(None)  # pensar (y esperar una confirmación) puede llevar minutos
                reply = server.handle_text(text, images=(image,)) if image else server.handle_text(text)
                self._reply(200, {"reply": reply})

            def _heard(self, device: str) -> str | None:
                """El audio que manda el aparato → lo que has dicho (arthur-voz). None si ya se contestó un error."""
                from ..integrations.voz import VoiceError
                from ..voice.controller import strip_wake_phrase

                audio = self._raw_body("audio/wav", MAX_AUDIO)
                if audio is None:
                    return None
                started = time.monotonic()
                try:
                    heard = server.voice.transcribe(audio)
                except VoiceError as e:
                    log.warning("core-api: voz de «%s» sin transcribir: %s", device, e)
                    self._reply(e.status, {"error": str(e)})
                    return None
                log.info("core-api: voz de «%s» transcrita en %.1f s", device, time.monotonic() - started)
                heard = strip_wake_phrase(heard)[:MAX_TEXT]  # sin el «Hey Arthur» que venía en el audio previo
                if heard and server.hub is not None:
                    server.hub.set_active(device)
                return heard

            def _voice(self, device: str):
                """Lo que has dicho → texto (arthur-voz) → ARTHUR → respuesta en texto. La voz de la respuesta
                la pide luego el aparato a /speech: así puede enseñarla ya y cortarla si vuelves a hablar."""
                heard = self._heard(device)
                if heard is None:
                    return
                if not heard:
                    return self._reply(200, {"heard": "", "reply": None})
                self.connection.settimeout(None)  # pensar (y esperar una confirmación) puede llevar minutos
                self._reply(200, {"heard": heard, "reply": server.handle_text(heard)})

            def _voice_stream(self, device: str):
                """Como /voice, pero la voz va saliendo a frases mientras el modelo escribe (core/spoken.py):
                cada frase lista va a arthur-voz en un hilo aparte y su audio sale en cuanto está, en orden.
                Una línea JSON por evento; la conexión se cierra al acabar (sin Content-Length)."""
                from ..integrations.voz import VoiceError

                heard = self._heard(device)
                if heard is None:
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.close_connection = True
                gone = threading.Event()  # el aparato ha cortado (le has vuelto a hablar): no se le escribe más

                def line(payload: dict) -> None:
                    if gone.is_set():
                        return
                    try:
                        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
                        self.wfile.flush()
                    except OSError:
                        gone.set()

                line({"heard": heard})
                if not heard:
                    return line({"reply": None})
                self.connection.settimeout(None)  # pensar (y esperar una confirmación) puede llevar minutos
                sentences: queue.Queue = queue.Queue()

                def speak() -> None:  # el único que escribe mientras ARTHUR piensa: las frases salen en orden
                    while (sentence := sentences.get()) is not None:
                        if gone.is_set():
                            continue  # ya no hay a quién: no se gasta la voz
                        try:
                            audio = server.voice.speak(sentence[:MAX_TEXT])
                        except VoiceError as e:
                            log.warning("core-api: sin voz para una frase de «%s»: %s", device, e)
                            continue
                        line({"say": sentence, "audio": base64.b64encode(audio).decode()})

                speaker = threading.Thread(target=speak, name="voice-stream", daemon=True)
                speaker.start()
                try:
                    reply = server.handle_text(heard, on_speech=sentences.put)
                finally:
                    sentences.put(None)
                    speaker.join()
                line({"reply": reply})

            def _speech(self, device: str):
                from ..integrations.voz import VoiceError

                body = self._json_body()
                if body is None:
                    return
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                    return self._reply(400, {"error": f"«text» debe ser un texto de 1 a {MAX_TEXT} caracteres"})
                try:
                    audio = server.voice.speak(text)
                except VoiceError as e:
                    log.warning("core-api: sin voz para «%s»: %s", device, e)
                    return self._reply(e.status, {"error": str(e)})
                self._send_bytes(200, "audio/wav", audio)

            def _push_key(self):
                from ..integrations.push import PushError

                try:
                    return self._reply(200, {"key": server.push.link.public_key()})
                except PushError as e:
                    log.warning("core-api: sin llave de avisos: %s", e)
                    return self._reply(502, {"error": "arthur-push no responde"})

            def _subscribe(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                try:
                    server.push.subscriptions.set(device, body.get("subscription"))
                except ValueError as e:  # SubscriptionError incluido: dirección o llaves que no valen
                    log.warning("core-api: suscripción de avisos rechazada para «%s»: %s", device, e)
                    return self._reply(400, {"error": str(e)})
                log.info("core-api: «%s» activa los avisos al móvil", device)
                # Uno de prueba al momento: así se ve ya que llegan, sin esperar a un recordatorio.
                server.push.notify_later(server._name, "Avisos activados en este aparato ✔", only=device)
                self._reply(200, {"ok": True})

            def _unsubscribe(self, device: str):
                removed = server.push.subscriptions.remove(device)
                log.info("core-api: «%s» quita los avisos al móvil", device)
                self._reply(200, {"ok": True, "removed": removed})

            def _confirm(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                pending_id, answer = body.get("id"), body.get("answer")
                if not isinstance(pending_id, str) or not isinstance(answer, bool) or server.broker is None:
                    return self._reply(400, {"error": "se espera {\"id\": texto, \"answer\": true|false}"})
                ok = server.broker.answer(pending_id, answer)
                log.info("core-api: «%s» %s una confirmación", device, "contesta" if ok else "llega tarde a")
                self._reply(200 if ok else 409, {"ok": ok})

            def _capabilities(self, device: str):
                from ..core.remote_tools import SpecError

                body = self._json_body()
                if body is None:
                    return
                try:
                    added, rejected = server.hub.register(device, body.get("tools"))
                except SpecError as e:
                    return self._reply(400, {"error": str(e)})
                self._reply(200, {"registered": added, "rejected": rejected})

            def _tool_result(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                ok = server.hub.resolve(device, body.get("id"), body.get("status"), body.get("output"))
                self._reply(200 if ok else 409, {"ok": ok})

            def _events(self, device: str):
                q: queue.Queue = queue.Queue(maxsize=500)
                with server._lock:
                    if len(server._streams) >= MAX_STREAMS:
                        return self._reply(503, {"error": "demasiadas conexiones"})
                    server._streams[q] = device
                log.info("core-api: «%s» se conecta a los eventos", device)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    pending = server.broker.pending() if server.broker else []
                    self._send(json.dumps({"type": "hello", "data": {
                        "name": server._name, "state": str(server._get_state()), "device": device,
                        "pending": pending}, "ts": time.time()}, ensure_ascii=False))
                    while not server._stopping.is_set():
                        try:
                            message = q.get(timeout=HEARTBEAT_SECONDS)
                            if message is None:
                                break
                            self._send(message)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    pass
                finally:
                    with server._lock:
                        server._streams.pop(q, None)
                        still_here = device in server._streams.values()
                    if server.hub is not None and not still_here:
                        server.hub.disconnect(device)  # sin conexión: sus manos, apartadas un rato por si vuelve
                    log.info("core-api: «%s» se desconecta de los eventos", device)

            def _send(self, message: str):
                self.wfile.write(f"data: {message}\n\n".encode())
                self.wfile.flush()

            def _reply(self, code: int, payload: dict):
                self._send_bytes(code, "application/json; charset=utf-8",
                                 json.dumps(payload, ensure_ascii=False).encode())

            def _send_bytes(self, code: int, content_type: str, body: bytes, headers: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {"Cache-Control": "no-store"}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            # --- la web de chat ---
            def _web_file(self, name: str, content_type: str):
                try:
                    body = (WEB_DIR / name).read_bytes()
                except OSError:
                    return self._reply(404, {"error": "no existe"})
                self._send_bytes(200, content_type, body, WEB_HEADERS)

            def _who(self, device: str) -> dict:
                # Lo mismo al entrar y al abrir la web: si no, tras entrar faltaba «voice» y no salía el 🎙.
                return {"device": device, "name": server._name, "voice": server.voice is not None,
                        "vision": server.vision, "push": server.push is not None,
                        "push_on": server.push is not None and device in server.push.subscriptions.all()}

            def _login(self):
                if not self._from_our_page():
                    return self._reply(403, {"error": "solo desde la web de ARTHUR"})
                body = self._json_body()
                if body is None:
                    return
                key = body.get("key")
                device = server.devices.check(key.strip()) if isinstance(key, str) else None
                if device is None:
                    log.warning("core-api: llave no válida al entrar en la web desde %s", self.client_address[0])
                    time.sleep(BAD_KEY_DELAY)
                    return self._reply(401, {"error": "llave no válida"})
                log.info("core-api: «%s» entra en la web", device)
                self._set_cookie(200, key.strip(), COOKIE_DAYS * 86400, self._who(device))

            def _set_cookie(self, code: int, value: str, max_age: int, payload: dict | None = None):
                # HttpOnly: el JavaScript de la página no puede leerla (ni uno inyectado). SameSite=Strict: el
                # navegador no la manda en peticiones que empiezan en otra web. Secure si se entra por HTTPS.
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                body = json.dumps(payload or {"ok": True}, ensure_ascii=False).encode()
                self._send_bytes(code, "application/json; charset=utf-8", body, {
                    "Cache-Control": "no-store",
                    "Set-Cookie": f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}",
                })

        return Handler


def check_image(value: object) -> str | None:
    """La foto en base64, limpia, si de verdad es un JPEG, PNG o WebP de como mucho MAX_IMAGE; si no, None."""
    if not isinstance(value, str) or len(value) > MAX_IMAGE * 4 // 3 + 4:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    webp = raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if not raw or len(raw) > MAX_IMAGE or not (raw.startswith(IMAGE_MAGIC) or webp):
        return None
    return base64.b64encode(raw).decode("ascii")
