"""La página de apuntes: subir fotos o PDF desde el móvil o el portátil, ver cómo se leen, retocar el texto y
descargar el PDF.

Rutas (JSON salvo los ficheros):
  GET  /                              la página (sin nada secreto; para ver algo hay que entrar con una llave)
  GET  /apuntes.js, /apuntes.css      su código y estilo (nombres fijos)
  GET  /manifest.webmanifest, /icono.svg, /icono-192.png, /icono-512.png   para anclarla en el móvil como una app
  POST /api/login {"key"}             guarda la llave en una cookie HttpOnly      POST /api/logout
  GET  /api/notes?carpeta=N           lo que hay en la carpeta N (0 = principal): ruta, subcarpetas y apuntes,
                                      más qué motores hay
  GET  /api/folders                   todas las carpetas con su ruta (para «Mover a…» y «Nuevo apunte en…»)
  POST /api/folders {"parent","name"}         carpeta nueva       POST /api/folders/<n>/rename {"name"}
  POST /api/folders/<n>/delete        solo si está vacía
  POST /api/notes {"title","engine","lang","folder"}   nota nueva (en borrador)
  GET  /api/notes/<n>                 detalle con el texto
  POST /api/notes/<n>/upload          el CUERPO es el fichero (image/jpeg, image/png o application/pdf), uno por
                                      petición: así no hace falta leer «multipart» (la biblioteca estándar ya no trae
                                      con qué) y el móvil sube las fotos de una en una con su barra de progreso
  POST /api/notes/<n>/start           a leer (también para reintentar tras un error, o releer con otro motor)
  POST /api/notes/<n>/text {"markdown"}       guardar el texto retocado
  POST /api/notes/<n>/scan            (re)hacer el PDF escaneado
  POST /api/notes/<n>/move {"folder"}
  POST /api/notes/<n>/delete
  GET  /files/<n>/original/<k>        el fichero k tal como se subió (para verlo en el navegador)
  GET  /files/<n>/page/<p>            la imagen de la página p (para comparar con el texto)
  GET  /files/<n>/limpio?estilo=X     el PDF limpio (se hace al momento con Typst, ~1 s); con &ver=1 se abre en el
                                      navegador en vez de descargarse
  GET  /files/<n>/escaneo             el PDF escaneado con texto buscable
  GET  /files/<n>/texto               el texto en Markdown (.md)
  GET  /api/ask/notes?carpeta=N       los apuntes ya leídos de esa carpeta y las de dentro (para marcarlos)
  POST /api/ask {"mode","question","notes"}   «Preguntar a la IA» (tutor.py): explica / ejemplos / libre, con el
                                      texto de los apuntes marcados; devuelve {"answer"} en Markdown con LaTeX
  POST /api/ask/save {"title","markdown","folder","lang"}   guardar la respuesta como un apunte (ya «listo»)

Seguridad (como la página del Studio, studio/web.py):
  - Llaves por aparato en <datos>/notes/devices.json (`--apuntes-dispositivo-nuevo movil`). Una llave mala espera.
  - Con cookie, toda petición que cambia algo exige la cabecera X-Arthur y, si llega Origin, que sea este servidor.
  - Los ficheros subidos se comprueban por su contenido (no por el nombre) y se guardan con nombre propio.
  - Solo se sirven ficheros con nombre hecho por el programa, nunca rutas que vengan de la petición.
  - Pensado para la red privada (Tailscale): nunca abierto a internet sin otra puerta delante.
"""

from __future__ import annotations

import http.cookies
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..security.devices import DeviceRegistry
from . import render, tutor
from .engines import ENGINES, NAMES, EngineError
from .store import LABELS, MAX_MARKDOWN, Folder, Note, NoteError, NotesStore, notes_dir
from .worker import Worker

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
             "/apuntes.js": ("apuntes.js", "text/javascript; charset=utf-8"),
             "/apuntes.css": ("apuntes.css", "text/css; charset=utf-8"),
             "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
             "/icono.svg": ("icono.svg", "image/svg+xml"),
             "/icono-192.png": ("icono-192.png", "image/png"),
             "/icono-512.png": ("icono-512.png", "image/png")}
