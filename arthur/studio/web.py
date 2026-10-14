"""La página de revisión de Creator Studio (paso 4): ver cada vídeo desde el móvil y Aprobar / Cambiar / Descartar.

Rutas (JSON salvo los ficheros):
  GET  /                         la página (sin nada secreto; para ver algo hay que entrar con una llave)
  GET  /studio.js, /studio.css   su código y estilo (nombres fijos: nada de rutas que vengan de la petición)
  POST /api/login {"key"}        guarda la llave en una cookie HttpOnly          POST /api/logout
  GET  /api/reviews              lista: primero lo que espera decisión
  GET  /api/reviews/<n>          detalle: título, descripción completa, etiquetas, canal, huella del vídeo
  GET  /media/<n>/video          el vídeo (admite «Range»: el móvil lo pide a trozos para poder adelantar)
  GET  /media/<n>/thumb          la miniatura
  GET  /media/<n>/download       el vídeo como descarga (para subirlo a mano a Instagram o TikTok desde el móvil)
  POST /api/reviews/<n>/approve  {"video_sha256": "<la del vídeo que has visto>", "confirm": "PUBLICAR" si hay dudas}
  POST /api/reviews/<n>/changes  {"text": "qué cambiar"}
  POST /api/reviews/<n>/discard
  POST /api/reviews/<n>/retry    vuelve a intentar una subida que falló (si el vídeo sigue siendo el mismo)
  GET  /api/pilot                el piloto automático: si hay un vídeo haciéndose y en qué paso va
  POST /api/pilot {"tema"}       «prepara un vídeo ahora» (lo pide ARTHUR desde el chat): deja un trabajo del piloto
                                 en la cola, que el servicio studio-piloto recoge al momento; «tema» es opcional

Al aprobar, el «publicador» (un hilo, un vídeo cada vez) lo sube a YouTube en privado (publish.py). Al arrancar
recoge lo que quedara aprobado sin subir (p. ej. si se reinició el servidor a mitad).

Seguridad (igual que la web de ARTHUR, api/core_api.py):
  - Llaves por aparato (security/devices.py) en <datos>/studio/devices.json: se dan de alta con
    `--studio-dispositivo-nuevo movil`. Solo hay huellas; una llave mala espera antes de contestar.
  - Con cookie, toda petición que cambia algo exige la cabecera X-Arthur y, si llega Origin, que sea este servidor.
  - Solo se sirven ficheros con nombre fijo de la carpeta del proyecto de esa revisión.
  - Pensado para la red privada (Tailscale), como el core: nunca abierto a internet sin otra puerta delante.
"""

from __future__ import annotations

import http.cookies
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..security.devices import DeviceRegistry
from . import autopilot
from .project import ScriptError, load_script, project_dir
from .review import LABELS, ReviewBook, ReviewError, metadata, outputs
from .store import studio_dir

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
             "/studio.js": ("studio.js", "text/javascript; charset=utf-8"),
             "/studio.css": ("studio.css", "text/css; charset=utf-8")}
WEB_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self'; media-src 'self'; object-src 'none'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
COOKIE = "studio_llave"
COOKIE_DAYS = 180
MAX_BODY = 8 * 1024
REQUEST_TIMEOUT = 15
BAD_KEY_DELAY = 0.5
CHUNK = 256 * 1024
ROUTE = re.compile(r"^/(api/reviews|media)/(\d{1,9})(?:/(approve|changes|discard|retry|video|thumb|download))?$")


def devices_file() -> Path:
    return studio_dir() / "devices.json"


class Publisher:
    """Un hilo que sube los vídeos aprobados de uno en uno (subir dos a la vez no gana nada y gasta más memoria)."""

    def __init__(self, book: ReviewBook, publish: Callable[[ReviewBook, int], object]) -> None:
        self.book, self._publish = book, publish
        self._queue: queue.Queue[int] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="studio-publicar", daemon=True)

    def start(self) -> None:
        for review in self.book.approved():
            self._queue.put(review.id)  # aprobado y sin subir: p. ej. se reinició el servidor a mitad
        self._thread.start()

    def enqueue(self, review_id: int) -> None:
        self._queue.put(review_id)

    def _loop(self) -> None:
        while True:
            review_id = self._queue.get()
            try:
                self._publish(self.book, review_id)
            except Exception as e:  # el motivo ya queda en la revisión («failed»); el hilo sigue para el siguiente
                log.warning("Studio: publicar #%d: %s", review_id, e)
            finally:
                self._queue.task_done()

    def join(self) -> None:
        self._queue.join()


