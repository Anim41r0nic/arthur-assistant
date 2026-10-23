"""La página del panel de vida: qué toca hoy (tareas, clases, correos) y el resumen de la mañana.

Rutas (JSON salvo los ficheros):
  GET  /                              la página (sin nada secreto; para ver algo hay que entrar con una llave)
  GET  /panel.js, /panel.css, /tokens.css, /componentes.css, /fonts/…, iconos y manifest   (nombres fijos)
  POST /api/login {"key"}             guarda la llave en una cookie HttpOnly      POST /api/logout
  GET  /api/today                     el día completo (today.py): tareas, progreso, ahora/siguiente, eventos, correos
                                      y `problems` (fuentes caídas)
  POST /api/tasks {"text"}            tarea nueva; «mañana», «el viernes», «el 20»… ponen la fecha (dates.py)
  POST /api/tasks/<n>/toggle          marcar o desmarcar una tarea propia o de Studio
  POST /api/obsidian/toggle {"file","line","text"}   marcar una casilla `- [ ] Yo: …` de la bóveda
  GET  /api/briefing                  el resumen de hoy guardado ({"briefing": null} si aún no hay)
  POST /api/briefing                  hacer otro resumen y guardarlo (sin mandar aviso al móvil)
  POST /api/cerebro/ask {"q"}         «Pregunta a tu cerebro»: se reenvía al cerebro con la llave del panel
  POST /api/cerebro/diario {…}        la nota «Hecho con IA» del portátil (solo el aparato «portatil»), reenviada
                                      al cerebro, que no tiene puerto fuera de la red de Docker
  GET  /api/revision                  la tarjeta «Cómo va hoy» (revision.py): hoy en vivo, la semana y el juego
  POST /api/revision/bloque {"fecha","clave","marca"}   ✓ hecho · ½ medio · ✗ no (o null: sin marcar)
  POST /api/revision/descanso {"activo"}   «Hoy descanso»      POST /api/revision/ocultar {"ocultar"}
  POST /api/revision/semana/leida     «Leído» en el resumen semanal (insignia «Mirar la semana»)
  GET  /api/revision/nota?fecha=&tipo=noche|semana   la nota Markdown para Obsidian (solo el aparato «portatil»)

Seguridad (igual que Apuntes, notes/web.py):
  - Llaves por aparato en <datos>/panel/devices.json. Una llave mala espera antes de contestar.
  - Con cookie, toda petición que cambia algo exige la cabecera X-Arthur y, si llega Origin, que sea este servidor;
    si no, 403. Con «Authorization: Bearer» (las herramientas del chat) no hace falta: otra web no puede ponerla.
  - Solo se sirven ficheros de una lista fija, nunca rutas que vengan de la petición. Obsidian valida la suya.
  - Pensado para una red privada: nunca abierto a internet sin otra puerta delante.
"""

from __future__ import annotations

import http.cookies
import json
import logging
import re
import threading
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..cerebro import relay
from ..security.devices import DeviceError, DeviceRegistry
from .dates import parse_task
from .sources import SourceError
from .sources.obsidian import LineChanged
from .juego import BADGE_IDS
from .revision import MAX_CLAVE
from .store import MARCAS, MAX_TITLE, PanelError, PanelStore, panel_dir
from .today import _tarea as task_json

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
_CSS, _JS, _FONT = "text/css; charset=utf-8", "text/javascript; charset=utf-8", "font/woff2"
WEB_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
             "/panel.js": ("panel.js", _JS),
             "/panel.css": ("panel.css", _CSS),
             "/tokens.css": ("tokens.css", _CSS),
             "/componentes.css": ("componentes.css", _CSS),
             "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
             "/arthur.svg": ("arthur.svg", "image/svg+xml"),
             "/arthur-180.png": ("arthur-180.png", "image/png"),
             "/arthur-512.png": ("arthur-512.png", "image/png"),
             "/fonts/ibm-plex-sans-400.woff2": ("fonts/ibm-plex-sans-400.woff2", _FONT),
             "/fonts/ibm-plex-sans-600.woff2": ("fonts/ibm-plex-sans-600.woff2", _FONT),
             "/fonts/jetbrains-mono-400.woff2": ("fonts/jetbrains-mono-400.woff2", _FONT),
             "/fonts/jetbrains-mono-700.woff2": ("fonts/jetbrains-mono-700.woff2", _FONT),
             # Las insignias de la revisión (SVG de Codex, rama codex/juego): una ruta fija por insignia
             **{f"/insignias/{b}.svg": (f"insignias/{b}.svg", "image/svg+xml") for b in BADGE_IDS}}
