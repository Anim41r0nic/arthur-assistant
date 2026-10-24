"""Notas de las bóvedas de Obsidian (llegan al servidor por Syncthing y se montan en solo lectura).

Se salta todo lo que no es una nota de Marc o que él no quiere que se lea: carpetas ocultas (`.obsidian/`
con la configuración, `.stversions/` con las versiones viejas de Syncthing), la papelera `.trash/`, cualquier
carpeta `private/` a cualquier profundidad, las notas con `cerebro: no` en el YAML del principio y los restos
de Syncthing (conflictos y temporales).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import Doc, is_sync_junk

ZONA = ZoneInfo("Europe/Madrid")
# Una nota de varios MB no es una nota escrita a mano (un volcado, un log pegado): no vale la pena trocearla.
MAX_BYTES = 2_000_000
_SKIP_DIRS = {".trash", "private"}
_WIKILINK = re.compile(r"\[\[([^\]\[|#^]*)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_TITLE = re.compile(r"^#[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_NO = {"no", "false", "off", "0"}


def front_matter(text: str) -> tuple[dict[str, str], str]:
    """Separa el YAML del principio (entre dos líneas `---`) del cuerpo de la nota.

    Solo se leen pares `clave: valor` sencillos: es todo lo que hace falta (`cerebro: no`) y así no se
    necesita una librería de YAML."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            meta = {}
            for line in lines[1:i]:
                key, sep, value = line.partition(":")
                if sep and key.strip() and not key.startswith((" ", "\t", "-")):
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            return meta, "".join(lines[i + 1:])
    return {}, text  # sin cierre: no era YAML, es parte de la nota


def wikilinks(text: str) -> tuple[str, ...]:
    """`[[Nota B|alias]]` y `[[Nota C#apartado]]` → ("Nota B", "Nota C"), sin repetir y en orden.
    Un enlace a un apartado de la misma nota (`[[#local]]`) no apunta a otra nota y no cuenta."""
    seen: dict[str, None] = {}
    for m in _WIKILINK.finditer(text):
        target = m.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return tuple(seen)


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in _SKIP_DIRS


def scan(root: Path, vault: str) -> Iterator[Doc]:
    """Recorre la bóveda `root` y da un `Doc` por nota. `vault` es el nombre corto (`cerebro`, `business-lab`).

    No sigue enlaces simbólicos: una nota enlazada a un fichero de fuera de la bóveda no debe colarse en el
    índice. Una bóveda que no existe (Syncthing aún sin la carpeta) no da nada en vez de fallar."""
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Podar aquí evita incluso entrar en las carpetas que no se leen.
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        for name in sorted(filenames):
            if not name.lower().endswith(".md") or name.startswith(".") or is_sync_junk(name):
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                st = path.stat()
                if st.st_size > MAX_BYTES:
                    continue
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # borrada justo ahora o sin permiso: ya entrará en la siguiente pasada
            meta, body = front_matter(raw)
            if meta.get("cerebro", "").lower() in _NO:
                continue
            rel = path.relative_to(root)
            m = _TITLE.search(body)
            yield Doc(
                source=f"obsidian:{vault}",
                ref=rel.as_posix(),
                title=m.group(1).strip() if m else path.stem,
                subject=rel.parts[0] if len(rel.parts) > 1 else "",
                changed_at=datetime.fromtimestamp(st.st_mtime, ZONA).isoformat(timespec="seconds"),
                text=body,
                links=wikilinks(body),
            )