WEB_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
COOKIE = "apuntes_llave"
COOKIE_DAYS = 180
MAX_JSON = 2 * MAX_MARKDOWN + 4096   # el texto entero va en JSON (escapado puede ocupar más)
MAX_UPLOAD = 40 * 1024 * 1024        # 40 MB por fichero (una foto del móvil son 2-5 MB)
MAX_FILES = 80
REQUEST_TIMEOUT = 60
BAD_KEY_DELAY = 0.5
CHUNK = 256 * 1024
LANGS = ("es", "ca", "en")
ROUTE = re.compile(r"^/(api/notes|files)/(\d{1,9})(?:/(upload|start|text|scan|move|delete|original|page|limpio|"
                   r"escaneo|texto)(?:/(\d{1,4}))?)?$")
FOLDER_ROUTE = re.compile(r"^/api/folders/(\d{1,9})/(rename|delete)$")
# Lo que hay al principio de cada tipo de fichero admitido (no nos fiamos del nombre ni del Content-Type).
MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"%PDF-", ".pdf"))


def devices_file() -> Path:
    return notes_dir() / "devices.json"


def sniff(head: bytes) -> str | None:
    return next((suffix for magic, suffix in MAGIC if head.startswith(magic)), None)


def download_name(title: str, suffix: str) -> str:
    """Nombre de descarga hecho por nosotros: letras sin tildes, números y guiones (nada que rompa la cabecera)."""
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", plain).strip("-").lower()[:60] or "apuntes"
    return f"{slug}{suffix}"