class StudioWeb:
    def __init__(self, book: ReviewBook, devices: DeviceRegistry, host: str = "127.0.0.1", port: int = 8773,
                 publisher: Publisher | None = None) -> None:
        self.book = book
        self.devices = devices
        self.publisher = publisher
        self._pilot_lock = threading.Lock()
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
            server_version = "arthur-studio"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # sin el registro por defecto; se anota a mano lo que importa

            # --- autenticación (como api/core_api.py) ---
            def _device(self, changes: bool = False) -> str | None:
                header = self.headers.get("Authorization", "")
                token = header[7:].strip() if header.startswith("Bearer ") else None
                if token is None:
                    token = self._cookie_key()
                    if token is not None and changes and not self._from_our_page():
                        log.warning("studio-web: petición con cookie desde fuera de la página (%s)", self.path[:40])
                        return None
                    if token is None:
                        return None
                name = web.devices.check(token)
                if name is None:
                    log.warning("studio-web: llave no válida desde %s", self.client_address[0])
                    time.sleep(BAD_KEY_DELAY)
                return name

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
                    self._reply(415, {"error": "se espera application/json"})
                    return None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= MAX_BODY:
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

            # --- rutas ---
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in WEB_FILES:
                    name, kind = WEB_FILES[path]
                    return self._send_bytes(200, kind, (WEB_DIR / name).read_bytes(), WEB_HEADERS)
                device = self._device()
                if device is None:
                    return self._reply(401, {"error": "hace falta una llave"})
                if path == "/api/reviews":
                    return self._reply(200, {"device": device, "reviews": [_summary(r) for r in web.book.list()]})
                if path == "/api/pilot":
                    return self._reply(200, _pilot(web.book.store))
                match = ROUTE.match(path)
                if not match:
                    return self._reply(404, {"error": "no existe"})
                kind, number, action = match.group(1), int(match.group(2)), match.group(3)
                review = web.book.get(number)
                if review is None:
                    return self._reply(404, {"error": "ese vídeo no existe"})
                if kind == "api/reviews" and action is None:
                    return self._reply(200, _detail(web.book, review))
                if kind == "media" and action in ("video", "thumb", "download"):
                    video, thumb, _ = outputs(project_dir(review.project), review.kind)
                    if action == "download":
                        # Nombre hecho por nosotros (proyecto validado + tipo): nada que venga de la petición.
                        name = f"{review.project}-{review.kind}.mp4"
                        return self._file(video, "video/mp4", {"Content-Disposition": f'attachment; filename="{name}"'})
                    return self._file(video if action == "video" else thumb,
                                      "video/mp4" if action == "video" else "image/jpeg")
                return self._reply(404, {"error": "no existe"})

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/login":
                    return self._login()
                if path == "/api/logout":
                    return self._set_cookie(200, "", 0)
                device = self._device(changes=True)
                if device is None:
                    return self._reply(401, {"error": "hace falta una llave"})
                if path == "/api/pilot":
                    return self._pilot_now(device)
                match = ROUTE.match(path)
                if not match or match.group(1) != "api/reviews" or \
                        match.group(3) not in ("approve", "changes", "discard", "retry"):
                    return self._reply(404, {"error": "no existe"})
                number, action = int(match.group(2)), match.group(3)
                body = self._json_body() if action in ("approve", "changes") else {}
                if body is None:
                    return
                try:
                    if action == "approve":
                        review = web.book.approve(number, str(body.get("video_sha256", "")), device,
                                                  str(body.get("confirm", "")))
                    elif action == "changes":
                        review = web.book.request_changes(number, str(body.get("text", "")), device)
                    elif action == "retry":
                        review = web.book.retry(number, device)
                    else:
                        review = web.book.discard(number, device)
                except (ReviewError, ScriptError) as e:
                    return self._reply(409, {"error": str(e)})
                if review.status == "approved" and web.publisher is not None:
                    web.publisher.enqueue(review.id)
                return self._reply(200, _summary(review))

            def _pilot_now(self, device: str):
                body = self._json_body() if self.headers.get("Content-Length", "0") not in ("", "0") else {}
                if body is None:
                    return
                tema = body.get("tema", "")
                if not isinstance(tema, str) or len(tema) > 200:
                    return self._reply(400, {"error": "el tema debe ser un texto corto"})
                with web._pilot_lock:  # dos peticiones a la vez no pueden lanzar dos vídeos
                    busy = autopilot.running(web.book.store)
                    if busy is not None:
                        return self._reply(409, {"error": f"ya se está haciendo un vídeo (trabajo #{busy.id})",
                                                 **_pilot(web.book.store)})
                    job = web.book.store.create_job(autopilot.KIND, {"tema": " ".join(tema.split()), "por": device})
                log.info("studio-web: «%s» pide un vídeo del piloto ahora (trabajo #%d)", device, job.id)
                return self._reply(200, _pilot(web.book.store))

            # --- respuestas ---
            def _reply(self, code: int, payload: dict):
                self._send_bytes(code, "application/json; charset=utf-8",
                                 json.dumps(payload, ensure_ascii=False).encode(), {"Cache-Control": "no-store"})

            def _send_bytes(self, code: int, content_type: str, body: bytes, headers: dict):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path: Path, content_type: str, extra: dict | None = None):
                """Fichero con soporte de «Range»: Safari en el iPhone no reproduce un vídeo si no puede pedirlo a trozos."""
                try:
                    size = path.stat().st_size
                except OSError:
                    return self._reply(404, {"error": "el fichero no está"})
                start, end, code = 0, size - 1, 200
                wanted = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
                if wanted and (wanted.group(1) or wanted.group(2)):
                    if wanted.group(1):
                        start = int(wanted.group(1))
                        end = min(int(wanted.group(2)), size - 1) if wanted.group(2) else size - 1
                    else:  # «bytes=-500»: los últimos 500
                        start = max(0, size - int(wanted.group(2)))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, no-store")
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                with path.open("rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    try:
                        while left > 0:
                            block = f.read(min(CHUNK, left))
                            if not block:
                                break
                            self.wfile.write(block)
                            left -= len(block)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # el móvil corta en cuanto tiene bastante: es normal

            def _login(self):
                if not self._from_our_page():
                    return self._reply(403, {"error": "solo desde la página de Studio"})
                body = self._json_body()
                if body is None:
                    return
                key = body.get("key")
                device = web.devices.check(key.strip()) if isinstance(key, str) else None
                if device is None:
                    time.sleep(BAD_KEY_DELAY)
                    return self._reply(401, {"error": "llave no válida"})
                log.info("studio-web: «%s» entra", device)
                self._set_cookie(200, key.strip(), COOKIE_DAYS * 86400, {"device": device})

            def _set_cookie(self, code: int, value: str, max_age: int, payload: dict | None = None):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                body = json.dumps(payload or {"ok": True}, ensure_ascii=False).encode()
                self._send_bytes(code, "application/json; charset=utf-8", body, {
                    "Cache-Control": "no-store",
                    "Set-Cookie": f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}",
                })

        return Handler


