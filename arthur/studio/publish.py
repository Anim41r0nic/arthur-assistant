"""Publicar en YouTube (paso 5): sube un vídeo APROBADO al canal que toca, en privado, con su miniatura.

Quién decide: solo Marc, con «Aprobar» en la página de revisión. Este módulo nunca aprueba nada; antes de subir
vuelve a comprobar las huellas (review.ready_to_publish) y que el permiso es del canal correcto. No existe ninguna
herramienta de chat que llegue aquí (regla de diseño: el modelo nunca publica).

Permisos (OAuth del canal, uno por canal):
  youtube.upload    subir vídeos y poner su miniatura
  youtube.readonly  ver qué canal es (para no subir nunca al canal del otro idioma)
NO se pide «youtube» ni «youtube.force-ssl» (gestionar la cuenta: borrar vídeos…). Por eso los subtítulos no se
suben solos (YouTube exige ese permiso grande para ellos): quedan en salida/subtitulos.srt.

Subida «reanudable» de YouTube: se pide una dirección de subida y se manda el vídeo a trozos de 8 MB. Si se corta,
se pregunta cuánto llegó y se sigue. Si se corta justo al final, se pregunta si el vídeo ya existe ANTES de volver a
mandarlo: así no salen vídeos repetidos. Si el vídeo sube pero la miniatura no, queda publicado con un aviso
(marcarlo como fallido haría que un reintento lo subiera dos veces).

El permiso del canal se guarda en <datos>/studio/youtube-oauth-<idioma> (600) y el cliente OAuth (lo descarga Marc
de Google Cloud, proyecto arthur-studio) en <datos>/studio/youtube-client.json.
"""

from __future__ import annotations

import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ..integrations.google import FileKeyring, GoogleAuth, GoogleError, NotConnected
from .review import CHANNELS, ReviewBook, ReviewError
from .store import studio_dir

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
CHUNK = 8 * 1024 * 1024        # múltiplo de 256 KB, como pide YouTube
MAX_THUMB = 2 * 1024 * 1024    # límite de YouTube para miniaturas
CATEGORY = "27"                # «Educación»
RECONNECT = "Ejecuta: arthur --studio-conectar-youtube {lang}"


class PublishError(Exception):
    """El mensaje se le enseña a Marc tal cual (página de revisión y terminal)."""


def client_file() -> Path:
    return studio_dir() / "youtube-client.json"


def token_file(language: str) -> Path:
    if language not in CHANNELS:
        raise PublishError(f"No hay canal para el idioma «{language}»")
    return studio_dir() / f"youtube-oauth-{language}"


def channel_file(language: str) -> Path:
    return studio_dir() / f"youtube-canal-{language}.json"


def install_client(source: Path) -> Path:
    """Copia el cliente OAuth que Marc descarga de Google Cloud (tipo «Aplicación de escritorio»)."""
    try:
        data = json.loads(source.read_text(encoding="utf-8"))["installed"]
        data["client_id"], data["client_secret"]
    except (OSError, ValueError, KeyError, TypeError):
        raise PublishError("Ese fichero no es un cliente OAuth de tipo «Aplicación de escritorio» de Google Cloud") from None
    target = client_file()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    shutil.copyfile(source, tmp)
    tmp.chmod(0o600)
    tmp.replace(target)
    return target


def auth_for(language: str, *, opener: urllib.request.OpenerDirector | None = None) -> GoogleAuth:
    return GoogleAuth(client_file(), FileKeyring(token_file(language)), opener=opener)