def _attachment(name: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store"}


class NotesWeb:
    def __init__(self, store: NotesStore, devices: DeviceRegistry, worker: Worker,
                 host: str = "127.0.0.1", port: int = 8774, *, gemini_ready=lambda: True, tutor_engine=None) -> None:
        self.store = store
        self.devices = devices
        self.worker = worker
        self.gemini_ready = gemini_ready
        self.tutor_engine = tutor_engine        # () -> algo con .ask(prompt); None = sin «Preguntar a la IA»
        self._asks = threading.Semaphore(2)     # el cupo gratis de Gemini es por minuto: dos preguntas a la vez
        self._renders = threading.Semaphore(2)  # Typst usa varios núcleos: dos PDF a la vez como mucho
        self._uploads = threading.Lock()        # dos subidas a la misma nota no pueden coger el mismo número
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

    def summary(self, note: Note) -> dict:
        return {"id": note.id, "title": note.title, "engine": note.engine, "engine_label": NAMES.get(note.engine, note.engine),
                "lang": note.lang, "status": note.status, "folder": note.folder_id,
                "status_label": LABELS.get(note.status, note.status), "error": note.error, "pages": note.pages,
                "done": note.done, "files": len(self.store.sources(note.id)), "edited": note.edited,
                "scan": note.scan, "scan_error": note.scan_error,
                "created_at": note.created_at, "updated_at": note.updated_at}

    def detail(self, note: Note) -> dict:
        data = self.summary(note)
        data["markdown"] = note.markdown
        data["path"] = [self.folder_info(f) for f in self.store.path(note.folder_id)]
        data["originals"] = [{"n": k, "kind": "pdf" if s.suffix == ".pdf" else "imagen"}
                             for k, s in enumerate(self.store.sources(note.id), start=1)]
        return data

    def folder_info(self, folder: Folder) -> dict:
        subs, notes = self.store.counts(folder.id)
        return {"id": folder.id, "name": folder.name, "folders": subs, "notes": notes}

    def listing(self, folder_id: int) -> dict:
        folder = self.store.require_folder(folder_id)
        return {"folder": self.folder_info(folder), "path": [self.folder_info(f) for f in self.store.path(folder_id)],
                "folders": [self.folder_info(f) for f in self.store.folders(folder_id)],
                "notes": [self.summary(n) for n in self.store.list(folder_id)],
                "engines": {"gemini": self.gemini_ready(), "local": True}, "scans": self.worker.scans_enabled,
                "tutor": self.tutor_engine is not None and self.gemini_ready(),
                "styles": list(render.STYLES)}

    def _make_handler(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT
            server_version = "arthur-apuntes"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # sin el registro por defecto (llevaría los títulos de los apuntes); se anota a mano

            # --- autenticación (como studio/web.py) ---
            def _device(self, changes: bool = False) -> str | None:
                header = self.headers.get("Authorization", "")
                token = header[7:].strip() if header.startswith("Bearer ") else None
                if token is None:
                    token = self._cookie_key()
                    if token is None:
                        return None
                    if changes and not self._from_our_page():
                        log.warning("apuntes-web: petición con cookie desde fuera de la página")
                        return None
                name = web.devices.check(token)
                if name is None:
                    log.warning("apuntes-web: llave no válida desde %s", self.client_address[0])
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

            def _length(self, limit: int) -> int | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= limit:
                    self.close_connection = True  # no se lee el cuerpo: se corta la conexión
                    self._reply(413, {"error": "fichero vacío o demasiado grande"})
                    return None
                return length

            def _json_body(self) -> dict | None:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self.close_connection = True
                    self._reply(415, {"error": "se espera application/json"})
                    return None
                length = self._length(MAX_JSON)
                if length is None:
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
                url = urllib.parse.urlsplit(self.path)
                if url.path in WEB_FILES:
                    name, kind = WEB_FILES[url.path]
                    return self._send(200, kind, (WEB_DIR / name).read_bytes(), WEB_HEADERS)
                if self._device() is None:
                    return self._reply(401, {"error": "hace falta una llave"})
                if url.path == "/api/notes":
                    folder = urllib.parse.parse_qs(url.query).get("carpeta", ["0"])[0]
                    if not folder.isdigit() or len(folder) > 9:
                        return self._reply(400, {"error": "carpeta no válida"})
                    try:
                        return self._reply(200, web.listing(int(folder)))
                    except NoteError as e:
                        return self._reply(404, {"error": str(e)})
                if url.path == "/api/ask/notes":
                    folder = urllib.parse.parse_qs(url.query).get("carpeta", ["0"])[0]
                    if not folder.isdigit() or len(folder) > 9:
                        return self._reply(400, {"error": "carpeta no válida"})
                    try:
                        found = web.store.ready_under(int(folder))
                    except NoteError as e:
                        return self._reply(404, {"error": str(e)})
                    return self._reply(200, {"notes": [{"id": n.id, "title": n.title, "path": path,
                                                        "chars": len(n.markdown)} for n, path in found]})
                if url.path == "/api/folders":
                    return self._reply(200, {"folders": [{"id": 0, "path": "Apuntes (principal)"}] + [
                        {"id": f.id, "path": path} for f, path in web.store.all_folders()]})
                match = ROUTE.match(url.path)
                if not match:
                    return self._reply(404, {"error": "no existe"})
                kind, note_id, action, extra = match.group(1), int(match.group(2)), match.group(3), match.group(4)
                note = web.store.get(note_id)
                if note is None:
                    return self._reply(404, {"error": "esa nota no existe"})
                if kind == "api/notes" and action is None:
                    return self._reply(200, web.detail(note))
                if kind != "files":
                    return self._reply(404, {"error": "no existe"})
                if action == "original" and extra:
                    sources = web.store.sources(note_id)
                    k = int(extra)
                    if not 1 <= k <= len(sources):
                        return self._reply(404, {"error": "ese fichero no existe"})
                    src = sources[k - 1]
                    kind = {".pdf": "application/pdf", ".png": "image/png"}.get(src.suffix, "image/jpeg")
                    name = download_name(note.title, f"-{k}{src.suffix}" if len(sources) > 1 else src.suffix)
                    return self._file(src, kind, {"Content-Disposition": f'inline; filename="{name}"'})
                if action == "page" and extra:
                    image = web.store.page_image(note_id, int(extra))
                    if image is None:
                        return self._reply(404, {"error": "esa página no existe"})
                    return self._file(image, "image/png" if image.suffix == ".png" else "image/jpeg")
                if action == "texto":
                    return self._send(200, "text/markdown; charset=utf-8", note.markdown.encode("utf-8"),
                                      _attachment(download_name(note.title, ".md")))
                if action == "escaneo":
                    pdf = web.store.folder(note_id) / "out" / "escaneo.pdf"
                    return self._file(pdf, "application/pdf", _attachment(download_name(note.title, "-escaneo.pdf")))
                if action == "limpio":
                    style = urllib.parse.parse_qs(url.query).get("estilo", [render.DEFAULT_STYLE])[0]
                    if style not in render.STYLES:
                        return self._reply(400, {"error": "estilo desconocido"})
                    if not note.markdown.strip():
                        return self._reply(409, {"error": "todavía no hay texto"})
                    with web._renders:
                        try:
                            pdf = render.clean_pdf(note.markdown, note.title, style=style, lang=note.lang)
                        except render.RenderError as e:
                            log.warning("apuntes-web: PDF limpio #%d: %s", note_id, e)
                            return self._reply(422, {"error": str(e)})
                    name = download_name(note.title, f"-{style}.pdf")
                    if urllib.parse.parse_qs(url.query).get("ver") == ["1"]:
                        return self._send(200, "application/pdf", pdf, {
                            "Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "private, no-store"})
                    return self._send(200, "application/pdf", pdf, _attachment(name))
                return self._reply(404, {"error": "no existe"})

            # --- POST ---
            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/login":
                    return self._login()
                if path == "/api/logout":
                    return self._set_cookie(200, "", 0)
                device = self._device(changes=True)
                if device is None:
                    self.close_connection = True
                    return self._reply(401, {"error": "hace falta una llave"})
                if path == "/api/notes":
                    return self._create()
                if path == "/api/ask":
                    return self._ask(device)
                if path == "/api/ask/save":
                    return self._save_answer(device)
                if path == "/api/folders" or FOLDER_ROUTE.match(path):
                    return self._folders(path, device)
                match = ROUTE.match(path)
                if not match or match.group(1) != "api/notes" or match.group(4) or \
                        match.group(3) not in ("upload", "start", "text", "scan", "move", "delete"):
                    self.close_connection = True
                    return self._reply(404, {"error": "no existe"})
                note_id, action = int(match.group(2)), match.group(3)
                try:
                    note = web.store.require(note_id)
                    if action == "upload":
                        return self._upload(note)
                    if action == "start":
                        if note.status in ("en_cola", "leyendo"):
                            return self._reply(409, {"error": "ya se está leyendo"})
                        if not web.store.sources(note_id):
                            return self._reply(409, {"error": "primero sube alguna foto o PDF"})
                        body = self._optional_json()
                        if body is None:
                            return
                        engine = body.get("engine", note.engine)
                        if engine not in ENGINES:
                            return self._reply(400, {"error": "motor no válido"})
                        if note.status == "listo" or engine != note.engine:
                            web.store.set_pages(note_id, [])  # releer desde cero (p. ej. con el otro motor)
                        web.store.update(note_id, engine=engine)
                        web.worker.read(note_id)
                        log.info("apuntes-web: «%s» manda leer la nota #%d (%s)", device, note_id, engine)
                    elif action == "text":
                        body = self._json_body()
                        if body is None:
                            return
                        text = body.get("markdown")
                        if not isinstance(text, str) or len(text) > MAX_MARKDOWN:
                            return self._reply(400, {"error": "texto no válido o demasiado largo"})
                        if note.status in ("en_cola", "leyendo"):
                            return self._reply(409, {"error": "espera a que termine de leer"})
                        fields: dict = {"markdown": text, "edited": 1}
                        title = body.get("title")
                        if isinstance(title, str) and title.strip():
                            fields["title"] = " ".join(title.split())[:120]
                        web.store.update(note_id, **fields)
                    elif action == "scan":
                        if not web.worker.scans_enabled:
                            return self._reply(409, {"error": "el escaneo no está instalado en este servidor"})
                        if note.scan in ("en_cola", "haciendo"):
                            return self._reply(409, {"error": "ya se está haciendo"})
                        if not web.store.sources(note_id):
                            return self._reply(409, {"error": "primero sube alguna foto o PDF"})
                        web.worker.scan(note_id)
                    elif action == "move":
                        body = self._json_body()
                        if body is None:
                            return
                        folder = body.get("folder")
                        if not isinstance(folder, int) or isinstance(folder, bool):
                            return self._reply(400, {"error": "carpeta no válida"})
                        web.store.move(note_id, folder)
                    elif action == "delete":
                        if note.status in ("en_cola", "leyendo") or note.scan in ("en_cola", "haciendo"):
                            return self._reply(409, {"error": "espera a que termine antes de borrarla"})
                        web.store.delete(note_id)
                        log.info("apuntes-web: «%s» borra la nota #%d", device, note_id)
                        return self._reply(200, {"ok": True})
                except NoteError as e:
                    self.close_connection = True
                    return self._reply(404, {"error": str(e)})
                return self._reply(200, web.detail(web.store.require(note_id)))

            def _optional_json(self) -> dict | None:
                if self.headers.get("Content-Length", "0") in ("", "0"):
                    return {}
                return self._json_body()

            def _create(self):
                body = self._json_body()
                if body is None:
                    return
                engine = body.get("engine", "auto")
                lang = body.get("lang", "es")
                folder = body.get("folder", 0)
                if engine not in ENGINES or lang not in LANGS:
                    return self._reply(400, {"error": "motor o idioma no válido"})
                if not isinstance(folder, int) or isinstance(folder, bool):
                    return self._reply(400, {"error": "carpeta no válida"})
                try:
                    note = web.store.create(str(body.get("title", "")), engine, lang, folder)
                except NoteError as e:
                    return self._reply(404, {"error": str(e)})
                return self._reply(200, web.detail(note))

            def _ask(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                if web.tutor_engine is None or not web.gemini_ready():
                    return self._reply(409, {"error": "Falta la clave de Gemini en el servidor"})
                ids = body.get("notes", [])
                if not isinstance(ids, list) or len(ids) > tutor.MAX_NOTES or \
                        not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
                    return self._reply(400, {"error": f"marca como mucho {tutor.MAX_NOTES} apuntes"})
                material = []
                for note_id in dict.fromkeys(ids):
                    note = web.store.get(note_id)
                    if note is None or note.status != "listo":
                        return self._reply(404, {"error": "algún apunte marcado ya no existe o no está leído"})
                    where = " › ".join(f.name for f in web.store.path(note.folder_id))
                    material.append(tutor.Material(note.title, where, note.markdown))
                question, mode = body.get("question"), body.get("mode", "libre")
                try:
                    prompt = tutor.build_prompt(mode if isinstance(mode, str) else "",
                                                question if isinstance(question, str) else "", material)
                except tutor.TutorError as e:
                    return self._reply(400, {"error": str(e)})
                log.info("apuntes-web: «%s» pregunta a la IA (%s, %d apuntes)", device, mode, len(material))
                with web._asks:
                    try:
                        answer = web.tutor_engine().ask(prompt)
                    except EngineError as e:
                        # los motores dicen «se reintentará» pensando en leer páginas; aquí no reintenta nadie
                        message = str(e).replace(": se reintentará", ": prueba otra vez en un rato")
                        return self._reply(503, {"error": message})
                return self._reply(200, {"answer": answer})

            def _save_answer(self, device: str):
                body = self._json_body()
                if body is None:
                    return
                text, folder, lang = body.get("markdown"), body.get("folder", 0), body.get("lang", "es")
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_MARKDOWN:
                    return self._reply(400, {"error": "no hay respuesta que guardar"})
                if not isinstance(folder, int) or isinstance(folder, bool) or lang not in LANGS:
                    return self._reply(400, {"error": "carpeta o idioma no válido"})
                try:
                    note = web.store.create(str(body.get("title", "")) or "Respuesta de la IA", "gemini", lang, folder)
                except NoteError as e:
                    return self._reply(404, {"error": str(e)})
                note = web.store.update(note.id, status="listo", markdown=text)
                log.info("apuntes-web: «%s» guarda una respuesta de la IA como la nota #%d", device, note.id)
                return self._reply(200, web.detail(note))

            def _folders(self, path: str, device: str):
                body = self._optional_json()
                if body is None:
                    return
                try:
                    if path == "/api/folders":
                        parent = body.get("parent", 0)
                        if not isinstance(parent, int) or isinstance(parent, bool):
                            return self._reply(400, {"error": "carpeta no válida"})
                        folder = web.store.create_folder(parent, str(body.get("name", "")))
                        return self._reply(200, web.folder_info(folder))
                    match = FOLDER_ROUTE.match(path)
                    folder_id, action = int(match.group(1)), match.group(2)
                    if action == "rename":
                        return self._reply(200, web.folder_info(web.store.rename_folder(folder_id,
                                                                                      str(body.get("name", "")))))
                    parent = web.store.require_folder(folder_id).parent_id
                    web.store.delete_folder(folder_id)
                    log.info("apuntes-web: «%s» borra la carpeta #%d", device, folder_id)
                    return self._reply(200, {"ok": True, "parent": parent})
                except NoteError as e:
                    return self._reply(409 if "existe" not in str(e) else 404, {"error": str(e)})

            def _upload(self, note: Note):
                if note.status != "borrador":
                    self.close_connection = True
                    return self._reply(409, {"error": "esta nota ya se ha leído: crea otra para más fotos"})
                length = self._length(MAX_UPLOAD)
                if length is None:
                    return
                with web._uploads:
                    if len(web.store.sources(note.id)) >= MAX_FILES:
                        self.close_connection = True
                        return self._reply(409, {"error": f"como mucho {MAX_FILES} ficheros por nota"})
                    head = self.rfile.read(min(16, length))
                    suffix = sniff(head)
                    if suffix is None:
                        self.close_connection = True  # no se lee el resto: se corta
                        return self._reply(415, {"error": "solo fotos JPEG o PNG y PDF (en el iPhone: Ajustes > "
                                                          "Cámara > Formatos > Más compatible)"})
                    target = web.store.next_source(note.id, suffix)
                    partial = target.with_suffix(".part")
                    try:
                        with partial.open("wb") as f:
                            f.write(head)
                            left = length - len(head)
                            while left > 0:
                                block = self.rfile.read(min(CHUNK, left))
                                if not block:
                                    raise ConnectionError("subida cortada")
                                f.write(block)
                                left -= len(block)
                        os.replace(partial, target)
                    except (OSError, ConnectionError):
                        partial.unlink(missing_ok=True)
                        self.close_connection = True
                        return self._reply(400, {"error": "la subida se ha cortado: prueba otra vez"})
                return self._reply(200, web.summary(web.store.require(note.id)))

            def _login(self):
                if not self._from_our_page():
                    self.close_connection = True
                    return self._reply(403, {"error": "solo desde la página de apuntes"})
                body = self._json_body()
                if body is None:
                    return
                key = body.get("key")
                device = web.devices.check(key.strip()) if isinstance(key, str) else None
                if device is None:
                    time.sleep(BAD_KEY_DELAY)
                    return self._reply(401, {"error": "llave no válida"})
                log.info("apuntes-web: «%s» entra", device)
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

            def _file(self, path: Path, content_type: str, extra: dict | None = None):
                try:
                    size = path.stat().st_size
                except OSError:
                    return self._reply(404, {"error": "el fichero no está (todavía)"})
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    with path.open("rb") as f:
                        while block := f.read(CHUNK):
                            self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _set_cookie(self, code: int, value: str, max_age: int, payload: dict | None = None):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                body = json.dumps(payload or {"ok": True}, ensure_ascii=False).encode()
                self._send(code, "application/json; charset=utf-8", body, {
                    "Cache-Control": "no-store",
                    "Set-Cookie": f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}",
                })

        return Handler