def _pilot(store) -> dict:
    """Lo último del piloto: el trabajo en marcha (o el más reciente) y su paso actual."""
    jobs = [j for j in store.list_jobs(limit=20) if j.kind == autopilot.KIND]
    if not jobs:
        return {"job": None}
    job = jobs[0]
    steps = store.steps(job.id)
    return {"job": {"id": job.id, "status": job.status, "error": job.error, "tema": job.params.get("tema", ""),
                    "created_at": job.created_at.isoformat(timespec="minutes"),
                    "steps_done": [s.name for s in steps if s.status == "done"]}}


def _summary(review) -> dict:
    return {"id": review.id, "project": review.project, "kind": review.kind, "title": review.title,
            "channel": review.channel,
            "language": review.language, "status": review.status, "status_label": LABELS.get(review.status, review.status),
            "created_at": review.created_at, "decided_at": review.decided_at, "decided_by": review.decided_by,
            "feedback": review.feedback, "youtube_id": review.youtube_id, "error": review.error}


def _detail(book: ReviewBook, review) -> dict:
    data = _summary(review)
    data["video_sha256"] = review.video_sha256
    try:
        folder = project_dir(review.project)
        script = load_script(folder / "guion.toml")
        meta = metadata(script, book.store.assets(review.project), review.kind)
        control = json.loads(outputs(folder, review.kind)[2].read_text(encoding="utf-8"))
        data.update({"description": meta["description"], "tags": meta["tags"],
                     "duration": control.get("duracion"), "lufs": control.get("lufs"),
                     "sources": [f"{s.autor}, {s.obra}" for s in script.fuentes]})
    except (OSError, ValueError, ScriptError, ReviewError) as e:
        data["problem"] = f"No se pueden leer los textos del proyecto: {e}"
    # Lo que el revisor automático no pudo dar por bueno (guiones del piloto): Marc lo mira antes de aprobar.
    try:
        warnings = book.warnings(review.project)
    except ScriptError:
        warnings = None
    data["warnings"] = warnings or []
    data["autopilot"] = warnings is not None   # del piloto: se sube en público (y solo, a su hora, si no hay avisos)
    return data
