"""Servidor local del Orb: sirve la interfaz y le retransmite los eventos del núcleo.

Usa Server-Sent Events (SSE): una conexión HTTP que se queda abierta y por la que
el servidor va enviando eventos ("data: {...}"). El navegador lo trae incluido
(EventSource) y se reconecta solo. Solo librería estándar.

El Orb NO puede darle órdenes a ARTHUR. Solo puede enviar dos cosas, cada una con
un único valor Sí/No:
  - la respuesta a una confirmación pendiente: POST /api/confirm {"id", "answer"};
  - el botón de la cámara de los gestos: POST /api/gestures {"enabled"} (solo existe
    si los gestos están permitidos en config.toml; encender es un clic tuyo).
Las dos exigen, además de lo de abajo: la cookie (nunca el token en la URL), que
Origin sea el propio Orb, la cabecera X-Arthur (obliga a cualquier otra web a pedir
un permiso CORS que nunca damos) y JSON de como mucho 1 KB.

Seguridad:
  - Solo escucha en 127.0.0.1 (este equipo).
  - Token secreto nuevo en cada arranque. La primera visita lo lleva en la URL
    (?t=…); después viaja en una cookie HttpOnly + SameSite=Strict, que ninguna
    otra web puede enviar ni leer.
  - Se comprueba la cabecera Host: evita el ataque "DNS rebinding", en el que una
    web maliciosa hace que su dominio apunte a 127.0.0.1 para leer servidores locales.
  - Content-Security-Policy: la página no puede cargar nada de fuera ni ejecutar
    código que no venga de este servidor.
  - Solo se sirven ficheros de ui/orb con extensiones conocidas (sin escapes "../").
  - Una conexión que no envía su petición en REQUEST_TIMEOUT segundos se cierra (no se acumulan hilos).
"""

import base64
from collections import deque
import hashlib
import hmac
import http.cookies
import json
import logging
import queue
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core.events import ALL, Event, EventBus

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[2] / "ui" / "orb"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
FORWARDED_EVENTS = {
    "state_changed", "user_message", "assistant_message", "error",
    "tool_started", "tool_finished", "confirmation_requested", "audio_level",
    "confirmation_pending", "confirmation_resolved", "camera_state", "gesture_action",
}  # "gesture" NO: el Orb no lo necesita y así sale menos información
# Eventos que se recuerdan para enviárselos a un Orb que se conecte tarde
# (los cambios de estado no: solo importa el estado actual, que va en "hello").
REPLAYED_EVENTS = FORWARDED_EVENTS - {
    "state_changed", "confirmation_requested", "audio_level", "confirmation_pending", "confirmation_resolved",
    "camera_state", "gesture_action",
}  # las confirmaciones abiertas y el estado de la cámara van en "hello"
MAX_POST_BYTES = 1024
REPLAY_SIZE = 30
MAX_CLIENTS = 8
MAX_TEXT = 2_000
HEARTBEAT_SECONDS = 15
# Cuánto espera el servidor a que un cliente envíe su petición. Sin límite, una conexión que no dice nada se
# queda abierta para siempre (con un hilo del servidor) y cualquiera que alcance el puerto podría juntar miles.
REQUEST_TIMEOUT = 15
COOKIE = "arthur_token"


def _same_secret(given: str, expected: str) -> bool:
    """Compara en tiempo constante. Con bytes: con `str`, hmac.compare_digest lanza TypeError si el texto
    que llega de fuera tiene caracteres no ASCII (un `?t=é` bastaba para romper la petición)."""
    return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))


def _csp(index_html: str) -> str:
    # El "import map" (dónde está la librería three) va dentro del HTML; autorizamos
    # exactamente ese bloque por su huella sha256 y nada más.
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(block.encode()).digest()).decode() + "'"
        for block in re.findall(r'<script type="importmap">(.*?)</script>', index_html, re.S)
    ]
    return "; ".join([
        "default-src 'none'",
        f"script-src 'self' {' '.join(hashes)}".strip(),
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])