WEB_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
COOKIE = "panel_llave"
COOKIE_DAYS = 180
MAX_JSON = 8 * 1024          # aquí solo viajan textos cortos (una tarea, una casilla)
MAX_DIARIO_JSON = 256 * 1024  # el material del día (el cerebro admite 60 000 caracteres; con escapes, más bytes)
DIARIO_DEVICE = "portatil"    # el único aparato del panel que manda la nota diaria
MAX_TEXT = 2 * MAX_TITLE     # «pan mañana» se recorta luego; esto solo frena textos absurdos
MAX_PATH = 500
REQUEST_TIMEOUT = 30
BAD_KEY_DELAY = 0.5
TASK_ROUTE = re.compile(r"^/api/tasks/(\d{1,9})/toggle$")
CHANGED = "La nota ha cambiado en Obsidian; recarga"


def devices_file() -> Path:
    return panel_dir() / "devices.json"


def _madrid_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid"))


class PanelWeb:
    def __init__(self, store: PanelStore, devices: DeviceRegistry, today_fn: Callable[[], dict], vault,
                 briefing_fn: Callable[[], dict], *, host: str = "127.0.0.1", port: int = 8775,
                 now: Callable[[], datetime] = _madrid_now, sleep: Callable[[float], None] = time.sleep,
                 cerebro=None, revision=None) -> None:
        self.store = store
        self.revision = revision          # revision.Revision, o None (la tarjeta dice «no disponible»)
        self.cerebro = cerebro            # ServiceLink con el cerebro, o None (la caja contesta «no disponible»)
        self.devices = devices
        self.today_fn = today_fn          # () -> el día completo (build_today con sus fuentes)
        self.vault = vault                # ObsidianVault o None si no hay bóveda
        self.briefing_fn = briefing_fn    # () -> {"date","text","engine"}: rehace y guarda, sin aviso
        self.now = now
        self.sleep = sleep                # inyectable: los tests comprueban la espera sin esperar de verdad
        # Un resumen tarda (Workers AI, Ollama): si Marc pulsa dos veces, el segundo no lanza otro a la vez.
        self._briefing = threading.Lock()
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
            server_version = "arthur-panel"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # sin el registro por defecto (llevaría títulos de tareas en las rutas); se anota a mano

            # --- autenticación (como notes/web.py) ---
            def _device(self, changes: bool = False) -> tuple[str | None, int]:
                """(aparato, código de error). Con cookie y fuera de nuestra página: 403, sin mirar la llave."""
                header = self.headers.get("Authorization", "")
                token = header[7:].strip() if header.startswith("Bearer ") else None
                if token is None:
                    token = self._cookie_key()
                    if token is None:
                        return None, 401
                    if changes and not self._from_our_page():
                        log.warning("panel-web: petición con cookie desde fuera de la página")
                        return None, 403
                try:
                    name = web.devices.check(token)
                except DeviceError:
                    # devices.json estropeado: respuesta clara en vez de cortar la conexión con un traceback
                    log.error("panel-web: no se puede leer el registro de aparatos")
                    return None, 503
                if name is None:
                    log.warning("panel-web: llave no válida desde %s", self.client_address[0])
                    web.sleep(BAD_KEY_DELAY)
                    return None, 401
                return name, 200

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

            def _json_body(self, limit: int = MAX_JSON) -> dict | None:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self.close_connection = True
                    self._reply(415, {"error": "se espera application/json"})
                    return None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= limit:
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
                path = urllib.parse.urlsplit(self.path).path
                if path in WEB_FILES:
                    name, kind = WEB_FILES[path]
                    return self._send(200, kind, (WEB_DIR / name).read_bytes(), WEB_HEADERS)
                device, code = self._device()
                if device is None:
                    return self._reply(code, {"error": "no disponible" if code == 503 else "hace falta una llave"})
                if path == "/api/today":
                    try:
                        return self._reply(200, web.today_fn())
                    except Exception:  # noqa: BLE001 - un fallo inesperado no debe dejar la conexión colgada
                        log.exception("panel-web: no se ha podido juntar el día")
                        return self._reply(503, {"error": "No se ha podido cargar el día. Prueba otra vez en un rato."})
                if path == "/api/briefing":
                    return self._reply(200, {"briefing": web.store.last_briefing(web.now().date())})
                if path == "/api/revision":
                    return self._revision_live()
                if path == "/api/revision/nota":
                    return self._revision_note(device)
                return self._reply(404, {"error": "no existe"})

            # --- POST ---
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/login":
                    return self._login()
                if path == "/api/logout":
                    # Cerrar sesión cambia la cookie del aparato; exige la misma prueba de origen que entrar.
                    if not self._from_our_page():
                        self.close_connection = True
                        return self._reply(403, {"error": "solo desde la página del panel"})
                    return self._set_cookie(200, "", 0)
                device, code = self._device(changes=True)
                if device is None:
                    self.close_connection = True
                    return self._reply(code, {"error": {401: "hace falta una llave", 503: "no disponible"}
                                              .get(code, "solo desde la página del panel")})
                if path == "/api/tasks":
                    return self._add_task(device)
                if path == "/api/obsidian/toggle":
                    return self._obsidian(device)
                if path == "/api/briefing":
                    return self._redo_briefing(device)
                if path == "/api/cerebro/ask":
                    return self._cerebro_ask(device)
                if path == "/api/cerebro/diario":
                    return self._cerebro_diario(device)
                if path.startswith("/api/revision/"):
                    return self._revision_post(device, path)
                match = TASK_ROUTE.match(path)
                if match:
                    try:
                        task = web.store.toggle(int(match.group(1)))
                    except KeyError:
                        return self._reply(404, {"error": "esa tarea no existe"})
                    return self._reply(200, task_json(task))
                self.close_connection = True
                return self._reply(404, {"error": "no existe"})

            def _add_task(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                text = body.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                    return self._reply(400, {"error": f"Escribe la tarea (hasta {MAX_TITLE} caracteres)"})
                title, due = parse_task(" ".join(text.split()), web.now().date())
                try:
                    task = web.store.add_task(title, due)
                except PanelError as e:
                    return self._reply(400, {"error": str(e)})
                log.info("panel-web: «%s» añade la tarea #%d", device, task.id)
                return self._reply(200, task_json(task))

            def _obsidian(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                file, line, text = body.get("file"), body.get("line"), body.get("text")
                # Los tipos se comprueban aquí: ObsidianVault.toggle da por hecho que `text` es texto.
                if not isinstance(file, str) or not 0 < len(file) <= MAX_PATH or \
                        not isinstance(line, int) or isinstance(line, bool) or line < 1 or \
                        not isinstance(text, str) or len(text) > MAX_JSON:
                    return self._reply(400, {"error": "casilla no válida"})
                if web.vault is None:
                    return self._reply(409, {"error": "Obsidian no está configurado en el panel"})
                try:
                    box = web.vault.toggle(file, line, text)
                except LineChanged:
                    return self._reply(409, {"error": CHANGED})
                except SourceError as e:
                    return self._reply(400, {"error": str(e)})
                log.info("panel-web: «%s» marca una casilla de Obsidian", device)
                return self._reply(200, {"file": box.file, "line": box.line, "text": box.text, "done": box.done})

            def _redo_briefing(self, device: str):
                if not web._briefing.acquire(blocking=False):
                    return self._reply(409, {"error": "Ya se está escribiendo un resumen; espera un momento"})
                try:
                    log.info("panel-web: «%s» pide otro resumen", device)
                    return self._reply(200, {"briefing": web.briefing_fn()})
                except Exception:  # noqa: BLE001 - make_briefing ya cae a la plantilla; esto es lo imprevisto
                    log.exception("panel-web: no se ha podido rehacer el resumen")
                    return self._reply(503, {"error": "No se pudo hacer otro resumen. Vuelve a intentarlo en un rato."})
                finally:
                    web._briefing.release()

            def _cerebro_ask(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                try:
                    answer = relay.ask(web.cerebro, body)
                except relay.RelayError as e:
                    return self._reply(e.status, {"error": str(e)})
                log.info("panel-web: «%s» pregunta al cerebro", device)  # nunca la pregunta: es de Marc
                return self._reply(200, answer)

            def _cerebro_diario(self, device: str):
                if device != DIARIO_DEVICE:
                    self.close_connection = True
                    return self._reply(403, {"error": "solo el portátil manda la nota diaria"})
                body = self._json_body(MAX_DIARIO_JSON)
                if body is None:
                    return
                try:
                    return self._reply(200, relay.diario(web.cerebro, body))
                except relay.RelayError as e:
                    return self._reply(e.status, {"error": str(e)})

            # --- revisión ---
            def _revision_live(self):
                if web.revision is None:
                    return self._reply(503, {"error": "La revisión no está disponible"})
                try:
                    return self._reply(200, web.revision.live())
                except Exception:  # noqa: BLE001 - un fallo de la revisión no debe colgar la conexión
                    log.exception("panel-web: no se ha podido contar el día")
                    return self._reply(503, {"error": "No se ha podido contar el día. Prueba otra vez en un rato."})

            def _revision_note(self, device: str):
                if device != DIARIO_DEVICE:
                    return self._reply(403, {"error": "solo el portátil baja las notas de la revisión"})
                if web.revision is None:
                    return self._reply(503, {"error": "La revisión no está disponible"})
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                fecha, tipo = query.get("fecha", [""])[0], query.get("tipo", [""])[0]
                try:
                    day = date.fromisoformat(fecha) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha) else None
                except ValueError:
                    day = None
                if day is None or tipo not in ("noche", "semana"):
                    return self._reply(400, {"error": "fecha (AAAA-MM-DD) y tipo (noche o semana)"})
                note = web.revision.note(day, tipo)
                if note is None:
                    return self._reply(404, {"error": "ese día no hubo revisión"})
                return self._reply(200, note)

            def _revision_post(self, device: str, path: str):
                if web.revision is None:
                    self.close_connection = True
                    return self._reply(503, {"error": "La revisión no está disponible"})
                body = self._json_body()
                if body is None:
                    return
                rev = web.revision
                if path == "/api/revision/bloque":
                    fecha, clave, marca = body.get("fecha"), body.get("clave"), body.get("marca")
                    if not isinstance(fecha, str) or not isinstance(clave, str) or not 0 < len(clave) <= MAX_CLAVE \
                            or (marca is not None and marca not in MARCAS):
                        return self._reply(400, {"error": "bloque no válido"})
                    try:
                        out = rev.mark(fecha, clave, marca)
                    except PanelError as e:
                        return self._reply(400, {"error": str(e)})
                    except KeyError:
                        return self._reply(404, {"error": "ese bloque no está en el calendario"})
                    log.info("panel-web: «%s» marca un bloque de estudio", device)
                    return self._reply(200, out)
                if path == "/api/revision/descanso":
                    if not isinstance(body.get("activo"), bool):
                        return self._reply(400, {"error": "falta «activo» (true o false)"})
                    return self._reply(200, rev.pause(body["activo"]))
                if path == "/api/revision/ocultar":
                    if not isinstance(body.get("ocultar"), bool):
                        return self._reply(400, {"error": "falta «ocultar» (true o false)"})
                    return self._reply(200, rev.hide(body["ocultar"]))
                if path == "/api/revision/semana/leida":
                    if not rev.week_read():
                        return self._reply(404, {"error": "no hay resumen semanal que marcar"})
                    return self._reply(200, {"leida": True})
                return self._reply(404, {"error": "no existe"})

            def _login(self):
                if not self._from_our_page():
                    self.close_connection = True
                    return self._reply(403, {"error": "solo desde la página del panel"})
                body = self._json_body()
                if body is None:
                    return
                key = body.get("key")
                try:
                    device = web.devices.check(key.strip()) if isinstance(key, str) else None
                except DeviceError:
                    log.error("panel-web: no se puede leer el registro de aparatos")
                    return self._reply(503, {"error": "no disponible"})
                if device is None:
                    web.sleep(BAD_KEY_DELAY)
                    return self._reply(401, {"error": "llave no válida"})
                log.info("panel-web: «%s» entra", device)
                self._set_cookie(200, key.strip(), COOKIE_DAYS * 86400, {"device": device})

            # --- respuestas ---
            def _reply(self, code: int, payload: dict):
                self._send(code, "application/json; charset=utf-8",
                           json.dumps(payload, ensure_ascii=False).encode(), {"Cache-Control": "no-store"})

            def _send(self, code: int, content_type: str, body: bytes, headers: dict):
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
