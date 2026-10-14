"""Importar de una vez una carpeta de apuntes (la carpeta UNI de Marc) a la página de apuntes.

Cada subcarpeta pasa a ser una carpeta (asignatura, tema…) y cada PDF o foto un apunte en «borrador» con su fichero
ya subido: se ve el original al momento y se pasa a texto solo cuando Marc lo pida (así no se gasta nada de golpe).
La carpeta de origen solo se lee, nunca se toca.

Se puede repetir: lo ya importado (misma ruta) se salta, así que sirve también para traer lo nuevo que cuelguen
los profes. Lo que no es PDF, JPEG o PNG (circuitos .dwm, .doc, .zip…) se cuenta como «no admitido» y se deja.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .store import NotesStore

MAX_FILE = 200 * 1024 * 1024  # 200 MB: más que eso no es un apunte (y la subida desde la página admite 40 MB)
# Moodle añade la fecha de descarga a las carpetas: «Apunts de teoria-20261013» -> «Apunts de teoria»
DOWNLOAD_DATE = re.compile(r"-\d{8}$")
MAGIC = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"%PDF-", ".pdf"))


@dataclass
class Report:
    imported: list[str] = field(default_factory=list)
    already: int = 0
    unsupported: list[str] = field(default_factory=list)
    too_big: list[str] = field(default_factory=list)


def clean_name(name: str) -> str:
    return DOWNLOAD_DATE.sub("", " ".join(name.replace("_", " ").split())) or name


def kind(path: Path) -> str | None:
    """Por el contenido, no por el nombre (como las subidas de la página)."""
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None
    return next((suffix for magic, suffix in MAGIC if head.startswith(magic)), None)


def import_tree(store: NotesStore, root: Path, *, engine: str = "auto", lang: str = "es",
                folder_id: int = 0) -> Report:
    report = Report()
    _walk(store, root, root, folder_id, engine, lang, report)
    return report


def _walk(store: NotesStore, root: Path, here: Path, folder_id: int, engine: str, lang: str, report: Report) -> None:
    for entry in sorted(here.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if entry.name.startswith(".") or entry.is_symlink():
            continue  # ocultos y enlaces: nada que se salga de la carpeta elegida
        rel = entry.relative_to(root).as_posix()
        if entry.is_dir():
            sub = store.ensure_folder(folder_id, clean_name(entry.name))
            _walk(store, root, entry, sub.id, engine, lang, report)
            continue
        if not entry.is_file():
            continue
        suffix = kind(entry)
        if suffix is None:
            report.unsupported.append(rel)
            continue
        if entry.stat().st_size > MAX_FILE:
            report.too_big.append(rel)
            continue
        if store.by_origin(rel) is not None:
            report.already += 1
            continue
        note = store.create(clean_name(entry.stem), engine, lang, folder_id, origin=rel)
        shutil.copyfile(entry, store.next_source(note.id, suffix))
        report.imported.append(rel)
