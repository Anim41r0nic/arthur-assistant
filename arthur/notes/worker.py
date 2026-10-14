"""El hilo que trabaja por detrás: pasa las fotos a texto página a página y luego hace el PDF escaneado.

Un solo hilo, un trabajo cada vez: el servidor tiene 8 GB y el modelo local ya ocupa la mitad; dos a la vez solo
irían más lentos. Primero se lee (lo que Marc espera) y después se hace el escaneo (que es más lento y opcional).

Cada página leída se guarda al momento: si algo falla en la 7 de 10, al reintentar se sigue por la 7. Los fallos
pasajeros (red, «demasiadas peticiones») se reintentan solos unas pocas veces con espera; los que no se arreglan
repitiendo (falta la clave) dejan la nota en «error» con el motivo.

Con los motores «auto» y «texto» cada página se mira por separado: si sale de un PDF que ya trae texto (los del
campus suelen traerlo), se usa ese texto con pdftotext, sin IA; si es una foto o un escaneo, «auto» se la pasa a la
IA y «texto» deja una línea avisando. Así se pueden pasar a texto los PDF de los profes sin gastar nada.
"""

from __future__ import annotations

import logging
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import render
from .engines import AI_ENGINES, EngineError, EngineRefused
from .store import NotesStore

log = logging.getLogger(__name__)

MAX_PAGES = 80           # por nota con IA: más de eso mejor en varias notas (y no se gasta la capa gratis de golpe)
MAX_PAGES_TEXT = 400     # con «auto» o «texto» (las diapositivas de un tema pasan de 80; sacar su texto es barato)
MIN_TEXT = 40            # caracteres: menos que eso es un escaneo con un número de página, no texto de verdad
NO_TEXT = "*[Página sin texto: es una imagen. Para leerla, vuelve a leer con Gemini o Local]*"
PDF_DPI = 170            # bastante para leer letra pequeña sin hacer imágenes enormes
RETRIES = 3
RETRY_WAIT = 30          # segundos, se multiplica por el intento (30, 60, 90)
PAGE_SEPARATOR = "\n\n"


class PagesError(Exception):
    pass


class Worker:
    def __init__(self, store: NotesStore, engines: dict[str, Callable[[], object]], *,
                 sleep: Callable[[float], None] = time.sleep, scan: Callable[..., None] | None = None,
                 scans_enabled: bool | None = None, auto_engine: Callable[[], str] = lambda: "gemini",
                 pdf_text: Callable[[Path, int], str] | None = None) -> None:
        self.store = store
        self._engines = engines  # nombre -> función que crea el motor (así la clave se lee al empezar cada nota)
        self._auto_engine = auto_engine  # qué IA usa «auto» para las fotos (Gemini si hay clave; si no, la local)
        self._pdf_text = pdf_text or pdf_page_text
        self._sleep = sleep
        self._scan = scan or render.scan_pdf
        self.scans_enabled = render.available("ocrmypdf") if scans_enabled is None else scans_enabled
        self._queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="apuntes", daemon=True)

    def start(self) -> None:
        for note in self.store.waiting():  # p. ej. el servidor se reinició a mitad
            self._queue.put(("read", note.id))
        for note in self.store.scans_waiting():
            self._queue.put(("scan", note.id))
        self._thread.start()

    def read(self, note_id: int) -> None:
        self.store.update(note_id, status="en_cola", error="")
        self._queue.put(("read", note_id))

    def scan(self, note_id: int) -> None:
        self.store.update(note_id, scan="en_cola", scan_error="")
        self._queue.put(("scan", note_id))

    def join(self) -> None:
        self._queue.join()

    def _loop(self) -> None:
        while True:
            job, note_id = self._queue.get()
            try:
                self.run(job, note_id)
            except Exception as e:  # el hilo sigue para la siguiente nota pase lo que pase
                log.exception("Apuntes: %s #%d", job, note_id)
                try:
                    if job == "read":
                        self.store.update(note_id, status="error", error=f"Fallo inesperado: {e}")
                    else:
                        self.store.update(note_id, scan="error", scan_error=f"Fallo inesperado: {e}")
                except Exception:
                    pass  # la nota se borró mientras tanto
            finally:
                self._queue.task_done()

    def run(self, job: str, note_id: int) -> None:
        if self.store.get(note_id) is None:
            return  # borrada mientras esperaba
        if job == "read":
            if self._read(note_id) and self.scans_enabled and self.store.require(note_id).scan == "no":
                self.scan(note_id)  # se encola detrás: lo primero es tener el texto
        else:
            self._do_scan(note_id)

    # --- leer ---
    def _read(self, note_id: int) -> bool:
        note = self.store.update(note_id, status="leyendo", error="")
        mixed = note.engine in ("auto", "texto")
        try:
            if note.engine not in (*self._engines, "auto", "texto"):
                raise PagesError(f"Motor desconocido: {note.engine}")
            if not self.store.pages(note_id):
                self.store.set_pages(note_id, expand_sources(self.store.sources(note_id),
                                                             self.store.folder(note_id) / "pages",
                                                             MAX_PAGES_TEXT if mixed else MAX_PAGES))
            ai_name = self._auto_engine() if note.engine == "auto" else note.engine
            if note.engine != "texto" and ai_name not in self._engines:
                raise PagesError(f"Motor desconocido: {ai_name}")
            # con «auto» la IA se crea solo si alguna página la necesita: un PDF con texto no pide clave de Gemini
            engine = None if mixed else self._engines[ai_name]()
        except (PagesError, EngineError) as e:
            self.store.update(note_id, status="error", error=str(e))
            return False
        started, used_ai = time.monotonic(), 0
        for page in self.store.pages(note_id):
            if page.text is not None:
                continue  # ya leída en un intento anterior
            text = None
            if mixed and page.src_page:
                extracted = self._pdf_text(self.store.folder(note_id) / "src" / page.src, page.src_page)
                if len(extracted.strip()) >= MIN_TEXT:
                    text = as_markdown(extracted)
            if text is None and note.engine == "texto":
                text = NO_TEXT
            if text is None:
                try:
                    if engine is None:
                        engine = self._engines[ai_name]()
                except EngineError as e:
                    self.store.update(note_id, status="error", error=f"Página {page.n} (es una imagen): {e}")
                    return False
                text = self._transcribe(note_id, engine, page)
                if text is None:
                    return False
                used_ai += 1
            if self.store.get(note_id) is None:
                return False  # borrada mientras se leía
            self.store.set_page_text(note_id, page.n, text)
        texts = [p.text or "" for p in self.store.pages(note_id)]
        self.store.update(note_id, status="listo", markdown=PAGE_SEPARATOR.join(texts).strip() + "\n", edited=0)
        log.info("Apuntes #%d: %d páginas (%d con IA) con %s en %.0f s", note_id, len(texts), used_ai, note.engine,
                 time.monotonic() - started)
        return True

    def _transcribe(self, note_id: int, engine, page) -> str | None:
        image = self.store.folder(note_id) / "pages" / page.image
        for attempt in range(1, RETRIES + 1):
            try:
                return engine.transcribe(image)
            except EngineRefused as e:
                self.store.update(note_id, status="error", error=f"Página {page.n}: {e}")
                return None
            except EngineError as e:
                if attempt == RETRIES:
                    self.store.update(note_id, status="error", error=f"Página {page.n}: {e}")
                    return None
                log.info("Apuntes #%d pág. %d: %s (reintento %d)", note_id, page.n, e, attempt)
                self._sleep(RETRY_WAIT * attempt)
        return None

    # --- escaneo ---
    def _do_scan(self, note_id: int) -> None:
        self.store.update(note_id, scan="haciendo", scan_error="")
        try:
            self._scan(self.store.sources(note_id), self.store.folder(note_id) / "out" / "escaneo.pdf")
        except render.RenderError as e:
            self.store.update(note_id, scan="error", scan_error=str(e))
            return
        self.store.update(note_id, scan="listo")


