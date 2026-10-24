"""La página de clientes y la entrada de contactos del chat de la web (docs/clientes.md).

Rutas (JSON salvo los ficheros):
  GET  /                                 la página (sin nada secreto; para ver algo hay que entrar con una llave)
  GET  /clientes.js, /clientes.css, /tokens.css, /componentes.css, /fonts/…, iconos y manifest   (nombres fijos)
  POST /api/login {"key"}                guarda la llave del aparato en una cookie HttpOnly     POST /api/logout

  POST /api/interno/contacto             SOLO con la llave de webchat («Authorization: Bearer»):
       {"name","email","phone","need","lang"} → {"id", "nuevo"}; 400 {"error","field"} si un dato no vale

  Con llave de aparato (cookie o Bearer):
  GET  /api/clientes?q=&estado=          la lista (número, nombre, estado, último contacto, sin leer, posible duplicado)
  GET  /api/clientes/<id>                la ficha con sus mensajes; al abrirla quedan leídos
  POST /api/clientes                     alta a mano {"nombre","email","telefono","origen","texto"} → {"id"}
  POST /api/clientes/<id>                cambiar {"nombre","email","telefono","estado","notas"} → la ficha
  POST /api/clientes/<id>/mensaje        apuntar una llamada o un WhatsApp {"texto","origen"} → la ficha
  POST /api/clientes/<id>/borrar         borrar la ficha y sus mensajes (derecho de supresión)

Seguridad (como el panel, panel/web.py):
  - Dos clases de llave que no se mezclan: la de webchat SOLO sirve para la ruta interna (no lista, no lee, no
    borra: 403) y las de aparato SOLO para la página (no pueden meter contactos: 403). Solo se guardan sus huellas.
  - Una llave mala espera antes de contestar. Con cookie, toda petición que cambia algo exige X-Arthur y, si llega
    Origin, que sea este servidor; si no, 403.
  - El registro solo apunta método, ruta (sin «?q=…»), código y tiempo: nunca nombres, correos, teléfonos, notas
    ni mensajes, ni las llaves.
  - Solo se sirven ficheros de una lista fija, nunca rutas que vengan de la petición.
  - Pensado para una red privada: nunca abierto a internet sin otra puerta delante.
"""

from __future__ import annotations

import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from ..security.devices import DeviceError, DeviceRegistry
from .store import ClientError, ClientStore

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
_CSS, _JS, _FONT = "text/css; charset=utf-8", "text/javascript; charset=utf-8", "font/woff2"
WEB_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
             "/clientes.js": ("clientes.js", _JS),
             "/clientes.css": ("clientes.css", _CSS),
             "/tokens.css": ("tokens.css", _CSS),
             "/componentes.css": ("componentes.css", _CSS),
             "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
             "/arthur.svg": ("arthur.svg", "image/svg+xml"),
             "/arthur-180.png": ("arthur-180.png", "image/png"),
             "/arthur-512.png": ("arthur-512.png", "image/png"),
             "/fonts/ibm-plex-sans-400.woff2": ("fonts/ibm-plex-sans-400.woff2", _FONT),
             "/fonts/ibm-plex-sans-600.woff2": ("fonts/ibm-plex-sans-600.woff2", _FONT),
             "/fonts/jetbrains-mono-400.woff2": ("fonts/jetbrains-mono-400.woff2", _FONT),
             "/fonts/jetbrains-mono-700.woff2": ("fonts/jetbrains-mono-700.woff2", _FONT)}
WEB_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
COOKIE = "clientes_llave"
COOKIE_DAYS = 180
# Lo más grande que viaja son unas notas (4000 caracteres); con acentos y escapes JSON caben de sobra en 32 KiB.
MAX_JSON = 32 * 1024
REQUEST_TIMEOUT = 30
BAD_KEY_DELAY = 0.5
INTERNAL = "/api/interno/contacto"
CLIENT_ROUTE = re.compile(r"^/api/clientes/(\d{1,9})(?:/(mensaje|borrar))?$")
ERRORS = {"name": "Falta el nombre o es demasiado largo", "email": "El correo no es válido",
          "phone": "El teléfono no es válido (solo cifras, espacios o guiones; mínimo 9 cifras)",
          "text": "Falta el mensaje o es demasiado largo", "origin": "Origen no válido",
          "status": "Estado no válido", "notes": "Las notas son demasiado largas",
          "contact": "Hace falta un correo o un teléfono",
          "duplicado": "Ese correo o teléfono ya es de otro cliente"}


