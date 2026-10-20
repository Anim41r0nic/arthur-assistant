"""Publicar en YouTube (paso 5): sube un vídeo APROBADO al canal que toca, en privado, con su miniatura.

Quién decide: solo Marc, con «Aprobar» en la página de revisión. Este módulo nunca aprueba nada; antes de subir
vuelve a comprobar las huellas (review.ready_to_publish) y que el permiso es del canal correcto. No existe ninguna
herramienta de chat que llegue aquí (regla de diseño: el modelo nunca publica).

Permisos (OAuth del canal, uno por canal):
  youtube.upload    subir vídeos y poner su miniatura
  youtube.readonly  ver qué canal es (para no subir nunca al canal de otro perfil)
NO se pide «youtube» ni «youtube.force-ssl» (gestionar la cuenta: borrar vídeos…). Por eso los subtítulos no se
suben solos (YouTube exige ese permiso grande para ellos): quedan en salida/subtitulos.srt.

Subida «reanudable» de YouTube: se pide una dirección de subida y se manda el vídeo a trozos de 8 MB. Si se corta,
se pregunta cuánto llegó y se sigue. Si se corta justo al final, se pregunta si el vídeo ya existe ANTES de volver a
mandarlo: así no salen vídeos repetidos. Si el vídeo sube pero la miniatura no, queda publicado con un aviso
(marcarlo como fallido haría que un reintento lo subiera dos veces).

El permiso de cada canal se guarda en <datos>/studio/youtube-oauth-<perfil> (600) y el @/id que se comprobó al
conectarlo en youtube-canal-<perfil>.json. Los de antes de los perfiles (youtube-oauth-es, youtube-canal-es.json…)
se siguen leyendo mientras no haya uno nuevo: no se renombra nada del volumen. El cliente OAuth (lo descarga Marc
de Google Cloud, proyecto arthur-studio) en <datos>/studio/youtube-client.json.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ..integrations.google import FileKeyring, GoogleAuth, GoogleError, NotConnected
from . import channels
from .review import ReviewBook, ReviewError
from .store import studio_dir

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
CHUNK = 8 * 1024 * 1024        # múltiplo de 256 KB, como pide YouTube
MAX_THUMB = 2 * 1024 * 1024    # límite de YouTube para miniaturas
CATEGORY = "27"                # «Educación»
RECONNECT = "Ejecuta: arthur --studio-conectar-youtube {profile}"


class PublishError(Exception):
    """El mensaje se le enseña a Marc tal cual (página de revisión y terminal)."""


class PermissionLost(PublishError):
    """El permiso guardado ya no vale (caducó o se retiró): hay que volver a conectar el canal."""


class WrongAccount(PublishError):
    """El permiso es de otro canal (u otro canal con el mismo @ que el que se conectó)."""


def client_file() -> Path:
    return studio_dir() / "youtube-client.json"


def profile_for(name: str) -> channels.Profile:
    try:
        return channels.get(name)
    except channels.UnknownProfile as e:
        raise PublishError(str(e)) from None


def _current(profile: channels.Profile, new: str, old: str) -> Path:
    """El fichero nuevo (por perfil) si existe; si no, el de antes de los perfiles (por idioma) si existe; si no, el
    nuevo. Así los permisos de estoicismo que ya hay en el servidor siguen valiendo sin tocarlos."""
    path = studio_dir() / new
    if not path.exists() and profile.legacy:
        legacy = studio_dir() / old
        if legacy.exists():
            return legacy
    return path


def token_file(name: str) -> Path:
    profile = profile_for(name)
    return _current(profile, f"youtube-oauth-{profile.id}", f"youtube-oauth-{profile.legacy}")


def channel_file(name: str) -> Path:
    profile = profile_for(name)
    return _current(profile, f"youtube-canal-{profile.id}.json", f"youtube-canal-{profile.legacy}.json")


def saved_channel(name: str) -> dict | None:
    """El @/id público que se comprobó al conectar (nunca correo ni permisos). None si no hay o no se entiende."""
    try:
        data = json.loads(channel_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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


def auth_for(name: str, *, opener: urllib.request.OpenerDirector | None = None) -> GoogleAuth:
    return GoogleAuth(client_file(), FileKeyring(token_file(name)), opener=opener)


def connect(name: str, open_browser: Callable[[str], None], paste: Callable[[str], str] | None = None, *,
            opener: urllib.request.OpenerDirector | None = None, api: str = API,
            record: Callable[[channels.Profile, dict], None] | None = None) -> dict:
    """Pide el permiso de YouTube para un perfil y comprueba que es SU canal antes de guardarlo.

    El permiso nuevo se guarda primero aparte («…pendiente»). Solo si Google devuelve el canal esperado (el @ y, si
    ya se había conectado, el mismo id) pasa a ser el permiso del perfil; si no, se borra y el permiso anterior
    (si lo había) queda como estaba. Antes, una conexión con la cuenta equivocada borraba también el bueno.
    Devuelve el canal comprobado (id, @ y título: datos públicos). `record`: apunta el estado para la página."""
    profile = profile_for(name)
    final = studio_dir() / f"youtube-oauth-{profile.id}"
    pending = final.with_name(final.name + ".pendiente")
    pending.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending.unlink(missing_ok=True)
    auth = GoogleAuth(client_file(), FileKeyring(pending), opener=opener)
    checked = False
    try:
        auth.authorize(SCOPES, open_browser, paste=paste)
        channel = YouTubeUploader(profile.id, auth=auth, opener=opener, api=api).check_channel()
        checked = True
    except GoogleError as e:
        raise PublishError(str(e)) from None
    finally:
        if not checked:  # otra cuenta, error o Ctrl+C: el permiso a medias no se queda
            pending.unlink(missing_ok=True)
    os.replace(pending, final)
    target = studio_dir() / f"youtube-canal-{profile.id}.json"
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"id": channel["id"], "handle": channel["handle"], "title": channel["title"]}, f, ensure_ascii=False)
    os.replace(tmp, target)
    if record is not None:
        record(profile, channel)
    log.info("Studio: canal %s conectado (%s)", profile.id, channel["handle"])
    return channel


class YouTubeUploader:
    def __init__(self, name: str, *, auth: GoogleAuth | None = None,
                 opener: urllib.request.OpenerDirector | None = None, api: str = API, upload: str = UPLOAD) -> None:
        """`name`: el perfil del canal (estoicismo_es…) o su alias viejo (es, en)."""
        self.profile = profile_for(name)
        self.language = self.profile.language
        self.expected = self.profile.handle
        self.auth = auth or auth_for(self.profile.id, opener=opener)
        self._opener = opener or urllib.request.build_opener()
        self.api, self.upload_url = api, upload

    # --- peticiones ---------------------------------------------------------------

    def _token(self) -> str:
        try:
            return self.auth.access_token()
        except NotConnected:
            raise PermissionLost(f"El canal {self.expected} no está conectado o el permiso caducó. "
                                 + RECONNECT.format(profile=self.profile.id)) from None
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
            # 401: Google ya no acepta el permiso. La página «Cuentas» lo distingue («Requiere reconexión»).
            raise (PermissionLost if e.code == 401 else PublishError)(_explain(e)) from None
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
        """El permiso tiene que ser del canal de este perfil, y el mismo que se conectó (id guardado)."""
        channel = self.my_channel()
        if channel["handle"].lower().lstrip("@") != self.expected.lower().lstrip("@"):
            raise WrongAccount(f"El permiso es del canal {channel['handle'] or channel['title']}, no de "
                               f"{self.expected}: no se sube nada. " + RECONNECT.format(profile=self.profile.id))
        saved = saved_channel(self.profile.id)
        if saved is None and channel_file(self.profile.id).exists():  # estropeado: mejor no subir que no comprobar
            raise PublishError(f"No se puede leer el canal guardado de {self.expected}: no se sube nada")
        if saved is not None and saved.get("id") != channel["id"]:
            raise WrongAccount(f"El canal {self.expected} no es el mismo que se conectó: no se sube nada")
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
        if meta.get("synthetic"):
            # La casilla «contenido alterado o sintético» de YouTube (Frutinovelas: imágenes y voces hechas con IA).
            resource["status"]["containsSyntheticMedia"] = True
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
        profile = review.target
    except (ReviewError, channels.UnknownProfile) as e:
        raise PublishError(str(e)) from None
    try:
        if not profile.publishing:
            # Frutinovelas (2026-10-17): conectado solo para comprobar la cuenta; subir es una decisión aparte.
            raise PublishError(f"La publicación en {profile.name} ({profile.handle}) está desactivada: no se sube "
                               "nada. Hay que activarla en el código (channels.py) cuando se decida")
        uploader = uploader_for(profile.id)
        if uploader.profile.id != profile.id:  # nunca el permiso de otro perfil, aunque sea del mismo idioma
            raise PublishError(f"Este vídeo es de {profile.handle} y el permiso preparado es de "
                               f"{uploader.profile.handle}: no se sube nada")
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