def pdf_pages(pdf: Path) -> int:
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=60, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise PagesError("No puedo leer el PDF (falta pdfinfo o ha tardado demasiado)") from None
    for line in out.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise PagesError("El PDF está dañado o protegido con contraseña")


def pdf_page_text(pdf: Path, page: int) -> str:
    """El texto que ya trae una página de un PDF ("" si es un escaneo o si pdftotext falla)."""
    try:
        out = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", "-f", str(page), "-l", str(page), str(pdf), "-"],
                             capture_output=True, timeout=60, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else ""


MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#$|~^])")


def as_markdown(text: str) -> str:
    """Texto plano de un PDF a Markdown que no se rompe al maquetar: se escapan los símbolos que Markdown o las
    fórmulas interpretarían (un «$» suelto abriría una fórmula y Typst fallaría) y se quitan los saltos de página."""
    lines = [MARKDOWN_SPECIAL.sub(r"\\\1", line.rstrip()) for line in text.replace("\f", "\n").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() or NO_TEXT


def expand_sources(sources: list[Path], pages_dir: Path, limit: int = MAX_PAGES) -> list[tuple[Path, Path, int]]:
    """Una imagen por página, con el fichero y la página de los que sale: las fotos se copian tal cual y cada PDF
    se pasa a imágenes JPEG con pdftoppm (para verlas al lado del texto, y para la IA si hace falta)."""
    if not sources:
        raise PagesError("No has subido ninguna foto ni PDF")
    pages_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    total = sum(pdf_pages(s) if s.suffix == ".pdf" else 1 for s in sources)
    if total > limit:
        raise PagesError(f"Son {total} páginas: el máximo por nota es {limit} (divídelo en varias notas)")
    out: list[tuple[Path, Path, int]] = []
    for src in sources:
        if src.suffix != ".pdf":
            target = pages_dir / f"{len(out) + 1:03d}{src.suffix}"
            shutil.copyfile(src, target)
            out.append((target, src, 0))
            continue
        with tempfile.TemporaryDirectory(prefix="pdf-", dir=pages_dir) as tmp:
            try:
                result = subprocess.run(["pdftoppm", "-r", str(PDF_DPI), "-jpeg", "-jpegopt", "quality=85",
                                         str(src), str(Path(tmp) / "p")], capture_output=True, timeout=600, check=False)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                raise PagesError("No puedo pasar el PDF a imágenes (falta pdftoppm o ha tardado demasiado)") from None
            if result.returncode != 0:
                raise PagesError("No puedo pasar el PDF a imágenes: está dañado o protegido")
            for k, image in enumerate(sorted(Path(tmp).glob("p-*.jpg")), start=1):
                target = pages_dir / f"{len(out) + 1:03d}.jpg"
                image.replace(target)
                out.append((target, src, k))
    return out
