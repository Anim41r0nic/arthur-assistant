"""Servidor HTTP del chat público de la web (detrás de nginx y del túnel de Cloudflare).

Rutas (JSON):
  POST /api/chat/session {turnstile}                        -> {token, expires}
  POST /api/chat {token, lang, history, message}            -> {reply, offer_contact, engine}
  POST /api/chat/contact {token, lang, name, email, phone?, need, consent} -> {ok: true}  (se entrega a clientes)
  GET  /api/chat/health                                     -> {ok, engines}  (sin datos de nadie)

Seguridad (esto está abierto a internet):
  - Todo POST exige `Origin` igual al de la web; sin él o con otro, 403 antes de leer el cuerpo.
  - Solo JSON, cuerpo <= 8 KB, cada campo validado por tipo y tamaño (400 si no).
  - Sin cookies. La IP sale de `CF-Connecting-IP` (nginx solo es alcanzable por el túnel) y, si falta o no es una
    IP, de la del socket.
  - El registro solo anota método, ruta conocida, código, motor y milisegundos: nunca texto de visitas, datos
    del formulario, IPs, pases ni el Origin.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .clientes_link import ClientesRejected, notify
from .prompt import norm_lang
from .store import ContactError, validate_contact

log = logging.getLogger(__name__)

MAX_JSON = 8 * 1024
MAX_TURNS = 10
MAX_TEXT = 500
ROLES = ("visita", "asistente")
REQUEST_TIMEOUT = 30
ROUTES = ("/api/chat/session", "/api/chat", "/api/chat/contact", "/api/chat/health")
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
TOO_MANY = {"error": "demasiados"}


def _log_internal(where: str, error: BaseException) -> None:
    # Como log.exception, pero SIN el mensaje de la excepción: ese mensaje puede llevar texto de la visita o su IP
    # (p. ej. un fallo que repite lo que recibió). Se anota el tipo y la pila (ficheros y líneas de código), que
    # basta para encontrar el fallo.
    log.error("webchat: fallo interno en %s (%s)\n%s", where, type(error).__name__,
              "".join(traceback.format_tb(error.__traceback__)).rstrip())


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # La versión estándar escribe en stderr la traza con la IP y el puerto de quien conectaba (acabaría en
        # `docker logs`). Aquí, el mismo aviso sin dirección y sin el mensaje de la excepción.
        _log_internal("la conexión", sys.exc_info()[1] or RuntimeError("desconocido"))


def _valid_history(history: object) -> bool:
    # Estricto: el historial acaba dentro del prompt, y un rol inventado («system») o un campo de más
    # es justo lo que intentaría quien quiere colar instrucciones.
    if not isinstance(history, list) or len(history) > MAX_TURNS:
        return False
    for turn in history:
        if not isinstance(turn, dict) or set(turn) != {"role", "text"}:
            return False
        if turn["role"] not in ROLES or not isinstance(turn["text"], str) or len(turn["text"]) > MAX_TEXT:
            return False
    return True


class WebchatWeb:
    def __init__(self, answerer, limiter, passes, turnstile, contacts, notifier, *, origin: str,
                 host: str = "127.0.0.1", port: int = 8776) -> None:
        self.answerer = answerer
        self.limiter = limiter
        self.passes = passes
        self.turnstile = turnstile
        self.contacts = contacts          # ClientesLink (deliver -> (id, nuevo) | None), o None si no hay clientes
        self.notifier = notifier          # con send(text, destination=…, client=…) -> bool, o None
        self.origin = origin
        self.httpd = _Server((host, port), self._make_handler())

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
            server_version = "arthur-webchat"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # el registro por defecto llevaría la IP y la ruta tal cual; se anota a mano en _done

            # --- utilidades ---
            def _ip(self) -> str:
                # Solo se acepta como IP lo que lo es de verdad: una cabecera basura no puede dar a cada
                # petición un «cliente» distinto y esquivar los límites; cae a la IP del socket.
                header = self.headers.get("CF-Connecting-IP", "").strip()
                try:
                    ipaddress.ip_address(header)
                    return header
                except ValueError:
                    return self.client_address[0]

            def _json_body(self) -> dict | None:
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    return self._bad(400)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self._bad(400)
                if length > MAX_JSON:
                    return self._bad(413)
                if length <= 0:
                    return self._bad(400)
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict):
                    return self._bad(400)
                return body

            def _bad(self, code: int):
                # Sin leer el cuerpo (o con basura) no se reutiliza la conexión.
                self.close_connection = True
                self._reply(code, {"error": {400: "peticion", 413: "grande"}[code]})
                return None

            def _reply(self, code: int, payload: dict, engine: str | None = None):
                self._code, self._engine = code, engine
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def end_headers(self):
                # Un solo sitio para las cabeceras de seguridad: pasan por aquí tanto nuestras respuestas como
                # las que fabrica la biblioteca estándar (send_error), así ninguna sale sin ellas.
                for name, value in HEADERS.items():
                    self.send_header(name, value)
                super().end_headers()

            def send_error(self, code, message=None, explain=None):
                # La versión estándar devuelve una página HTML que repite la línea de la petición (texto
                # ajeno) y no lleva nuestras cabeceras. Aquí: JSON mínimo, sin eco, y se cierra la conexión.
                self.close_connection = True
                self._code, self._engine = code, None
                body = json.dumps({"error": "peticion"}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _done(self, method: str, path: str, started: float):
                # Solo rutas conocidas: una ruta inventada puede llevar cualquier texto dentro.
                shown = path if path in ROUTES else "(otra)"
                log.info("webchat: %s %s -> %s motor=%s %d ms", method, shown, getattr(self, "_code", None) or "-",
                         getattr(self, "_engine", None) or "-", round((time.monotonic() - started) * 1000))

            def _internal(self, where: str, error: Exception):
                """Último recurso: un fallo que nadie esperaba se convierte en un 500 JSON con las cabeceras de
                seguridad, en vez de cortar la conexión (y de que la biblioteca vuelque la traza con la IP)."""
                _log_internal(where, error)
                self.close_connection = True
                if self._code is None:   # si ya salió una respuesta, no se puede mandar otra encima
                    try:
                        self._reply(500, {"error": "interno"})
                    except OSError:
                        pass

            # --- rutas ---
            def do_GET(self):
                started, path = time.monotonic(), self.path.split("?", 1)[0]
                # La misma conexión puede traer varias peticiones: lo de la anterior no cuenta.
                self._code, self._engine = None, None
                try:
                    if path == "/api/chat/health":
                        # Los motores se dicen por nombre, sin tocarlos: es un chequeo barato y público.
                        engines = [n for n, e in (("workers-ai", getattr(web.answerer, "_workers_ai", None)),
                                                  ("local", getattr(web.answerer, "_local", None)))
                                   if e is not None]
                        return self._reply(200, {"ok": True, "engines": engines})
                    return self._reply(404, {"error": "no existe"})
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except Exception as e:  # noqa: BLE001 - ver _internal
                    self._internal("GET", e)
                finally:
                    self._done("GET", path, started)

            def do_POST(self):
                started, path = time.monotonic(), self.path.split("?", 1)[0]
                self._code, self._engine = None, None
                try:
                    if path not in ROUTES[:3]:
                        self.close_connection = True
                        return self._reply(404, {"error": "no existe"})
                    # Origen antes que nada, también antes de leer el cuerpo: una web ajena no llega ni a gastar
                    # el Turnstile ni los límites de la visita. (Un `curl` puede ponerlo a mano: no es una
                    # autenticación, solo frena a otras páginas en el navegador de la gente.)
                    if self.headers.get("Origin") != web.origin:
                        self.close_connection = True
                        return self._reply(403, {"error": "origen"})
                    body = self._json_body()
                    if body is None:
                        return
                    if path == "/api/chat/session":
                        return self._session(body)
                    if path == "/api/chat":
                        return self._chat(body)
                    return self._contact(body)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except Exception as e:  # noqa: BLE001 - ver _internal
                    self._internal(path if path in ROUTES else "(otra)", e)
                finally:
                    self._done("POST", path, started)

            def _session(self, body: dict):
                ip = self._ip()
                # Primero el límite: así no se puede usar este servidor para gastar consultas a Cloudflare.
                if not web.limiter.allow_session(ip):
                    return self._reply(429, TOO_MANY)
                ok = web.turnstile.verify(body.get("turnstile"), ip)
                if ok is None:
                    log.error("webchat: Turnstile no disponible")
                    return self._reply(503, {"error": "no disponible"})
                if not ok:
                    return self._reply(401, {"error": "robot"})
                token, expires = web.passes.issue(ip)
                return self._reply(200, {"token": token, "expires": expires})

            def _chat(self, body: dict):
                ip = self._ip()
                if not web.passes.check(body.get("token"), ip):
                    return self._reply(401, {"error": "pase"})
                message = body.get("message")
                if not _valid_history(body.get("history")) or not isinstance(message, str):
                    return self._reply(400, {"error": "peticion"})
                message = message.strip()
                if not 0 < len(message) <= MAX_TEXT:
                    return self._reply(400, {"error": "peticion"})
                # El límite va tras validar: una petición mal formada no gasta el cupo de la visita.
                if not web.limiter.allow_message(ip):
                    return self._reply(429, TOO_MANY)
                try:
                    answer = web.answerer.reply(body["history"], message, norm_lang(body.get("lang")))
                except Exception as e:  # noqa: BLE001 - un fallo inesperado no debe cortar la conexión
                    log.error("webchat: fallo al responder (%s)", type(e).__name__)  # solo el tipo, sin contenido
                    return self._reply(503, {"error": "no disponible"})
                return self._reply(200, {"reply": answer["reply"], "offer_contact": bool(answer["offer_contact"]),
                                         "engine": answer["engine"]}, engine=answer["engine"])

            def _contact(self, body: dict):
                ip = self._ip()
                if not web.passes.check(body.get("token"), ip):
                    return self._reply(401, {"error": "pase"})
                try:
                    contact = validate_contact(body)
                except ContactError as e:
                    return self._reply(400, {"error": e.field})
                if web.contacts is None:
                    # Sin registro de clientes configurado no hay dónde dejarlo: «no disponible» (la web muestra el
                    # correo de Marc), nunca un «recibido» que se pierde en silencio.
                    return self._reply(503, {"error": "no disponible"})
                if not web.limiter.allow_contact(ip):
                    return self._reply(429, TOO_MANY)
                try:
                    # (id, nuevo), o None si `clientes` no responde y ha quedado en la cola: para la visita es igual.
                    result = web.contacts.deliver(contact)
                except ClientesRejected as e:
                    return self._reply(400, {"error": e.field})
                except OSError as e:
                    # Ni entregado ni encolado (disco lleno, volumen de solo lectura…).
                    log.error("webchat: no se ha podido guardar un contacto (%s)", type(e).__name__)
                    return self._reply(503, {"error": "no disponible"})
                # El contacto ya está a salvo: si el aviso al móvil falla, la visita no debe notarlo.
                notify(web.notifier, result)
                return self._reply(200, {"ok": True})

        return Handler
