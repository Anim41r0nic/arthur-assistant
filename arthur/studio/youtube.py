"""YouTube Data API v3 en modo SOLO LECTURA, con una clave de API (sin iniciar sesión en ninguna cuenta).

Solo se leen datos públicos: búsquedas, vídeos y canales. Con una clave de API no se puede subir, borrar ni
cambiar nada de ningún canal (eso necesitará el permiso OAuth del canal, paso 6).

Cuota: Google da 10.000 unidades al día gratis y se reinicia a medianoche de California. Una búsqueda cuesta
100; leer vídeos, canales o listas, 1 por cada 50. Aquí se lleva la cuenta en studio.sqlite3 y se para ANTES
de pasarse (con margen), y cada respuesta se guarda unas horas para no pagar dos veces la misma pregunta.

La clave viaja en la cabecera X-Goog-Api-Key, nunca en la dirección: así no acaba en registros ni en errores.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import StudioStore, studio_dir

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/youtube/v3"
API_NAME = "youtube"
COSTS = {"search": 100, "videos": 1, "channels": 1, "playlistItems": 1}
DAILY_QUOTA = 10_000
RESERVE = 1_000          # nunca se gasta la última parte: queda para el resto del día (y para publicar)
CACHE_HOURS = 12
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{30,60}$")  # las claves de Google son «AIza…» de 39 caracteres
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


class YouTubeError(Exception):
    """Fallo pasajero (red, Google ocupado): se puede reintentar más tarde."""


class YouTubeRefused(YouTubeError):
    """Fallo que no se arregla repitiendo: clave mala, API sin activar, cuota agotada."""


class QuotaExceeded(YouTubeRefused):
    pass


def key_file() -> Path:
    return studio_dir() / "youtube-key"


def save_key(key: str, path: Path | None = None) -> None:
    """Fichero 600 en la carpeta 700 de Studio. Escribe primero a un temporal para no dejarlo a medias."""
    key = key.strip()
    if not KEY_PATTERN.match(key):
        raise ValueError("Eso no parece una clave de API de Google (suelen empezar por «AIza» y tener 39 caracteres)")
    path = path or key_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    os.replace(tmp, path)


def load_key(path: Path | None = None) -> str | None:
    try:
        return (path or key_file()).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def quota_day(moment: datetime) -> str:
    """El «día» de la cuota de YouTube: empieza a medianoche de California (hora del Pacífico).

    Se usa UTC-8 fijo: en verano (UTC-7) el día cambia una hora antes de lo calculado, así que como mucho
    creemos tener menos cuota de la real, nunca más.
    """
    if moment.tzinfo is None:
        moment = moment.astimezone()  # hora local del equipo
    return (moment.astimezone(timezone.utc) - timedelta(hours=8)).date().isoformat()


def rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _chunks(items: list[str], size: int = 50) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


class YouTubeClient:
    def __init__(self, key: str, store: StudioStore, *, api_url: str = API_URL,
                 opener: urllib.request.OpenerDirector | None = None,
                 now: Callable[[], datetime] = datetime.now, budget: int = DAILY_QUOTA - RESERVE) -> None:
        if not KEY_PATTERN.match(key or ""):
            raise YouTubeRefused("La clave de YouTube no es válida. Guárdala con: arthur --studio-clave-youtube")
        self._key = key
        self.store = store
        self.api_url = api_url
        self._opener = opener or urllib.request.build_opener()
        self.now = now
        self.budget = budget

    # --- cuota ---------------------------------------------------------------

    def used_today(self) -> int:
        return self.store.usage(quota_day(self.now()), API_NAME)

    def left_today(self) -> int:
        return max(0, self.budget - self.used_today())

    # --- llamadas --------------------------------------------------------------

    def _call(self, endpoint: str, params: dict[str, str]) -> tuple[dict[str, Any], datetime]:
        """Devuelve (respuesta, cuándo se pidió a Google). Primero mira la caché."""
        cache_key = hashlib.sha256(json.dumps([endpoint, sorted(params.items())]).encode()).hexdigest()
        cached = self.store.cache_get(cache_key, self.now() - timedelta(hours=CACHE_HOURS))
        if cached:
            return cached
        cost = COSTS[endpoint]
        if self.used_today() + cost > self.budget:
            raise QuotaExceeded(
                f"Hoy ya se han gastado {self.used_today()} de {self.budget} unidades de YouTube (se guardan "
                f"{RESERVE} de reserva). La cuota se renueva a las 9:00 de España.")
        # Se apunta ANTES de preguntar: Google cobra también las peticiones que fallan.
        self.store.add_usage(quota_day(self.now()), API_NAME, cost)
        url = f"{self.api_url}/{endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"X-Goog-Api-Key": self._key, "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=20) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read(4096).decode("utf-8", errors="replace")
            e.close()
            raise self._explain(e.code, detail) from None
        except TimeoutError:
            raise YouTubeError("YouTube no ha contestado a tiempo") from None
        except urllib.error.URLError as e:
            raise YouTubeError(f"No puedo conectar con YouTube: {e.reason}") from None
        except ValueError:
            raise YouTubeError("YouTube ha devuelto algo que no se entiende") from None
        fetched = self.now().replace(microsecond=0)
        self.store.cache_put(cache_key, body, now=fetched)
        return body, fetched

    @staticmethod
    def _explain(code: int, detail: str) -> YouTubeError:
        if "quotaExceeded" in detail or "dailyLimitExceeded" in detail:
            return QuotaExceeded("Google dice que la cuota de YouTube de hoy está agotada. Se renueva a las 9:00 de España.")
        if "keyInvalid" in detail or "API_KEY_INVALID" in detail or (code == 400 and "API key" in detail):
            return YouTubeRefused("Google no acepta la clave de YouTube. Revísala y guárdala otra vez con: "
                                  "arthur --studio-clave-youtube")
        if "accessNotConfigured" in detail or "SERVICE_DISABLED" in detail:
            return YouTubeRefused("La YouTube Data API v3 no está activada en el proyecto de Google Cloud de la clave")
        if code == 403:
            return YouTubeRefused("Google no deja usar la clave para esto (¿restricciones de la clave?)")
        if code == 400:
            return YouTubeRefused("YouTube no entiende la búsqueda (error 400)")
        if code in (429, 500, 502, 503, 504):
            return YouTubeError(f"YouTube está ocupado (error {code}); se reintentará")
        return YouTubeRefused(f"YouTube ha respondido con error {code}")

    # --- lo que usa el investigador ----------------------------------------------

    def search_videos(self, query: str, published_after: datetime, *, region: str = "ES",
                      language: str = "es", duration: str | None = None, max_results: int = 50) -> dict[str, Any]:
        """Los vídeos con más visitas publicados desde `published_after` que responden a `query` (100 unidades).

        `duration`: «short» (menos de 4 min), «medium» (4 a 20 min) o «long» (más de 20 min), filtro de YouTube.
        `language` solo es una preferencia para YouTube: se cuelan vídeos de otros idiomas (se filtran después).
        """
        params = {
            "part": "snippet", "type": "video", "q": query[:100], "order": "viewCount",
            "publishedAfter": rfc3339(published_after), "regionCode": region, "relevanceLanguage": language,
            "maxResults": str(max(1, min(max_results, 50))),
        }
        if duration:
            if duration not in ("short", "medium", "long"):
                raise ValueError(f"Duración no válida: {duration}")
            params["videoDuration"] = duration
        body, fetched = self._call("search", params)
        ids = [item["id"]["videoId"] for item in body.get("items", [])
               if ID_PATTERN.match(str(item.get("id", {}).get("videoId", "")))]
        return {
            "ids": list(dict.fromkeys(ids)),  # sin repetidos, en el mismo orden
            # Google lo llama «estimación» y a menudo se queda en 1.000.000: solo orienta.
            "total_estimated": body.get("pageInfo", {}).get("totalResults"),
            "fetched_at": fetched.isoformat(),
        }

    def videos(self, ids: list[str]) -> list[dict[str, Any]]:
        out = []
        for chunk in _chunks([i for i in dict.fromkeys(ids) if ID_PATTERN.match(i)]):
            body, fetched = self._call("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)})
            for item in body.get("items", []):
                snippet, stats = item.get("snippet", {}), item.get("statistics", {})
                out.append({
                    "id": item.get("id"),
                    "title": str(snippet.get("title", ""))[:150],
                    "channel_id": snippet.get("channelId"),
                    "channel_title": str(snippet.get("channelTitle", ""))[:80],
                    "published_at": snippet.get("publishedAt"),
                    # El idioma que declara el autor (p. ej. «es», «es-419», «pt-BR»); muchos no lo ponen.
                    "language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage"),
                    "seconds": parse_duration(item.get("contentDetails", {}).get("duration", "")),
                    "views": _int(stats.get("viewCount")),
                    "likes": _int(stats.get("likeCount")),        # None si el autor las oculta
                    "comments": _int(stats.get("commentCount")),  # None si están desactivados
                    "fetched_at": fetched.isoformat(),
                })
        return out

    def channels(self, ids: list[str]) -> list[dict[str, Any]]:
        out = []
        for chunk in _chunks([i for i in dict.fromkeys(ids) if ID_PATTERN.match(i)]):
            body, fetched = self._call("channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)})
            for item in body.get("items", []):
                stats = item.get("statistics", {})
                out.append({
                    "id": item.get("id"),
                    "title": str(item.get("snippet", {}).get("title", ""))[:80],
                    # YouTube redondea los suscriptores públicos (p. ej. 1.234.567 → 1.230.000) y algunos los ocultan.
                    "subscribers": None if stats.get("hiddenSubscriberCount") else _int(stats.get("subscriberCount")),
                    "videos": _int(stats.get("videoCount")),
                    "views": _int(stats.get("viewCount")),
                    "uploads": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
                    "fetched_at": fetched.isoformat(),
                })
        return out

    def recent_uploads(self, playlist_id: str, count: int = 10) -> list[str]:
        """Los últimos vídeos subidos por un canal (su lista «uploads»), del más nuevo al más viejo."""
        if not ID_PATTERN.match(playlist_id or ""):
            return []
        body, _ = self._call("playlistItems", {"part": "contentDetails", "playlistId": playlist_id,
                                               "maxResults": str(max(1, min(count, 50)))})
        return [i["contentDetails"]["videoId"] for i in body.get("items", [])
                if ID_PATTERN.match(str(i.get("contentDetails", {}).get("videoId", "")))]


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(text: str) -> int | None:
    """«PT1H2M3S» → 3723 segundos. None si no se entiende (directos en marcha devuelven «P0D»)."""
    match = _DURATION.match(text or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
