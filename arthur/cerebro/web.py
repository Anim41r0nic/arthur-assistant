"""La API del cerebro: JSON y nada más (no tiene página propia).

Rutas:
  POST /api/ask {"q": str (1-500), "fuentes"?: [..]}   pregunta a las notas → {"answer","engine","found","citations"}
  POST /api/diario {"day","registro","memoria","commits"}   resume el material del día (diario.py) → {"markdown","engine"}
  GET  /api/estado                                        trozos, pendientes, presupuesto y estado de cada fuente

  Repaso (repaso/service.py; lo usa Apuntes, que hace de intermediario de su pestaña Repaso):
  GET  /api/repaso/hoy                                    la sesión de hoy, SIN respuestas
  GET  /api/repaso/libre[?carpeta=Asignatura/Tema]        sin carpeta: qué carpetas hay; con ella, sus preguntas
  POST /api/repaso/respuesta {"question_id", "respuesta"|"nota"}   corrige (test/vf/formula: el servidor) y apunta
  POST /api/repaso/mal {"question_id", "motivo" ≤ 300}   retira la pregunta para siempre
  GET|POST /api/repaso/ajustes {"asignaturas", "aviso": "HH:MM", "retencion"}
  GET  /api/repaso/resumen                                racha, pendientes y aciertos (también para el panel)

Quién puede entrar: solo con «Authorization: Bearer <llave>» de un aparato del registro del cerebro
(<datos>/cerebro/devices.json) y solo si ese aparato es uno de los suyos: `core` (el chat), `panel`, `apuntes` y
`portatil`. Cada ruta, además, solo para quien la usa: la nota diaria la manda el portátil (o el panel, que se la
reenvía, porque el cerebro no tiene puerto fuera de la red de Docker), preguntar es cosa del chat, el panel y
Apuntes, y el Repaso es de Apuntes (el panel solo lee el resumen para su línea de «Hoy»). Todo POST exige la
cabecera `X-Arthur: 1`.

No hay cookies ni página: el navegador nunca habla con el cerebro. El panel y Apuntes hacen de intermediarios.
El puerto solo existe dentro de la red de Docker, pero se comprueba todo igual, como si estuviera abierto.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..security.devices import DeviceError, DeviceRegistry
from . import diario as diario_mod
from .repaso.service import RepasoInputError
from .repaso.store import RepasoError

log = logging.getLogger(__name__)

DEVICES = ("core", "panel", "apuntes", "portatil")
ROUTES = {  # ruta → aparatos que la pueden usar
    ("GET", "/api/estado"): {"core", "panel", "apuntes", "portatil"},
    ("POST", "/api/ask"): {"core", "panel", "apuntes"},
    ("POST", "/api/diario"): {"portatil", "panel"},
    ("GET", "/api/repaso/hoy"): {"apuntes"},
    ("GET", "/api/repaso/libre"): {"apuntes"},
    ("POST", "/api/repaso/respuesta"): {"apuntes"},
    ("POST", "/api/repaso/mal"): {"apuntes"},
    ("GET", "/api/repaso/ajustes"): {"apuntes"},
    ("POST", "/api/repaso/ajustes"): {"apuntes"},
    ("GET", "/api/repaso/resumen"): {"apuntes", "panel"},
}
SOURCES = ("obsidian", "obsidian:cerebro", "obsidian:business-lab", "apuntes", "pdf")
MAX_QUESTION = 500
MAX_JSON = 8 * 1024             # una pregunta cabe de sobra
MAX_DIARIO_JSON = 256 * 1024    # 60 000 caracteres con tildes y escapes JSON (á) pueden pasar de 60 KB
REQUEST_TIMEOUT = 30
BAD_KEY_DELAY = 0.5


def build_estado(index, budget, reindexer, repaso_clock=None) -> dict:
    """Lo que dice GET /api/estado: lo indexado (por fuente), lo pendiente de significado, el presupuesto gastado,
    la última pasada y el último error de cada fuente. El panel lo usa para «Cerebro: Apuntes sin actualizar…»."""
    from .sources import pdf

    stats = index.stats()
    rx = reindexer.state()
    fuentes: dict[str, dict] = {}
    for name in sorted(set(stats["fuentes"]) | set(rx["fuentes"])):
        f = {"documentos": 0, "trozos": 0, "pendientes": 0, "ultima_ok": None, "error": None}
        f.update(stats["fuentes"].get(name, {}))
        st = rx["fuentes"].get(name, {})
        f.update(ultima_ok=st.get("ultima_ok"), error=st.get("error"))
        fuentes[name] = f
    out = {"documentos": stats["documentos"], "trozos": stats["trozos"], "pendientes": stats["pendientes"],
            "error_embeddings": stats["error_embeddings"], "gastado_hoy": round(budget.spent_today(), 2),
            "presupuesto": budget.daily, "ultima_pasada": rx["ultima_pasada"], "fuentes": fuentes,
            "pdf_sin_texto": list(pdf.skipped)[:50]}
    if repaso_clock is not None:
        out["repaso"] = repaso_clock.state()  # última noche de preguntas y estado del aviso de hoy
    return out


class CerebroWeb:
    def __init__(self, devices: DeviceRegistry, ask_fn: Callable[[str, tuple[str, ...] | None], dict],
                 estado_fn: Callable[[], dict], diario_fn: Callable[[dict], dict] | None = None, *,
                 repaso=None, host: str = "127.0.0.1", port: int = 8778, sleep: Callable[[float], None] = time.sleep) -> None:
        self.devices = devices
        self.ask_fn = ask_fn          # (pregunta, fuentes) -> respuesta con citas (answer.ask ya montado)
        self.estado_fn = estado_fn
        self.diario_fn = diario_fn    # (material comprobado) -> {"markdown","engine"}
        self.repaso = repaso          # repaso.service.Repaso, o None (entonces /api/repaso/* contesta 503)
        self.sleep = sleep            # inyectable: los tests comprueban la espera sin esperar de verdad
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
            server_version = "arthur-cerebro"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # el registro por defecto llevaría rutas con datos; se anota a mano lo que importa

            def _device(self, method: str, path: str) -> tuple[str | None, int]:
                """(aparato, código). 401 sin llave o con una que no vale; 403 si el aparato no puede usar la ruta."""
                header = self.headers.get("Authorization", "")
                token = header[7:].strip() if header.startswith("Bearer ") else None
                if not token:
                    return None, 401
                try:
                    name = web.devices.check(token)
                except DeviceError:
                    log.error("cerebro-web: no se puede leer el registro de aparatos")
                    return None, 503
                if name is None:
                    log.warning("cerebro-web: llave no válida desde %s", self.client_address[0])
                    web.sleep(BAD_KEY_DELAY)
                    return None, 401
                if name not in DEVICES or name not in ROUTES[(method, path)]:
                    log.warning("cerebro-web: «%s» no puede usar %s %s", name, method, path)
                    return None, 403
                return name, 200

            def _json_body(self, limit: int) -> dict | None:
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

            def _route(self, method: str) -> tuple[str | None, str]:
                path = urllib.parse.urlsplit(self.path).path
                if (method, path) not in ROUTES:
                    self.close_connection = method == "POST"
                    self._reply(404, {"error": "no existe"})
                    return None, path
                if method == "POST" and self.headers.get("X-Arthur") != "1":
                    self.close_connection = True
                    self._reply(403, {"error": "falta la cabecera X-Arthur"})
                    return None, path
                device, code = self._device(method, path)
                if device is None:
                    self.close_connection = method == "POST"
                    self._reply(code, {"error": {401: "hace falta una llave", 403: "este aparato no puede",
                                                 503: "no disponible"}[code]})
                return device, path

            def do_GET(self):
                device, path = self._route("GET")
                if device is None:
                    return
                if path.startswith("/api/repaso/"):
                    return self._repaso("GET", path, None)
                try:
                    return self._reply(200, web.estado_fn())
                except Exception:  # noqa: BLE001
                    log.exception("cerebro-web: no se ha podido juntar el estado")
                    return self._reply(503, {"error": "no disponible"})

            def do_POST(self):
                device, path = self._route("POST")
                if device is None:
                    return
                if path == "/api/ask":
                    return self._ask(device)
                if path.startswith("/api/repaso/"):
                    body = self._json_body(MAX_JSON)
                    if body is None:
                        return
                    return self._repaso("POST", path, body)
                return self._diario(device)

            def _repaso(self, method: str, path: str, body: dict | None):
                r = web.repaso
                if r is None:
                    return self._reply(503, {"error": "el Repaso no está disponible"})
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, max_num_fields=5)
                handlers = {
                    ("GET", "/api/repaso/hoy"): lambda: r.today(),
                    ("GET", "/api/repaso/libre"): lambda: r.free((query.get("carpeta") or [None])[0]),
                    ("POST", "/api/repaso/respuesta"): lambda: r.answer(body),
                    ("POST", "/api/repaso/mal"): lambda: r.report(body),
                    ("GET", "/api/repaso/ajustes"): lambda: r.settings_view(),
                    ("POST", "/api/repaso/ajustes"): lambda: r.save_settings(body),
                    ("GET", "/api/repaso/resumen"): lambda: r.summary(),
                }
                try:
                    return self._reply(200, handlers[(method, path)]())
                except RepasoInputError as e:
                    return self._reply(400, {"error": str(e)})
                except KeyError:
                    return self._reply(404, {"error": "Esa pregunta no existe"})
                except RepasoError as e:  # p. ej. contestar una pregunta ya retirada
                    return self._reply(409, {"error": str(e)})
                except ValueError:  # parse_qs con demasiados campos
                    return self._reply(400, {"error": "petición no válida"})
                except Exception:  # noqa: BLE001
                    log.exception("cerebro-web: el Repaso ha fallado (%s %s)", method, path)
                    return self._reply(503, {"error": "El Repaso no está disponible ahora. Prueba en un rato."})

            def _ask(self, device: str):
                body = self._json_body(MAX_JSON)
                if body is None:
                    return
                q = body.get("q")
                if not isinstance(q, str) or not q.strip() or len(q) > MAX_QUESTION:
                    return self._reply(400, {"error": f"Escribe la pregunta (hasta {MAX_QUESTION} caracteres)"})
                sources = body.get("fuentes")
                if sources is not None:
                    if not isinstance(sources, list) or not sources or \
                            not all(isinstance(s, str) and s in SOURCES for s in sources):
                        return self._reply(400, {"error": "«fuentes» no válidas (" + ", ".join(SOURCES) + ")"})
                    sources = tuple(dict.fromkeys(sources))
                # Ni la pregunta ni la respuesta van al registro: son de Marc.
                log.info("cerebro-web: «%s» pregunta", device)
                try:
                    return self._reply(200, web.ask_fn(" ".join(q.split()), sources))
                except Exception:  # noqa: BLE001 — answer.ask ya cae a los trozos; esto es lo imprevisto
                    log.exception("cerebro-web: la pregunta ha fallado")
                    return self._reply(503, {"error": "El cerebro no ha podido contestar ahora. Prueba en un rato."})

            def _diario(self, device: str):
                if web.diario_fn is None:
                    return self._reply(503, {"error": "la nota diaria no está disponible"})
                body = self._json_body(MAX_DIARIO_JSON)
                if body is None:
                    return
                try:
                    material = diario_mod.validate(body)
                except ValueError as e:
                    return self._reply(400, {"error": str(e)})
                log.info("cerebro-web: «%s» manda el material del %s", device, material["day"])
                try:
                    return self._reply(200, web.diario_fn(material))
                except Exception:  # noqa: BLE001
                    log.exception("cerebro-web: la nota diaria ha fallado")
                    return self._reply(503, {"error": "No se ha podido resumir el día. Se reintenta mañana."})

            def _reply(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

        return Handler
