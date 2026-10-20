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
from . import channels

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
            raise ValueError("La dirección de los avisos debe ser de la red interna (p. ej. http://core:8770)") from None
        if len(token) < 32:
            raise ValueError("La llave de los avisos es demasiado corta")
        self.url = base + "/api/v1/notice"
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls, env=os.environ, prefix: str = "ARTHUR_STUDIO_AVISOS",
                 default_token_file: str = DEFAULT_TOKEN_FILE) -> Notifier | None:
        # El prefijo deja que el panel y el cerebro usen su propia llave con el mismo código que Studio; los
        # mensajes del registro dicen el prefijo para saber quién no ha podido avisar.
        url = env.get(f"{prefix}_URL", "").strip()
        if not url:
            return None
        path = env.get(f"{prefix}_TOKEN_FILE", default_token_file)
        try:
            return cls(url, Path(path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as e:
            log.warning("%s: sin avisos por ARTHUR (%s)", prefix, e)
            return None

    def send(self, text: str, *, destination: str | None = None, client: int | None = None,
             review: int | None = None, profile: str | None = None) -> bool:
        """`destination`: None, «panel», «clientes» (con `client`, el número del cliente), «studio» (con `review`, el
        número del vídeo, y `profile`, su canal) o «apuntes» (el Repaso, sin nada más). Son nombres fijos: la dirección real la pone el núcleo, y solo si
        esta llave puede pedirla."""
        text = " ".join(str(text).split())
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        if destination not in (None, "panel", "clientes", "studio", "apuntes"):
            raise ValueError("Destino de aviso desconocido")
        # El número va solo con «clientes», y siempre como entero (True es un int en Python: se descarta aparte).
        valid_client = isinstance(client, int) and not isinstance(client, bool) and client > 0
        if (destination == "clientes") != (client is not None) or (client is not None and not valid_client):
            raise ValueError("El aviso de clientes necesita el número del cliente")
        valid_review = isinstance(review, int) and not isinstance(review, bool) and review > 0
        if (destination == "studio") != (review is not None or profile is not None) or (
                destination == "studio" and not (valid_review and profile in channels.ORDER)):
            raise ValueError("El aviso de un vídeo necesita su número y su canal")
        body = {"text": text}
        if destination:
            body["destination"] = destination
        if client is not None:
            body["client"] = client
        if destination == "studio":
            body["review"], body["profile"] = review, profile
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                r.read()
            return True
        except urllib.error.HTTPError as e:
            e.close()
            log.warning("Avisos: ARTHUR no ha aceptado el aviso (error %d)", e.code)
        except (urllib.error.URLError, OSError) as e:
            log.warning("Avisos: no llego a ARTHUR para avisar (%s)", getattr(e, "reason", e))
        return False


def send(notifier: Notifier | None, text: str | None, link: tuple[int, str] | None = None) -> None:
    """Sin avisos configurados o sin nada que decir, no hace nada. `link` (número del vídeo, perfil): tocar el aviso
    en el móvil abre ese vídeo ya en su canal. Si el núcleo no acepta el enlace (sin ARTHUR_STUDIO_PUBLIC_URL, o uno
    de antes de 0.63.1), se manda sin él: mejor el aviso sin enlace que ninguno."""
    if notifier is None or not text:
        return
    if link is not None:
        try:
            if notifier.send(text, destination="studio", review=link[0], profile=link[1]):
                return
        except ValueError as e:
            log.warning("Studio: aviso sin enlace al vídeo (%s)", e)
        else:
            log.warning("Studio: el aviso con enlace al vídeo no ha entrado; se manda sin enlace")
    notifier.send(text)


def review_link(book, review_id: int | None) -> tuple[int, str] | None:
    """(número, perfil) de una revisión para el enlace del aviso; None si no existe o su canal ya no se conoce."""
    if not isinstance(review_id, int) or isinstance(review_id, bool):
        return None
    try:
        review = book.get(review_id)
        return (review.id, review.target.id) if review else None
    except channels.UnknownProfile:
        return None
