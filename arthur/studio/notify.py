"""Avisos de Creator Studio por ARTHUR: «vídeo listo», «tiene dudas», «publicado»… al portátil y al móvil.

Studio no sabe hablar con el móvil: se lo pide al núcleo de ARTHUR (POST /api/v1/notice, solo con la llave del
aparato «studio»), que lo entrega como un recordatorio. Se configura con variables de entorno del contenedor:
  ARTHUR_STUDIO_AVISOS_URL         p. ej. http://core:8770 (sin ella, Studio funciona igual, sin avisos)
  ARTHUR_STUDIO_AVISOS_TOKEN_FILE  la llave del aparato «studio» (fichero de secretos)

Un aviso que no llega NUNCA para un vídeo ni una subida: se apunta en el registro y se sigue.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from ..integrations.voz import check_voice_url

log = logging.getLogger(__name__)

TIMEOUT = 15
MAX_TEXT = 300          # lo que acepta el núcleo
DEFAULT_TOKEN_FILE = "/run/secrets/studio_avisos"


class Notifier:
    def __init__(self, url: str, token: str, *, opener=None) -> None:
        try:
            # La misma regla que la voz: solo dentro de la red interna, la llave no sale a internet por un error.
            base = check_voice_url(url)
        except ValueError:
            raise ValueError("ARTHUR_STUDIO_AVISOS_URL debe ser de la red interna (p. ej. http://core:8770)") from None
        if len(token) < 32:
            raise ValueError("La llave de los avisos es demasiado corta")
        self.url = base + "/api/v1/notice"
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls, env=os.environ) -> Notifier | None:
        url = env.get("ARTHUR_STUDIO_AVISOS_URL", "").strip()
        if not url:
            return None
        path = env.get("ARTHUR_STUDIO_AVISOS_TOKEN_FILE", DEFAULT_TOKEN_FILE)
        try:
            return cls(url, Path(path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as e:
            log.warning("Studio: sin avisos por ARTHUR (%s)", e)
            return None

    def send(self, text: str) -> bool:
        text = " ".join(str(text).split())
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        req = urllib.request.Request(self.url, data=json.dumps({"text": text}).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                r.read()
            return True
        except urllib.error.HTTPError as e:
            e.close()
            log.warning("Studio: ARTHUR no ha aceptado el aviso (error %d)", e.code)
        except (urllib.error.URLError, OSError) as e:
            log.warning("Studio: no llego a ARTHUR para avisar (%s)", getattr(e, "reason", e))
        return False


def send(notifier: Notifier | None, text: str | None) -> None:
    """Sin avisos configurados o sin nada que decir, no hace nada."""
    if notifier is not None and text:
        notifier.send(text)
