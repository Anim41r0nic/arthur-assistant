"""PDF sueltos de la carpeta `PDF/` de la bóveda Cerebro, leídos con `pdftotext` (poppler).

Un PDF escaneado no tiene texto dentro: `pdftotext` devuelve casi nada. Esos se apuntan en `skipped` (para
que `GET /api/estado` pueda decir «sin texto») y no se indexan; para leerlos hay que subirlos a Apuntes,
que sí hace OCR.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import Doc, is_sync_junk

ZONA = ZoneInfo("Europe/Madrid")
MIN_CHARS = 50
TIMEOUT = 60  # un PDF enorme o roto no debe dejar el reindexado colgado

# Rutas (relativas a la carpeta) de la última pasada que no dieron texto. Se vacía al empezar cada `scan`.
skipped: list[str] = []


def _text(path: Path, run) -> str | None:
    try:
        # La lista de argumentos va sin shell: el nombre del fichero nunca se interpreta como orden.
        out = run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
                  capture_output=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None  # sin poppler instalado o se pasó del tiempo
    if out.returncode != 0:
        return None
    stdout = out.stdout
    text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else str(stdout or "")
    # `-layout` separa las páginas con un salto de página (\f): para trocear sirve igual un párrafo.
    return text.replace("\f", "\n\n")


def scan(root: Path, run=subprocess.run) -> Iterator[Doc]:
    """Da un `Doc` por PDF con texto bajo `root`. `ref` es la ruta relativa a `root`."""
    skipped.clear()
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.lower().endswith(".pdf") or name.startswith(".") or is_sync_junk(name):
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            text = _text(path, run)
            if text is None or len(text.strip()) < MIN_CHARS:
                skipped.append(rel)
                continue
            yield Doc(
                source="pdf",
                ref=rel,
                title=path.stem,
                subject="PDF",
                changed_at=datetime.fromtimestamp(mtime, ZONA).isoformat(timespec="seconds"),
                text=text,
                links=(),
            )
