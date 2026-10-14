"""Dónde se guardan los apuntes: <datos>/notes/

  notes.sqlite3        carpetas, notas y el texto de cada página (así un fallo a mitad no obliga a repetir lo leído)
  <id>/src/NNN.ext     lo que subió Marc tal cual (JPEG, PNG o PDF)
  <id>/pages/NNN.ext   una imagen por página para el motor (los PDF se pasan a imágenes con pdftoppm)
  <id>/out/escaneo.pdf el PDF escaneado con texto buscable (el limpio se hace al descargarlo: tarda ~1 s)

Carpetas: un árbol como el de la carpeta UNI de Marc (asignatura → tema → …). La raíz es la carpeta 0, que no
está en la tabla. Una carpeta solo se borra vacía: así nunca se pierden apuntes por un toque.

Estados de una nota: borrador (subiendo fotos) -> en_cola -> leyendo -> listo | error.
El escaneo va aparte: no | en_cola | haciendo | listo | error.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.config import data_dir

SCHEMA_VERSION = 2  # 1: notas y páginas · 2: carpetas y de qué fichero sale cada página
MAX_TITLE = 120
MAX_FOLDER = 80
MAX_DEPTH = 6  # más hondo no cabe en la pantalla del móvil
MAX_MARKDOWN = 2_000_000  # caracteres: un cuatrimestre entero cabe de sobra
STATUS = ("borrador", "en_cola", "leyendo", "listo", "error")
LABELS = {"borrador": "Subiendo fotos", "en_cola": "En cola", "leyendo": "Leyendo…", "listo": "Listo", "error": "Error"}


def notes_dir() -> Path:
    return data_dir() / "notes"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class NoteError(Exception):
    pass


@dataclass(frozen=True)
class Note:
    id: int
    title: str
    engine: str
    lang: str
    status: str
    error: str
    pages: int
    done: int
    markdown: str
    edited: bool
    scan: str
    scan_error: str
    created_at: str
    updated_at: str
    folder_id: int = 0
    origin: str = ""   # de dónde se importó (ruta relativa en la carpeta UNI); vacío si se subió desde la página


@dataclass(frozen=True)
class Page:
    n: int
    image: str
    text: str | None
    src: str = ""      # fichero subido del que sale (p. ej. 001.pdf) …
    src_page: int = 0  # … y su página (0 si es una foto): con esto se saca el texto que ya trae el PDF


@dataclass(frozen=True)
class Folder:
    id: int
    parent_id: int
    name: str


class NotesStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or notes_dir()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()  # una conexión compartida entre el hilo de la web y el que lee las páginas
        self._db = sqlite3.connect(self.root / "notes.sqlite3", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        if self._db.execute("PRAGMA user_version").fetchone()[0] < 1:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    lang TEXT NOT NULL DEFAULT 'es',
                    status TEXT NOT NULL DEFAULT 'borrador',
                    error TEXT NOT NULL DEFAULT '',
                    pages INTEGER NOT NULL DEFAULT 0,
                    done INTEGER NOT NULL DEFAULT 0,
                    markdown TEXT NOT NULL DEFAULT '',
                    edited INTEGER NOT NULL DEFAULT 0,
                    scan TEXT NOT NULL DEFAULT 'no',
                    scan_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pages (
                    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    n INTEGER NOT NULL,
                    image TEXT NOT NULL,
                    text TEXT,
                    PRIMARY KEY (note_id, n));
                PRAGMA user_version = 1;
            """)
        if self._db.execute("PRAGMA user_version").fetchone()[0] < 2:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER NOT NULL DEFAULT 0,
                    name TEXT NOT NULL,
                    UNIQUE (parent_id, name));
                ALTER TABLE notes ADD COLUMN folder_id INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE notes ADD COLUMN origin TEXT NOT NULL DEFAULT '';
                ALTER TABLE pages ADD COLUMN src TEXT NOT NULL DEFAULT '';
                ALTER TABLE pages ADD COLUMN src_page INTEGER NOT NULL DEFAULT 0;
                CREATE INDEX IF NOT EXISTS notes_folder ON notes (folder_id);
                PRAGMA user_version = 2;
            """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- carpetas ---
    def folder(self, note_id: int) -> Path:
        return self.root / str(int(note_id))

    # --- notas ---
    def create(self, title: str, engine: str, lang: str = "es", folder_id: int = 0, origin: str = "") -> Note:
        title = " ".join(str(title).split())[:MAX_TITLE] or "Apuntes"
        self.require_folder(folder_id)
        now = _now()
        with self._lock:
            cur = self._db.execute("INSERT INTO notes (title, engine, lang, folder_id, origin, created_at, updated_at) "
                                   "VALUES (?,?,?,?,?,?,?)", (title, engine, lang, folder_id, origin, now, now))
            note_id = cur.lastrowid
        for sub in ("src", "pages", "out"):
            (self.folder(note_id) / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.require(note_id)

    def get(self, note_id: int) -> Note | None:
        rows = self._q("SELECT * FROM notes WHERE id = ?", (note_id,))
        return _note(rows[0]) if rows else None

    def require(self, note_id: int) -> Note:
        note = self.get(note_id)
        if note is None:
            raise NoteError("Esa nota no existe")
        return note

    def list(self, folder_id: int | None = None) -> list[Note]:
        if folder_id is None:
            return [_note(r) for r in self._q("SELECT * FROM notes ORDER BY id DESC")]
        return [_note(r) for r in self._q("SELECT * FROM notes WHERE folder_id = ? ORDER BY title COLLATE NOCASE, id",
                                          (folder_id,))]

    def by_origin(self, origin: str) -> Note | None:
        rows = self._q("SELECT * FROM notes WHERE origin = ? AND origin != ''", (origin,))
        return _note(rows[0]) if rows else None

    def move(self, note_id: int, folder_id: int) -> Note:
        self.require_folder(folder_id)
        self._q("UPDATE notes SET folder_id = ?, updated_at = ? WHERE id = ?", (folder_id, _now(), note_id))
        return self.require(note_id)

    # --- carpetas (asignaturas, temas…) ---
    def get_folder(self, folder_id: int) -> Folder | None:
        if folder_id == 0:
            return Folder(0, 0, "Apuntes")
        rows = self._q("SELECT id, parent_id, name FROM folders WHERE id = ?", (folder_id,))
        return Folder(rows[0]["id"], rows[0]["parent_id"], rows[0]["name"]) if rows else None

    def require_folder(self, folder_id: int) -> Folder:
        folder = self.get_folder(folder_id)
        if folder is None:
            raise NoteError("Esa carpeta no existe")
        return folder

    def folders(self, parent_id: int) -> list[Folder]:
        return [Folder(r["id"], r["parent_id"], r["name"])
                for r in self._q("SELECT id, parent_id, name FROM folders WHERE parent_id = ? "
                                 "ORDER BY name COLLATE NOCASE", (parent_id,))]

    def all_folders(self) -> list[tuple[Folder, str]]:
        """Todas las carpetas con su ruta («CALCULO / COMPLEJOS»), en orden de árbol: para «Mover a…»."""
        out: list[tuple[Folder, str]] = []

        def walk(parent: int, prefix: str) -> None:
            for f in self.folders(parent):
                path = f"{prefix}{f.name}"
                out.append((f, path))
                walk(f.id, path + " / ")
        walk(0, "")
        return out

    def ready_under(self, folder_id: int) -> list[tuple[Note, str]]:
        """Los apuntes ya leídos de esta carpeta y de todas las de dentro, con la ruta relativa a ella (vacía si
        están en ella misma): lo que se puede marcar para «Preguntar a la IA» (p. ej. todo Cálculo con sus temas
        y exámenes)."""
        self.require_folder(folder_id)
        places: list[tuple[int, str]] = [(folder_id, "")]

        def walk(parent: int, prefix: str) -> None:
            for f in self.folders(parent):
                path = f"{prefix}{f.name}"
                places.append((f.id, path))
                walk(f.id, path + " / ")
        walk(folder_id, "")
        out = []
        for fid, path in places:
            out += [(_note(r), path) for r in self._q(
                "SELECT * FROM notes WHERE folder_id = ? AND status = 'listo' ORDER BY title COLLATE NOCASE", (fid,))]
        return out

    def path(self, folder_id: int) -> list[Folder]:
        """De la raíz a esta carpeta (la raíz no se incluye)."""
        out: list[Folder] = []
        while folder_id:
            folder = self.require_folder(folder_id)
            out.append(folder)
            folder_id = folder.parent_id
        return out[::-1]

    def counts(self, folder_id: int) -> tuple[int, int]:
        """(subcarpetas, apuntes) que tiene dentro, directamente."""
        subs = self._q("SELECT COUNT(*) FROM folders WHERE parent_id = ?", (folder_id,))[0][0]
        notes = self._q("SELECT COUNT(*) FROM notes WHERE folder_id = ?", (folder_id,))[0][0]
        return subs, notes

    def create_folder(self, parent_id: int, name: str) -> Folder:
        name = _folder_name(name)
        self.require_folder(parent_id)
        if len(self.path(parent_id)) >= MAX_DEPTH:
            raise NoteError(f"Como mucho {MAX_DEPTH} carpetas una dentro de otra")
        try:
            with self._lock:
                cur = self._db.execute("INSERT INTO folders (parent_id, name) VALUES (?, ?)", (parent_id, name))
        except sqlite3.IntegrityError:
            raise NoteError(f"Ya hay una carpeta «{name}» aquí") from None
        return self.require_folder(cur.lastrowid)

    def ensure_folder(self, parent_id: int, name: str) -> Folder:
        name = _folder_name(name)
        rows = self._q("SELECT id FROM folders WHERE parent_id = ? AND name = ?", (parent_id, name))
        return self.require_folder(rows[0]["id"]) if rows else self.create_folder(parent_id, name)

    def rename_folder(self, folder_id: int, name: str) -> Folder:
        if folder_id == 0:
            raise NoteError("La carpeta principal no se puede renombrar")
        self.require_folder(folder_id)
        try:
            self._q("UPDATE folders SET name = ? WHERE id = ?", (_folder_name(name), folder_id))
        except sqlite3.IntegrityError:
            raise NoteError("Ya hay una carpeta con ese nombre aquí") from None
        return self.require_folder(folder_id)

    def delete_folder(self, folder_id: int) -> None:
        if folder_id == 0:
            raise NoteError("La carpeta principal no se puede borrar")
        self.require_folder(folder_id)
        if self.counts(folder_id) != (0, 0):
            raise NoteError("Solo se puede borrar una carpeta vacía: mueve o borra antes lo que tiene dentro")
        self._q("DELETE FROM folders WHERE id = ?", (folder_id,))

    def update(self, note_id: int, **fields) -> Note:
        allowed = {"title", "engine", "lang", "status", "error", "pages", "done", "markdown", "edited", "scan",
                   "scan_error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"campos no válidos: {set(fields) - allowed}")
        if "status" in fields and fields["status"] not in STATUS:
            raise ValueError(f"estado no válido: {fields['status']}")
        sets = ", ".join(f"{name} = ?" for name in fields)  # nombres de la lista de arriba, nunca de fuera
        self._q(f"UPDATE notes SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), _now(), note_id))
        return self.require(note_id)

    def delete(self, note_id: int) -> None:
        self._q("DELETE FROM notes WHERE id = ?", (note_id,))
        shutil.rmtree(self.folder(note_id), ignore_errors=True)

    def waiting(self) -> list[Note]:
        """Lo que hay que (seguir) leyendo al arrancar: p. ej. si se reinició el servidor a mitad."""
        return [_note(r) for r in self._q("SELECT * FROM notes WHERE status IN ('en_cola', 'leyendo') ORDER BY id")]

    def scans_waiting(self) -> list[Note]:
        return [_note(r) for r in self._q("SELECT * FROM notes WHERE scan IN ('en_cola', 'haciendo') ORDER BY id")]

    # --- ficheros subidos ---
    def sources(self, note_id: int) -> list[Path]:
        return sorted(p for p in (self.folder(note_id) / "src").glob("[0-9][0-9][0-9].*") if p.is_file())

    def next_source(self, note_id: int, suffix: str) -> Path:
        n = len(self.sources(note_id)) + 1
        return self.folder(note_id) / "src" / f"{n:03d}{suffix}"

    # --- páginas ---
    def set_pages(self, note_id: int, images: list) -> None:
        """images: rutas de imagen, o (imagen, fichero subido, página de ese fichero) para saber de dónde sale."""
        rows = []
        for i, item in enumerate(images):
            image, src, src_page = item if isinstance(item, tuple) else (item, None, 0)
            rows.append((note_id, i + 1, image.name, src.name if src else "", src_page))
        with self._lock:
            self._db.execute("DELETE FROM pages WHERE note_id = ?", (note_id,))
            self._db.executemany("INSERT INTO pages (note_id, n, image, src, src_page) VALUES (?,?,?,?,?)", rows)
            self._db.execute("UPDATE notes SET pages = ?, done = 0, updated_at = ? WHERE id = ?",
                             (len(images), _now(), note_id))

    def pages(self, note_id: int) -> list[Page]:
        return [Page(r["n"], r["image"], r["text"], r["src"], r["src_page"])
                for r in self._q("SELECT n, image, text, src, src_page FROM pages WHERE note_id = ? ORDER BY n",
                                 (note_id,))]

    def page_image(self, note_id: int, n: int) -> Path | None:
        rows = self._q("SELECT image FROM pages WHERE note_id = ? AND n = ?", (note_id, n))
        return self.folder(note_id) / "pages" / rows[0]["image"] if rows else None

    def set_page_text(self, note_id: int, n: int, text: str) -> None:
        with self._lock:
            self._db.execute("UPDATE pages SET text = ? WHERE note_id = ? AND n = ?", (text, note_id, n))
            done = self._db.execute("SELECT COUNT(*) FROM pages WHERE note_id = ? AND text IS NOT NULL",
                                    (note_id,)).fetchone()[0]
            self._db.execute("UPDATE notes SET done = ?, updated_at = ? WHERE id = ?", (done, _now(), note_id))


def _folder_name(name: str) -> str:
    name = " ".join(str(name).split())[:MAX_FOLDER]
    if not name or name in (".", "..") or "/" in name:
        raise NoteError("Nombre de carpeta no válido")
    return name


def _note(row: sqlite3.Row) -> Note:
    return Note(id=row["id"], title=row["title"], engine=row["engine"], lang=row["lang"], status=row["status"],
                error=row["error"], pages=row["pages"], done=row["done"], markdown=row["markdown"],
                edited=bool(row["edited"]), scan=row["scan"], scan_error=row["scan_error"],
                created_at=row["created_at"], updated_at=row["updated_at"], folder_id=row["folder_id"],
                origin=row["origin"])
