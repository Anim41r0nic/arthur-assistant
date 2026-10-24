"""Lectura de `conocimiento.md`, lo único que el asistente de la web puede usar para contestar.

Marc edita el archivo en el servidor sin reiniciar nada: se vuelve a leer solo cuando cambia su fecha de
modificación. Si falta, no se puede leer o es enorme, `text()` da `None` y el servicio contesta el mensaje fijo
de contacto (mejor eso que inventar o mandar un texto gigante al modelo).
"""

from __future__ import annotations

import threading
from pathlib import Path

MAX_CHARS = 40_000
# El marcador que Marc deja en el apartado de precios mientras no los haya decidido.
PRICE_PLACEHOLDER = "[PRECIO]"


class Knowledge:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()  # el servidor atiende varias visitas a la vez
        self._mtime: int | None = None
        self._text: str | None = None

    def text(self) -> str | None:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
            except OSError:
                self._mtime = self._text = None
                return None
            if mtime == self._mtime:
                return self._text
            self._mtime = mtime
            try:
                text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = None
            if text is not None and (len(text) > MAX_CHARS or not text.strip()):
                text = None
            self._text = text
            return text

    def has_price_placeholder(self) -> bool:
        text = self.text()
        return text is not None and PRICE_PLACEHOLDER in text
