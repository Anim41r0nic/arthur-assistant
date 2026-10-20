"""arthur-push: el que manda los avisos al móvil (fase S6, docs/servidor.md).

Corre en su propio contenedor (Dockerfile.push) al lado de arthur-core. Solo le habla arthur-core, por la red
interna de Docker y con llave; no publica ningún puerto. Sale a internet únicamente para entregar el aviso al
servicio de avisos del navegador (Apple, Google, Mozilla o Microsoft: subscription.py).

API:
  GET  /health → {"ok": true}                                   (sin llave)
  GET  /key    → {"key": "B…"}   la llave pública VAPID, la que la web le da al navegador al suscribirse
  POST /send   {"subscription": {...}, "title": "…", "body": "…", "ttl": 86400, "urgency": "high"}
               → {"status": 201}  lo que contestó el servicio de avisos (404/410: esa suscripción ya no vale)

Por qué un contenedor aparte: cifrar y firmar necesita la biblioteca `cryptography`, y arthur-core sigue siendo
solo biblioteca estándar. Además, la llave privada VAPID vive solo aquí (/data/vapid.pem, 600).

Seguridad:
  - Llave compartida con arthur-core (secreto 600) y comparación en tiempo constante.
  - Vuelve a comprobar la suscripción: solo servicios de avisos conocidos, por https (nada de la red de casa).
  - No sigue redirecciones: el aviso va a esa dirección exacta o a ninguna.
  - El registro dice a qué servicio se mandó y qué contestó; nunca el texto del aviso ni la dirección entera
    (identifica al teléfono).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .subscription import SubscriptionError, b64url_decode, check_subscription

log = logging.getLogger(__name__)

MAX_JSON = 8 * 1024
MAX_TITLE = 100
MAX_BODY_TEXT = 1000
MAX_TTL = 7 * 86400
REQUEST_TIMEOUT = 15
SEND_TIMEOUT = 15
BAD_KEY_DELAY = 0.5


class PushServerError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # una redirección se queda en error: nunca se sigue a otra dirección


def _https_send(endpoint: str, body: bytes, headers: dict[str, str]) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with opener.open(req, timeout=SEND_TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code


class PushServer:
    def __init__(self, *, token: str, vapid, subject: str, host: str = "127.0.0.1", port: int = 8772,
                 encrypt: Callable[[bytes, bytes, bytes], bytes] | None = None,
                 send: Callable[[str, bytes, dict], int] = _https_send) -> None:
        """`vapid`: webpush.Vapid (o uno falso en los tests). `encrypt(texto, p256dh, auth)` y
        `send(endpoint, cuerpo, cabeceras) -> código` se pueden cambiar en los tests: sin red ni criptografía."""
        if len(token) < 32:
            raise PushServerError("La llave de arthur-push es demasiado corta (mínimo 32 caracteres)")
        if not subject.startswith(("mailto:", "https://")):
            raise PushServerError("ARTHUR_PUSH_SUBJECT debe ser mailto:… o https://… (lo exigen Apple y Google)")
        if encrypt is None:
            from .webpush import encrypt
        self._token = token.encode()
        self.vapid = vapid
        self.subject = subject
        self.encrypt = encrypt
        self.send = send
        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="push-api", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("arthur-push escuchando en el puerto %d", self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def deliver(self, subscription: dict, payload: dict, *, ttl: int, urgency: str) -> int:
        body = self.encrypt(json.dumps(payload, ensure_ascii=False).encode(),
                            b64url_decode(subscription["keys"]["p256dh"]),
                            b64url_decode(subscription["keys"]["auth"]))
        endpoint = subscription["endpoint"]
        headers = {
            "Authorization": self.vapid.authorization(endpoint, self.subject),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),            # cuánto lo guarda el servicio si el móvil está apagado
            "Urgency": urgency,
        }
        return self.send(endpoint, body, headers)

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT
            server_version = "arthur-push"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # sin el registro por defecto; se anota a mano y sin contenido

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                given = header[7:].strip().encode() if header.startswith("Bearer ") else b""
                if hmac.compare_digest(given, server._token):  # tiempo constante: no da pistas de la llave
                    return True
                log.warning("arthur-push: llave no válida desde %s", self.client_address[0])
                time.sleep(BAD_KEY_DELAY)
                self._reply(401, {"error": "llave no válida"})
                return False

            def do_GET(self):
                if self.path == "/health":
                    return self._reply(200, {"ok": True})
                if not self._authorized():
                    return
                if self.path == "/key":
                    return self._reply(200, {"key": server.vapid.public_key})
                self._reply(404, {"error": "no existe"})

            def do_POST(self):
                if not self._authorized():
                    return
                if self.path != "/send":
                    return self._reply(404, {"error": "no existe"})
                body = self._json()
                if body is None:
                    return
                try:
                    subscription = check_subscription(body.get("subscription"))
                except SubscriptionError as e:
                    return self._reply(400, {"error": str(e)})
                title, text = body.get("title"), body.get("body")
                ttl, urgency = body.get("ttl", 86400), body.get("urgency", "normal")
                if not isinstance(title, str) or not 0 < len(title) <= MAX_TITLE:
                    return self._reply(400, {"error": f"«title» debe tener de 1 a {MAX_TITLE} caracteres"})
                if not isinstance(text, str) or not 0 < len(text) <= MAX_BODY_TEXT:
                    return self._reply(400, {"error": f"«body» debe tener de 1 a {MAX_BODY_TEXT} caracteres"})
                if isinstance(ttl, bool) or not isinstance(ttl, int) or not 0 <= ttl <= MAX_TTL:
                    return self._reply(400, {"error": f"«ttl» debe ser de 0 a {MAX_TTL} segundos"})
                if urgency not in ("very-low", "low", "normal", "high"):
                    return self._reply(400, {"error": "«urgency» no válida"})
                destination = body.get("url")
                if destination is not None:
                    from .destination import check_notice_target

                    try:
                        # La raíz del panel, de clientes o de Studio; como mucho con `#c<n>` o `#v<n>-<perfil>`.
                        destination = check_notice_target(destination)
                    except ValueError as e:
                        return self._reply(400, {"error": str(e)})
                service = urllib.parse.urlsplit(subscription["endpoint"]).hostname
                started = time.monotonic()
                try:
                    payload = {"title": title, "body": text}
                    if destination:
                        payload["url"] = destination
                    status = server.deliver(subscription, payload, ttl=ttl, urgency=urgency)
                except (urllib.error.URLError, OSError) as e:
                    log.warning("arthur-push: no llegó a %s: %s", service, getattr(e, "reason", e))
                    return self._reply(502, {"error": f"no llegó al servicio de avisos ({service})"})
                log.info("arthur-push: aviso a %s → %d (%.1f s)", service, status, time.monotonic() - started)
                self._reply(200, {"status": status})

            def _json(self) -> dict | None:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self._reply(415, {"error": "se espera application/json"})
                    return None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= MAX_JSON:
                    self._reply(413, {"error": "cuerpo vacío o demasiado grande"})
                    return None
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict):
                    self._reply(400, {"error": "JSON no válido"})
                    return None
                return body

            def _reply(self, code: int, payload: dict):
                data = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

        return Handler


# --- arranque (lo usa el contenedor) ------------------------------------------

def main(env: dict[str, str] | None = None, stop: threading.Event | None = None) -> int:
    from .webpush import Vapid

    env = os.environ if env is None else env
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    try:
        token_path = env.get("ARTHUR_PUSH_TOKEN_FILE", "/run/secrets/push_token")
        try:
            token = Path(token_path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise PushServerError(f"No puedo leer la llave de arthur-push ({token_path}): {e}") from e
        vapid = Vapid.load_or_create(Path(env.get("ARTHUR_PUSH_KEY_FILE", "/data/vapid.pem")))
        server = PushServer(token=token, vapid=vapid, subject=env.get("ARTHUR_PUSH_SUBJECT", ""),
                            host=env.get("ARTHUR_PUSH_HOST", "127.0.0.1"),
                            port=int(env.get("ARTHUR_PUSH_PORT", "8772")))
    except (PushServerError, ValueError, OSError) as e:
        log.error("arthur-push no arranca: %s", e)
        return 1
    server.start()
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        import signal

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
