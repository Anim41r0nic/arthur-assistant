"""Recursos de un vídeo con su licencia: imágenes de Wikimedia Commons (paso 3).

Regla de diseño (docs/studio.md): un recurso sin licencia válida BLOQUEA el montaje. Aquí se decide qué vale:

  dominio público (pd…), CC0, CC BY (x.y)  → sí. CC BY obliga a citar al autor: va a la descripción del vídeo.
  CC BY-SA                                 → no: podría obligar a dar el vídeo entero con esa misma licencia.
  NC (no comercial), ND (sin cambios)      → no: el canal quiere ganar dinero y el montaje recorta y mueve.
  cualquier otra cosa o sin dato           → no.
  «Restrictions» (marca registrada, derechos de imagen…) → no, hasta que Marc lo mire.

Commons da la licencia en extmetadata. Se descarga una copia de 2560 px de ancho como mucho (de sobra para
1080p con zoom) y se guarda su sha256: si alguien cambia el fichero luego, se nota.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .. import __version__
from .store import Asset, StudioStore

log = logging.getLogger(__name__)

API_URL = "https://commons.wikimedia.org/w/api.php"
# Wikimedia pide que cada programa diga quién es y cómo contactar; sin contacto frena las peticiones (429).
# El contacto es el canal (público); nada personal de Marc.
CONTACT = "https://www.youtube.com/@ArthuroEstoicismo"
USER_AGENT = f"ARTHUR-Studio/{__version__} ({CONTACT}; imágenes libres con su licencia)"
DOWNLOAD_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}  # originales y copias reducidas
MAX_BYTES = 30 * 1024 * 1024
WIDTH = 2560
MIMES = {"image/jpeg": ".jpg", "image/png": ".png"}
TITLE = re.compile(r"^File:[^\n\r#<>\[\]{}|]{1,240}$")
MIN_INTERVAL = 1.0      # segundos entre peticiones: Wikimedia frena (error 429) a quien pide muy seguido
MAX_WAIT = 30.0         # lo más que se espera si Wikimedia pide paciencia (cabecera Retry-After)
ALLOWED = re.compile(r"^(pd|public domain|cc0|cc-zero|cc-pd|cc-by-\d(\.\d)?)$")


class AssetError(Exception):
    pass


def license_verdict(meta: dict[str, Any]) -> tuple[bool, str]:
    """(¿vale?, motivo legible). `meta` = extmetadata de Commons."""
    code = _value(meta, "License").lower().strip()
    short = _value(meta, "LicenseShortName")
    restrictions = _value(meta, "Restrictions").strip()
    if restrictions:
        return False, f"tiene restricciones ({restrictions}): míralo antes de usarlo"
    if not code and short.lower() == "public domain":
        code = "pd"
    if code.startswith("pd") or ALLOWED.match(code):
        return True, short or code
    if "-sa" in code:
        return False, f"{short or code}: obliga a compartir el vídeo con la misma licencia"
    if "-nc" in code or "-nd" in code:
        return False, f"{short or code}: no permite uso comercial o cambios"
    return False, f"licencia no admitida: {short or code or 'sin dato'}"


class CommonsClient:
    def __init__(self, store: StudioStore, *, api_url: str = API_URL,
                 opener: urllib.request.OpenerDirector | None = None, now: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.api_url = api_url
        self._opener = opener or urllib.request.build_opener()
        self.now = now
        self._sleep = sleep
        self._monotonic = monotonic
        self._last = float("-inf")

    def _get(self, url: str, limit: int = 2 * 1024 * 1024, *, retry: bool = True) -> bytes:
        wait = MIN_INTERVAL - (self._monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener.open(request, timeout=30) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            e.close()
            if e.code == 429 and retry:
                # Una sola vez y sin pasarse de MAX_WAIT: si sigue frenando, mejor parar y avisar.
                try:
                    pause = min(MAX_WAIT, max(MIN_INTERVAL, float(retry_after or 5)))
                except ValueError:
                    pause = 5.0
                log.info("Studio: Wikimedia pide esperar %.0f s", pause)
                self._sleep(pause)
                return self._get(url, limit, retry=False)
            raise AssetError(f"Wikimedia ha respondido con error {e.code}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise AssetError(f"No puedo conectar con Wikimedia: {getattr(e, 'reason', e)}") from None
        if len(data) > limit:
            raise AssetError(f"El fichero pasa de {limit // (1024 * 1024)} MB")
        return data

    def _api(self, params: dict[str, str]) -> dict[str, Any]:
        query = urllib.parse.urlencode({"format": "json", "formatversion": "2", **params})
        try:
            return json.loads(self._get(f"{self.api_url}?{query}"))
        except ValueError:
            raise AssetError("Wikimedia ha devuelto algo que no se entiende") from None

    def info(self, title: str) -> dict[str, Any]:
        """Datos de una imagen de Commons: dirección de descarga, tipo, licencia y autor."""
        if not TITLE.match(title):
            raise AssetError(f"Nombre de imagen de Commons no válido: {title!r} (debe empezar por «File:»)")
        body = self._api({"action": "query", "prop": "imageinfo", "titles": title,
                          "iiprop": "url|extmetadata|mime|size", "iiurlwidth": str(WIDTH)})
        pages = body.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or not pages[0].get("imageinfo"):
            raise AssetError(f"No existe en Commons: {title}")
        info = pages[0]["imageinfo"][0]
        meta = info.get("extmetadata", {})
        ok, reason = license_verdict(meta)
        return {
            "title": pages[0].get("title", title),
            "url": info.get("thumburl") or info.get("url"),
            "page": info.get("descriptionurl", ""),
            "mime": info.get("mime", ""),
            "license": (_value(meta, "License") or ("pd" if ok else "desconocida")).lower(),
            "license_name": _value(meta, "LicenseShortName"),
            "license_url": _value(meta, "LicenseUrl") or None,
            "author": _text(_value(meta, "Artist")) or _text(_value(meta, "Credit")) or None,
            "attribution": _value(meta, "AttributionRequired").lower() == "true",
            "ok": ok,
            "reason": reason,
        }

    def search(self, query: str, limit: int = 10) -> list[str]:
        """Nombres de imágenes de Commons que responden a `query` (sin mirar aún su licencia)."""
        body = self._api({"action": "query", "list": "search", "srnamespace": "6", "srlimit": str(max(1, min(limit, 50))),
                          "srsearch": f"{query[:200]} filetype:bitmap"})
        return [hit["title"] for hit in body.get("query", {}).get("search", []) if TITLE.match(hit.get("title", ""))]

    def fetch(self, title: str, project: str, folder: Path, name: str) -> Asset:
        """Comprueba la licencia, descarga (solo si vale) y lo apunta en el registro. Nunca lanza por licencia:
        devuelve el recurso «blocked» con el motivo, para que Marc vea qué falta."""
        info = self.info(title)
        asset = Asset(project=project, name=name, kind="imagen", source="commons", source_url=info["page"],
                      title=info["title"], license=info["license"], license_url=info["license_url"],
                      author=info["author"], attribution=info["attribution"], sha256=None,
                      status="ok" if info["ok"] else "blocked", reason=info["reason"])
        if info["ok"]:
            extension = MIMES.get(info["mime"])
            url = urllib.parse.urlsplit(info["url"] or "")
            if not extension:
                asset = _blocked(asset, f"tipo de fichero no admitido: {info['mime']}")
            elif url.scheme != "https" or url.hostname not in DOWNLOAD_HOSTS:
                asset = _blocked(asset, "la descarga no viene de los servidores de Wikimedia")
            else:
                data = self._get(info["url"], MAX_BYTES)
                target = folder / f"{name}{extension}"
                folder.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(target)
                asset = Asset(**{**asset.__dict__, "name": target.name, "sha256": hashlib.sha256(data).hexdigest()})
        self.store.save_asset(asset, now=self.now())
        log.info("Studio: recurso %s (%s) %s", asset.name, asset.license, asset.status)
        return asset


def attribution_lines(assets: list[Asset]) -> list[str]:
    """Créditos para la descripción del vídeo: los que la licencia exige y, por cortesía, el resto."""
    lines = []
    for a in assets:
        if a.status != "ok":
            continue
        who = f" — {a.author}" if a.author else ""
        lines.append(f"{a.title.removeprefix('File:')}{who} ({a.license.upper()}) {a.source_url}")
    return lines


def _blocked(asset: Asset, reason: str) -> Asset:
    return Asset(**{**asset.__dict__, "status": "blocked", "reason": reason})


def _value(meta: dict[str, Any], key: str) -> str:
    return str(meta.get(key, {}).get("value", "") or "")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _text(html: str) -> str:
    """El autor viene en HTML (enlaces a su página): solo el texto, en una línea y sin pasarse."""
    parser = _Text()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())[:200]
