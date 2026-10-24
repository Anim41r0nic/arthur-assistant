"""El apartado «Cuentas» de Studio: en qué estado está la conexión de cada canal de YouTube.

Estados (la página los enseña con texto, no solo con color):
  disconnected   no hay permiso guardado para ese canal
  unverified     hay un permiso guardado, pero aún no se ha comprobado con Google (ver un fichero no basta)
  connected      Google ha dicho que el permiso es de ESE canal (@ y, si se conectó antes, el mismo id)
  reconnect      Google ya no acepta el permiso (caducó o se retiró): hay que volver a conectarlo
  wrong_account  el permiso es de otro canal: con él no se sube nada
  unavailable    no se pudo comprobar (sin red, sin cliente OAuth, límite de YouTube…)

«Comprobar» solo pide a YouTube qué canal es (`channels.list mine=true`, lectura): nunca sube ni cambia nada. El
resultado se apunta en <datos>/studio/youtube-estado-<perfil>.json con su fecha, para enseñarla después sin
volver a llamar a Google. Si el permiso es más nuevo que esa comprobación (se ha vuelto a conectar), vuelve a
«sin comprobar».

Lo que sale hacia la página: nombre, @ e id público del canal, estado, fecha y una explicación. NUNCA el correo de
la cuenta, el permiso (token) ni el cliente OAuth: ni se leen para esto más allá de saber si existen.

Conectar no se hace desde la página: Google tiene que volver a 127.0.0.1 del ordenador que pidió el permiso, y eso
solo lo resuelve `--studio-conectar-youtube <perfil>` en la terminal (publish.connect). La página lo explica.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import channels
from .publish import (PermissionLost, PublishError, WrongAccount, YouTubeUploader, client_file, saved_channel,
                      token_file)
from .store import studio_dir

log = logging.getLogger(__name__)

STATES = ("disconnected", "unverified", "connected", "reconnect", "wrong_account", "unavailable")
CONNECT = "arthur --studio-conectar-youtube {profile}"
# En el servidor Studio va en Docker (docs/studio.md): la misma orden, dentro del contenedor de la fábrica.
CONNECT_SERVER = "docker compose --profile studio run --rm studio --studio-conectar-youtube {profile}"


def state_file(profile: channels.Profile) -> Path:
    return studio_dir() / f"youtube-estado-{profile.id}.json"


def remember(profile: channels.Profile, status: str, detail: str, now: datetime) -> dict:
    """Apunta el resultado de una comprobación (600, escritura atómica). Solo estado, fecha y explicación."""
    if status not in STATES:
        raise ValueError(status)
    record = {"status": status, "checked_at": now.replace(microsecond=0).isoformat(), "detail": detail[:300]}
    target = state_file(profile)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    os.replace(tmp, target)
    return record


def _recorded(profile: channels.Profile, token: Path) -> dict | None:
    path = state_file(profile)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if token.stat().st_mtime > path.stat().st_mtime:
            return None  # se ha vuelto a conectar después: lo apuntado ya no dice nada del permiso de ahora
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("status") not in STATES:
        return None
    return record


def row(name: str) -> dict:
    """La fila de un canal para la página, sin llamar a Google."""
    profile = channels.get(name)
    token = token_file(profile.id)
    saved = saved_channel(profile.id) or {}
    data = {
        "profile": profile.id, "name": profile.name, "handle": profile.handle,
        # El id del canal (UC…) es público; solo se conoce después de conectarlo y comprobarlo.
        "channel_id": saved.get("id") if isinstance(saved.get("id"), str) else None,
        "publishing": profile.publishing,
        "connect_command": CONNECT.format(profile=profile.id),
        "connect_command_server": CONNECT_SERVER.format(profile=profile.id),
        "status": "disconnected", "checked_at": None,
        "detail": profile.note or "No hay permiso guardado para este canal.",
    }
    if not token.exists():
        return data
    record = _recorded(profile, token)
    if record is None:
        data.update(status="unverified",
                    detail="Hay un permiso guardado, pero aún no se ha comprobado con Google.")
    else:
        data.update(status=record["status"], checked_at=record.get("checked_at"),
                    detail=str(record.get("detail", ""))[:300])
    return data


class Accounts:
    """Lo que usa la página. `uploader_for` se cambia en los tests por un YouTube falso."""

    def __init__(self, *, uploader_for: Callable[[str], YouTubeUploader] = YouTubeUploader,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._uploader_for = uploader_for
        self.now = now
        self._lock = threading.Lock()  # dos «Comprobar» a la vez no escriben el mismo fichero a la vez

    def rows(self) -> list[dict]:
        return [row(name) for name in channels.ORDER]

    def check(self, name: str) -> dict:
        """Comprueba con Google de quién es el permiso guardado. No sube ni cambia nada en YouTube."""
        profile = channels.get(name)
        with self._lock:
            if not token_file(profile.id).exists():
                return row(profile.id)  # sin permiso no hay nada que preguntar a Google
            if not client_file().exists():
                status, detail = "unavailable", ("Falta el cliente OAuth de Studio en este ordenador "
                                                 "(arthur --studio-cliente-youtube FICHERO).")
            else:
                try:
                    channel = self._uploader_for(profile.id).check_channel()
                    status, detail = "connected", f"Google confirma que el permiso es del canal {channel['handle']}."
                except PermissionLost:
                    status, detail = "reconnect", ("Google ya no acepta el permiso guardado (caducó o se retiró). "
                                                   "Vuelve a conectarlo.")
                except WrongAccount as e:
                    status, detail = "wrong_account", str(e)
                except PublishError as e:
                    status, detail = "unavailable", str(e)
                except Exception:  # nunca se enseña el error crudo: podría llevar algo que no toca
                    log.exception("Studio: comprobar la cuenta %s", profile.id)
                    status, detail = "unavailable", "Error inesperado al comprobar: mira el registro del servidor."
            remember(profile, status, detail, self.now())
            log.info("Studio: cuenta %s comprobada: %s", profile.id, status)
            return row(profile.id)