class OrbServer:
    def __init__(
        self,
        bus: EventBus,
        *,
        port: int,
        assistant_name: str,
        get_state: Callable[[], str],
        static_dir: Path = STATIC_DIR,
        broker=None,
        gestures=None,
    ) -> None:
        self.broker = broker  # ConfirmationBroker: si es None, /api/confirm no existe
        self.gestures = gestures  # GestureSwitch: si es None (gestos no permitidos), /api/gestures no existe
        self.token = secrets.token_urlsafe(24)
        self._name = assistant_name
        self._get_state = get_state
        self._static = static_dir.resolve()
        self._clients: set[queue.Queue] = set()
        self._recent: deque[str] = deque(maxlen=REPLAY_SIZE)  # solo en RAM
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._csp = _csp((self._static / "index.html").read_text(encoding="utf-8"))
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._unsubscribe = bus.subscribe(ALL, self._on_event)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="orb-server", daemon=True)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        self._thread.start()
        log.info("Orb disponible en http://127.0.0.1:%s/", self.port)

    def stop(self) -> None:
        self._stopping.set()
        self._unsubscribe()
        self._httpd.shutdown()
        self._httpd.server_close()

    def camera_status(self) -> dict:
        """Para el «hello»: ¿hay botón de la cámara? ¿está encendida? (el punto rojo no depende de un evento viejo)."""
        watcher = self.gestures.watcher if self.gestures is not None else None
        return {"available": watcher is not None, "on": bool(watcher and watcher.active)}

    # --- eventos ----------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        if event.type not in FORWARDED_EVENTS:
            return
        data = {k: (v[:MAX_TEXT] if isinstance(v, str) else v) for k, v in event.data.items()}
        self._broadcast({"type": event.type, "data": data, "ts": event.timestamp})

    def _broadcast(self, payload: dict) -> None:
        message = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            if payload["type"] in REPLAYED_EVENTS:
                self._recent.append(message)
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(message)
            except queue.Full:
                pass  # un cliente lento no debe frenar a ARTHUR: pierde eventos, no el núcleo

    # --- HTTP -------------------------------------------------------------

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ARTHUR"
            sys_version = ""
            timeout = REQUEST_TIMEOUT

            def log_message(self, fmt, *args):
                log.debug("orb %s", fmt % args)

            def _host_ok(self) -> bool:
                return self.headers.get("Host") in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

            def _cookie_ok(self) -> bool:
                cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                return COOKIE in cookie and _same_secret(cookie[COOKIE].value, server.token)

            def do_POST(self):
                if not self._host_ok():
                    return self._plain(403, "Host no permitido")
                routes = {
                    "/api/confirm": self._post_confirm if server.broker is not None else None,
                    "/api/gestures": self._post_gestures if server.gestures is not None else None,
                }
                handler = routes.get(urllib.parse.urlsplit(self.path).path)
                if handler is None:
                    return self._plain(404, "No encontrado")
                origins = (f"http://127.0.0.1:{server.port}", f"http://localhost:{server.port}")
                if not self._cookie_ok() or self.headers.get("Origin") not in origins or self.headers.get("X-Arthur") != "1":
                    return self._plain(403, "No autorizado")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    return self._plain(415, "Se espera JSON")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= MAX_POST_BYTES:
                    return self._plain(413, "Petición vacía o demasiado grande")
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    return self._plain(400, "Formato no válido")
                if not isinstance(body, dict):
                    return self._plain(400, "Formato no válido")
                handler(body)

            def _post_confirm(self, body: dict):
                pending_id, answer = body.get("id"), body.get("answer")
                if not isinstance(pending_id, str) or not isinstance(answer, bool):
                    return self._plain(400, "Formato no válido")
                if not server.broker.answer(pending_id, answer):
                    return self._plain(404, "Esa confirmación no existe o ya ha caducado")
                self._plain(200, "ok")

            def _post_gestures(self, body: dict):
                # Solo un Sí/No: ni rutas, ni dispositivos, ni nada más que se pueda colar.
                if body.keys() != {"enabled"} or not isinstance(body["enabled"], bool):
                    return self._plain(400, "Formato no válido")
                if server.gestures.watcher is None:
                    return self._plain(404, "Los gestos no están activos")
                self._plain(200, server.gestures.set(body["enabled"], by="orb"))

            def do_GET(self):
                if not self._host_ok():
                    return self._plain(403, "Host no permitido")
                url = urllib.parse.urlsplit(self.path)
                query_token = urllib.parse.parse_qs(url.query).get("t", [""])[0]
                cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                cookie_token = cookie[COOKIE].value if COOKIE in cookie else ""
                via_query = _same_secret(query_token, server.token)
                if not (via_query or _same_secret(cookie_token, server.token)):
                    return self._plain(403, "Falta el token. Abre la dirección que muestra ARTHUR al arrancar.")

                if url.path == "/events":
                    return self._events()
                path = "index.html" if url.path == "/" else url.path.lstrip("/")
                self._static_file(path, set_cookie=via_query)

            def _headers(self, code: int, content_type: str, *, set_cookie=False, length: int | None = None):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Security-Policy", server._csp)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                if set_cookie:
                    self.send_header("Set-Cookie", f"{COOKIE}={server.token}; HttpOnly; SameSite=Strict; Path=/")
                if length is not None:
                    self.send_header("Content-Length", str(length))
                self.end_headers()

            def _plain(self, code: int, text: str):
                body = text.encode()
                self._headers(code, "text/plain; charset=utf-8", length=len(body))
                self.wfile.write(body)

            def _static_file(self, rel: str, *, set_cookie: bool):
                target = (server._static / urllib.parse.unquote(rel)).resolve()
                content_type = CONTENT_TYPES.get(target.suffix)
                if not target.is_relative_to(server._static) or not target.is_file() or content_type is None:
                    return self._plain(404, "No encontrado")
                body = target.read_bytes()
                self._headers(200, content_type, set_cookie=set_cookie, length=len(body))
                self.wfile.write(body)

            def _events(self):
                q: queue.Queue = queue.Queue(maxsize=500)
                with server._lock:
                    if len(server._clients) >= MAX_CLIENTS:
                        return self._plain(503, "Demasiadas conexiones")
                    server._clients.add(q)
                    recent = list(server._recent)
                try:
                    self._headers(200, "text/event-stream; charset=utf-8")
                    pending = server.broker.pending() if server.broker else []
                    hello = {"type": "hello", "data": {"name": server._name, "state": str(server._get_state()),
                                                       "pending": pending, "camera": server.camera_status()},
                             "ts": time.time()}
                    self._send(json.dumps(hello, ensure_ascii=False))
                    for message in recent:
                        self._send(message)
                    while not server._stopping.is_set():
                        try:
                            self._send(q.get(timeout=HEARTBEAT_SECONDS))
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")  # comentario SSE: mantiene viva la conexión
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass  # el navegador cerró la pestaña (o dejó de leer: el límite de tiempo también corta escrituras)
                finally:
                    with server._lock:
                        server._clients.discard(q)

            def _send(self, message: str):
                self.wfile.write(f"data: {message}\n\n".encode())
                self.wfile.flush()

        return Handler
