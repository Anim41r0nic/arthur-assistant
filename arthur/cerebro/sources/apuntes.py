"""Los apuntes de la uni para el cerebro: se piden a Apuntes por su API (GET /api/export) con la llave del aparato
«cerebro». El cerebro nunca abre la base de datos de Apuntes: así Apuntes sigue mandando sobre sus datos y solo
entrega lo ya leído («listo»).

Si la respuesta no tiene la forma esperada se lanza ToolError en vez de devolver una lista vacía: el índice
tomaría «no hay nada» por «se ha borrado todo» y quitaría los apuntes.
"""

from __future__ import annotations

import urllib.parse

from ...tools.base import ToolError
from ...tools.services import ServiceLink
from ..chunks import Doc

SOURCE = "apuntes"


def ref(note_id: int) -> str:
    return f"apunte:{int(note_id)}"


def fetch(link: ServiceLink, since: str | None) -> list[Doc]:
    """Los apuntes «listo» que cambiaron desde `since` (ISO), o todos si es None."""
    path = "/api/export"
    if since is not None:
        path += "?" + urllib.parse.urlencode({"since": since})  # la zona lleva «+», que sin codificar sería un espacio
    notes = link.call(path).get("notes")
    if not isinstance(notes, list):
        raise ToolError(f"{link.name}: la exportación no trae la lista de apuntes")
    return [_doc(note, link.name) for note in notes]


def ids(link: ServiceLink) -> set[str]:
    """Las ref (`apunte:<id>`) de todos los apuntes «listo» que hay ahora: lo que no esté se quita del índice."""
    found = link.call("/api/export?ids=1").get("ids")
    if not isinstance(found, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in found):
        raise ToolError(f"{link.name}: la lista de apuntes no se entiende")
    return {ref(n) for n in found}


def _doc(note: object, name: str) -> Doc:
    try:
        path = note["path"]
        if not isinstance(note["id"], int) or not isinstance(path, list):
            raise TypeError
        return Doc(source=SOURCE, ref=ref(note["id"]), title=str(note["title"]),
                   # La asignatura es la primera carpeta (Cálculo → Tema 2 → …); en la principal no hay.
                   subject=str(path[0]) if path else "",
                   changed_at=str(note["changed_at"]), text=str(note["text"]), links=())
    except (KeyError, TypeError):
        raise ToolError(f"{name}: un apunte de la exportación viene incompleto") from None