class WebchatKey:
    """La llave del chat de la web para la ruta interna. En disco solo queda su huella (sha256, fichero 600):
    quien lea el volumen no puede usarla. `rotate()` hace otra y la anterior deja de valer."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def rotate(self) -> str:
        token = "arthur_" + secrets.token_urlsafe(32)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        # Se crea ya con permisos 600 y se cambia de golpe, como devices.json.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_digest(token) + "\n")
        os.replace(tmp, self.path)
        return token

    def check(self, token: str | None) -> bool:
        if not token or len(token) > 200:
            return False
        try:
            stored = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return False  # sin llave creada nadie entra por la ruta interna
        return bool(stored) and hmac.compare_digest(stored, _digest(token))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


class ClientesWeb:
    def __init__(self, store: ClientStore, devices: DeviceRegistry, webchat: WebchatKey, *,
                 host: str = "127.0.0.1", port: int = 8777, sleep: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self.devices = devices
        self.webchat = webchat
        self.sleep = sleep  # inyectable: los tests comprueban la espera sin esperar de verdad
        self.httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self.httpd.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _make_handler(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT
            server_version = "arthur-clientes"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # el registro por defecto lleva la ruta entera (con «?q=nombre»); se anota a mano en _send

            # --- autenticación ---
            def _token(self) -> tuple[str | None, bool]:
                """(llave, viene en cookie)."""
                header = self.headers.get("Authorization", "")
                if header.startswith("Bearer "):
                    return header[7:].strip(), False
                return self._cookie_key(), True

            def _device(self, changes: bool = False) -> tuple[str | None, int]:
                """(aparato, código de error) para las rutas de la página."""
                token, from_cookie = self._token()
                if not token:
                    return None, 401
                if from_cookie and changes and not self._from_our_page():
                    log.warning("clientes-web: petición con cookie desde fuera de la página")
                    return None, 403
                try:
                    name = web.devices.check(token)
                except DeviceError:
                    log.error("clientes-web: no se puede leer el registro de aparatos")
                    return None, 503
                if name is not None:
                    return name, 200
                if web.webchat.check(token):
                    # La llave del chat es válida, pero no para esto: no lista, no lee, no borra.
                    log.warning("clientes-web: la llave de webchat intenta usar la página")
                    return None, 403
                log.warning("clientes-web: llave no válida")
                web.sleep(BAD_KEY_DELAY)
                return None, 401

            def _webchat_ok(self) -> int:
                """Código para la ruta interna: 200 solo con la llave de webchat por Bearer."""
                token, from_cookie = self._token()
                if not token:
                    return 401
                if not from_cookie and web.webchat.check(token):
                    return 200
                try:
                    is_device = web.devices.check(token) is not None
                except DeviceError:
                    is_device = False
                if is_device:
                    log.warning("clientes-web: una llave de aparato intenta usar la ruta interna")
                    return 403
                log.warning("clientes-web: llave no válida en la ruta interna")
                web.sleep(BAD_KEY_DELAY)
                return 401

            def _cookie_key(self) -> str | None:
                try:
                    jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                except http.cookies.CookieError:
                    return None
                return jar[COOKIE].value if COOKIE in jar else None

            def _from_our_page(self) -> bool:
                if self.headers.get("X-Arthur") != "1":
                    return False
                origin, host = self.headers.get("Origin"), self.headers.get("Host", "")
                return origin is None or origin in (f"http://{host}", f"https://{host}")

            def _json_body(self) -> dict | None:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self.close_connection = True
                    self._reply(415, {"error": "se espera application/json"})
                    return None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= MAX_JSON:
                    self.close_connection = True  # no se lee el cuerpo: se corta la conexión
                    self._reply(413, {"error": "petición vacía o demasiado grande"})
                    return None
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict):
                    self._reply(400, {"error": "JSON no válido"})
                    return None
                return body

            # --- GET ---
            def do_GET(self):
                self._started = time.monotonic()
                url = urllib.parse.urlsplit(self.path)
                self._route = url.path
                if url.path in WEB_FILES:
                    name, kind = WEB_FILES[url.path]
                    return self._send(200, kind, (WEB_DIR / name).read_bytes(), WEB_HEADERS)
                device, code = self._device()
                if device is None:
                    return self._deny(code)
                if url.path == "/api/clientes":
                    params = urllib.parse.parse_qs(url.query)
                    query = params.get("q", [""])[0][:200]
                    estado = params.get("estado", [""])[0] or None
                    try:
                        return self._reply(200, {"clientes": web.store.list(query, estado)})
                    except ClientError as e:
                        return self._error(e)
                match = CLIENT_ROUTE.match(url.path)
                if match and match.group(2) is None:
                    cid = int(match.group(1))
                    web.store.mark_read(cid)
                    ficha = web.store.get(cid)
                    if ficha is None:
                        return self._reply(404, {"error": "Ese cliente no existe"})
                    return self._reply(200, ficha)
                return self._reply(404, {"error": "no existe"})

            # --- POST ---
            def do_POST(self):
                self._started = time.monotonic()
                path = self.path.split("?", 1)[0]
                self._route = path
                if path == "/api/login":
                    return self._login()
                if path == "/api/logout":
                    if not self._from_our_page():
                        self.close_connection = True
                        return self._reply(403, {"error": "solo desde la página de clientes"})
                    return self._set_cookie(200, "", 0)
                if path == INTERNAL:
                    code = self._webchat_ok()
                    if code != 200:
                        self.close_connection = True
                        return self._deny(code)
                    return self._internal_contact()
                device, code = self._device(changes=True)
                if device is None:
                    self.close_connection = True
                    return self._deny(code)
                if path == "/api/clientes":
                    return self._create()
                match = CLIENT_ROUTE.match(path)
                if match:
                    cid, action = int(match.group(1)), match.group(2)
                    if action == "borrar":
                        if not web.store.delete(cid):
                            return self._reply(404, {"error": "Ese cliente no existe"})
                        log.info("clientes-web: «%s» borra un cliente", device)
                        return self._reply(200, {"ok": True})
                    return self._message(cid) if action == "mensaje" else self._update(cid)
                self.close_connection = True
                return self._reply(404, {"error": "no existe"})

            def _internal_contact(self):
                body = self._json_body()
                if body is None:
                    return
                phone = body.get("phone")
                try:
                    cid, new = web.store.record_contact(body.get("name"), body.get("email"),
                                                        "" if phone is None else phone, body.get("need"),
                                                        body.get("lang"))
                except ClientError as e:
                    # Para el chat, el campo del mensaje se llama «need» (como en su formulario).
                    field = "need" if e.field == "text" else e.field
                    return self._reply(400, {"error": ERRORS.get(e.field, "dato no válido"), "field": field})
                return self._reply(200, {"id": cid, "nuevo": new})

            def _create(self):
                body = self._json_body()
                if body is None:
                    return
                try:
                    cid = web.store.create(body.get("nombre"), body.get("email"), body.get("telefono"),
                                           body.get("origen"), body.get("texto") or "")
                except ClientError as e:
                    return self._error(e)
                return self._reply(200, {"id": cid})

            def _update(self, cid: int):
                body = self._json_body()
                if body is None:
                    return
                try:
                    web.store.update(cid, **body)
                except KeyError:
                    return self._reply(404, {"error": "Ese cliente no existe"})
                except ClientError as e:
                    return self._error(e)
                return self._reply(200, web.store.get(cid))

            def _message(self, cid: int):
                body = self._json_body()
                if body is None:
                    return
                try:
                    web.store.add_message(cid, body.get("texto"), body.get("origen"))
                except KeyError:
                    return self._reply(404, {"error": "Ese cliente no existe"})
                except ClientError as e:
                    return self._error(e)
                return self._reply(200, web.store.get(cid))

            def _login(self):
                if not self._from_our_page():
                    self.close_connection = True
                    return self._reply(403, {"error": "solo desde la página de clientes"})
                body = self._json_body()
                if body is None:
                    return
                key = body.get("key")
                try:
                    device = web.devices.check(key.strip()) if isinstance(key, str) else None
                except DeviceError:
                    log.error("clientes-web: no se puede leer el registro de aparatos")
                    return self._reply(503, {"error": "no disponible"})
                if device is None:
                    web.sleep(BAD_KEY_DELAY)
                    return self._reply(401, {"error": "llave no válida"})
                log.info("clientes-web: «%s» entra", device)
                self._set_cookie(200, key.strip(), COOKIE_DAYS * 86400, {"device": device})

            # --- respuestas ---
            def _deny(self, code: int):
                return self._reply(code, {"error": {401: "hace falta una llave", 503: "no disponible"}
                                          .get(code, "esta llave no sirve para esto")})

            def _error(self, e: ClientError):
                code = 409 if e.field == "duplicado" else 400
                # `field` es el nombre del almacén (name, phone, status…): la página sabe qué casilla marcar.
                return self._reply(code, {"error": ERRORS.get(e.field, "dato no válido"), "field": e.field})

            def _reply(self, code: int, payload):
                self._send(code, "application/json; charset=utf-8",
                           json.dumps(payload, ensure_ascii=False).encode(), {"Cache-Control": "no-store"})

            def _send(self, code: int, content_type: str, body: bytes, headers: dict):
                # Solo método, ruta (ya sin «?…»), código y tiempo. Las rutas solo llevan números de cliente.
                # Se apunta antes de contestar: así la línea ya está escrita cuando el otro lado lee la respuesta.
                started = getattr(self, "_started", None)
                ms = (time.monotonic() - started) * 1000 if started is not None else 0.0
                log.info("clientes-web: %s %s %d %.0f ms", self.command, getattr(self, "_route", "?"), code, ms)
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _set_cookie(self, code: int, value: str, max_age: int, payload: dict | None = None):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                body = json.dumps(payload or {"ok": True}, ensure_ascii=False).encode()
                self._send(code, "application/json; charset=utf-8", body, {
                    "Cache-Control": "no-store",
                    "Set-Cookie": f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}",
                })

        return Handler