class YouTubeUploader:
    def __init__(self, language: str, *, auth: GoogleAuth | None = None,
                 opener: urllib.request.OpenerDirector | None = None, api: str = API, upload: str = UPLOAD) -> None:
        self.language = language
        self.expected = CHANNELS.get(language)
        if self.expected is None:
            raise PublishError(f"No hay canal para el idioma «{language}»")
        self.auth = auth or auth_for(language, opener=opener)
        self._opener = opener or urllib.request.build_opener()
        self.api, self.upload_url = api, upload

    # --- peticiones ---------------------------------------------------------------

    def _token(self) -> str:
        try:
            return self.auth.access_token()
        except NotConnected:
            raise PublishError(f"El canal {self.expected} no está conectado o el permiso caducó. "
                               + RECONNECT.format(lang=self.language)) from None
        except GoogleError as e:
            raise PublishError(f"Google: {e}") from None

    def _open(self, request: urllib.request.Request, timeout: int = 60):
        request.add_header("Authorization", f"Bearer {self._token()}")
        return self._opener.open(request, timeout=timeout)

    def _json(self, method: str, url: str, body: dict | None = None, headers: dict | None = None) -> tuple[dict, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        if data is not None:
            request.add_header("Content-Type", "application/json; charset=UTF-8")
        try:
            with self._open(request) as response:
                raw = response.read()
                return (json.loads(raw) if raw.strip() else {}), dict(response.headers)
        except urllib.error.HTTPError as e:
            raise PublishError(_explain(e)) from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise PublishError(f"No puedo conectar con YouTube: {getattr(e, 'reason', e)}") from None

    # --- comprobar el canal -----------------------------------------------------------

    def my_channel(self) -> dict:
        body, _ = self._json("GET", f"{self.api}/channels?part=snippet&mine=true")
        items = body.get("items", [])
        if len(items) != 1:
            raise PublishError("Ese permiso no corresponde a un único canal de YouTube")
        snippet = items[0].get("snippet", {})
        return {"id": items[0].get("id"), "title": snippet.get("title", ""), "handle": snippet.get("customUrl", "")}

    def check_channel(self) -> dict:
        """El permiso tiene que ser del canal de este idioma, y el mismo que se conectó (id guardado)."""
        channel = self.my_channel()
        if channel["handle"].lower().lstrip("@") != self.expected.lower().lstrip("@"):
            raise PublishError(f"El permiso es del canal {channel['handle'] or channel['title']}, no de {self.expected}: "
                               "no se sube nada. " + RECONNECT.format(lang=self.language))
        saved = channel_file(self.language)
        if saved.exists() and json.loads(saved.read_text(encoding="utf-8")).get("id") != channel["id"]:
            raise PublishError(f"El canal {self.expected} no es el mismo que se conectó: no se sube nada")
        return channel

    # --- subir -----------------------------------------------------------------------

    def upload(self, meta: dict, progress: Callable[[str], None] = lambda _: None) -> tuple[str, str | None]:
        """Sube el vídeo y pone la miniatura. Devuelve (id del vídeo, aviso o None)."""
        video = Path(meta["video"])
        size = video.stat().st_size
        resource = {
            "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
                        "categoryId": CATEGORY, "defaultLanguage": meta["language"],
                        "defaultAudioLanguage": meta["language"]},
            # En privado salvo lo del piloto automático (review.ready_to_publish decide; nunca otro valor).
            "status": {"privacyStatus": "public" if meta.get("privacy") == "public" else "private",
                       "selfDeclaredMadeForKids": False, "embeddable": True},
        }
        _, headers = self._json(
            "POST", f"{self.upload_url}/videos?uploadType=resumable&part=snippet,status", resource,
            {"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
        location = headers.get("Location") or headers.get("location")
        if not location or urllib.parse.urlsplit(location).hostname != urllib.parse.urlsplit(self.upload_url).hostname:
            raise PublishError("YouTube no ha dado una dirección de subida válida")
        video_id = self._send(location, video, size, progress)
        if not meta.get("thumbnail"):
            return video_id, None  # un corto (Short): YouTube elige el fotograma
        progress("miniatura")
        try:
            self._thumbnail(video_id, Path(meta["thumbnail"]))
        except PublishError as e:
            return video_id, f"La miniatura no se ha podido poner ({e}): ponla a mano en YouTube Studio"
        return video_id, None

    def _send(self, location: str, video: Path, size: int, progress: Callable[[str], None]) -> str:
        offset, failures = 0, 0
        with video.open("rb") as f:
            while True:
                f.seek(offset)
                block = f.read(CHUNK)
                end = offset + len(block) - 1
                request = urllib.request.Request(location, data=block, method="PUT", headers={
                    "Content-Length": str(len(block)), "Content-Range": f"bytes {offset}-{end}/{size}"})
                try:
                    with self._open(request, timeout=300) as response:
                        return self._video_id(response.read())  # 200/201: terminado
                except urllib.error.HTTPError as e:
                    if e.code == 308:  # «sigue»: dice hasta dónde ha llegado
                        offset = _received(e.headers.get("Range"))
                        e.close()
                        failures = 0
                        progress(f"subido {offset * 100 // size} %")
                        continue
                    if e.code not in (500, 502, 503, 504) or failures >= 3:
                        raise PublishError(_explain(e)) from None
                    e.close()
                    failures += 1
                except (urllib.error.URLError, TimeoutError):
                    if failures >= 3:
                        raise PublishError("La subida se ha cortado varias veces: inténtalo más tarde") from None
                    failures += 1
                # Se cortó: antes de repetir nada, preguntar qué tiene YouTube (quizá ya está entero).
                done, offset = self._status(location, size)
                if done:
                    return done

    def _status(self, location: str, size: int) -> tuple[str | None, int]:
        request = urllib.request.Request(location, data=b"", method="PUT",
                                         headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"})
        try:
            with self._open(request, timeout=60) as response:
                return self._video_id(response.read()), size
        except urllib.error.HTTPError as e:
            if e.code == 308:
                received = _received(e.headers.get("Range"))
                e.close()
                return None, received
            raise PublishError(_explain(e)) from None

    @staticmethod
    def _video_id(raw: bytes) -> str:
        try:
            video_id = json.loads(raw)["id"]
        except (ValueError, KeyError, TypeError):
            raise PublishError("YouTube ha terminado la subida pero no ha dicho el id del vídeo") from None
        if not isinstance(video_id, str) or not 5 <= len(video_id) <= 20:
            raise PublishError("YouTube ha devuelto un id de vídeo raro")
        return video_id

    def _thumbnail(self, video_id: str, thumb: Path) -> None:
        data = thumb.read_bytes()
        if len(data) > MAX_THUMB:
            raise PublishError("la miniatura pasa de 2 MB (límite de YouTube)")
        request = urllib.request.Request(f"{self.upload_url}/thumbnails/set?videoId={urllib.parse.quote(video_id)}",
                                         data=data, method="POST", headers={"Content-Type": "image/jpeg"})
        try:
            with self._open(request) as response:
                response.read()
        except urllib.error.HTTPError as e:
            raise PublishError(_explain(e)) from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise PublishError(f"no puedo conectar con YouTube: {getattr(e, 'reason', e)}") from None


def _received(header: str | None) -> int:
    """«Range: bytes=0-1048575» → 1048576 (lo siguiente que hay que mandar). Sin cabecera: no llegó nada."""
    if not header or "-" not in header:
        return 0
    return int(header.rsplit("-", 1)[1]) + 1


def _explain(error: urllib.error.HTTPError) -> str:
    try:
        detail = error.read(4096).decode("utf-8", errors="replace")
    finally:
        error.close()
    if "quotaExceeded" in detail or "uploadLimitExceeded" in detail:
        return "YouTube dice que hoy ya no se pueden subir más vídeos (límite diario): inténtalo mañana"
    if "insufficientPermissions" in detail or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in detail:
        return "El permiso del canal no deja hacer esto: vuelve a conectarlo"
    if error.code == 401:
        return "YouTube no acepta el permiso del canal (¿caducó?): vuelve a conectarlo"
    if error.code == 403 and "thumbnail" in detail.lower():
        return "YouTube no deja poner miniaturas propias: verifica el canal en youtube.com/verify"
    return f"YouTube ha respondido con error {error.code}"


def publish(book: ReviewBook, review_id: int, *, uploader_for: Callable[[str], YouTubeUploader] = YouTubeUploader,
            progress: Callable[[str], None] = lambda _: None,
            notify: Callable[[str], None] = lambda _: None) -> tuple[str, str | None]:
    """Publica una revisión aprobada. La marca «published» (con el id y, si lo hay, un aviso) o «failed».
    `notify`: el aviso por ARTHUR (notify.py) de que se ha subido o de que ha fallado."""
    try:
        review, meta = book.ready_to_publish(review_id)
    except ReviewError as e:
        raise PublishError(str(e)) from None
    uploader = uploader_for(review.language)
    try:
        channel = uploader.check_channel()
        progress(f"canal {channel['handle']} comprobado")
        video_id, warning = uploader.upload(meta, progress)
    except PublishError as e:
        book.mark_failed(review_id, str(e))
        log.warning("Studio: el vídeo #%d no se ha publicado: %s", review_id, e)
        notify(f"Studio: «{review.title[:120]}» NO se ha podido subir a {review.channel}. Mira la página.")
        raise
    book.mark_published(review_id, video_id, warning)
    where = "público" if meta.get("privacy") == "public" else "privado"
    log.info("Studio: vídeo #%d subido a %s en %s (%s)", review_id, review.channel, where, video_id)
    notify(f"Studio: «{review.title[:120]}» subido a {review.channel} en {where}.")
    return video_id, warning
