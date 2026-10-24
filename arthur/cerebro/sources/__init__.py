"""Fuentes del cerebro: cada una convierte lo que lee (notas, apuntes, PDF) en `Doc` para el índice.

Se leen en solo lectura: el cerebro nunca escribe en las bóvedas de Obsidian ni en la base de Apuntes.
"""

from __future__ import annotations

import re

from ..chunks import Doc

# Restos de Syncthing que no son notas de verdad: copias de un conflicto entre dos aparatos
# (`nota.sync-conflict-20261017-120000-ABCDEFG.md`) y ficheros a medio bajar (`~syncthing~nota.md.tmp`,
# `.syncthing.nota.md.tmp`). Indexarlos daría la misma nota dos veces o una versión a medias.
_SYNC_JUNK = re.compile(r"\.sync-conflict-|^~syncthing~|^\.syncthing\.", re.IGNORECASE)


def is_sync_junk(name: str) -> bool:
    return bool(_SYNC_JUNK.search(name))


__all__ = ["Doc", "is_sync_junk"]
