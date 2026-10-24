"""Permiso de Google del panel, cifrado con AES-256-GCM y una clave fuera de /data.

La clave se monta como secreto de Docker; no se guarda ni se copia junto al volumen del panel.
`cryptography` solo se instala en la imagen panel-web: el núcleo de ARTHUR sigue usando la biblioteca estándar.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

from .store import PanelError, panel_dir

MAGIC = b"ARTHUR-PANEL-GOOGLE-v1\n"
NONCE_SIZE = 12
KEY_FILE = "/run/secrets/panel_google_key"


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise PanelError("Falta cryptography en la imagen panel-web; no se puede usar Google cifrado") from exc
    return AESGCM(key)


class EncryptedFileKeyring:
    """Solo almacena el token de este panel. La clave y el texto cifrado están en montajes distintos."""

    def __init__(self, path: Path | None = None, key_file: Path | None = None) -> None:
        self.path = path or panel_dir() / "google.enc"
        self.key_file = key_file or Path(os.environ.get("ARTHUR_PANEL_GOOGLE_KEY_FILE", KEY_FILE))

    def _key(self) -> bytes:
        try:
            # En el contenedor /data es el volumen que contiene el texto cifrado.
            if self.key_file.resolve().is_relative_to(self.path.parent.resolve()) or \
                    self.key_file.resolve().is_relative_to(Path("/data")):
                raise PanelError("La clave de Google debe estar fuera del volumen del panel")
            raw = self.key_file.read_text(encoding="ascii").strip()
            if len(raw) != 64:
                raise ValueError("longitud")
            key = bytes.fromhex(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            raise PanelError("Falta la clave de cifrado de Google del panel o tiene un formato incorrecto") from exc
        return key

    def ensure_ready(self) -> None:
        _aesgcm(self._key())

    def store(self, secret: str) -> None:
        key = self._key()
        nonce = secrets.token_bytes(NONCE_SIZE)
        encrypted = MAGIC + nonce + _aesgcm(key).encrypt(nonce, secret.encode("utf-8"), MAGIC)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".google-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self) -> str | None:
        try:
            encrypted = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PanelError("No se puede leer el permiso cifrado de Google") from exc
        if not encrypted.startswith(MAGIC) or len(encrypted) < len(MAGIC) + NONCE_SIZE + 16:
            raise PanelError("El permiso cifrado de Google está dañado")
        nonce = encrypted[len(MAGIC):len(MAGIC) + NONCE_SIZE]
        payload = encrypted[len(MAGIC) + NONCE_SIZE:]
        try:
            return _aesgcm(self._key()).decrypt(nonce, payload, MAGIC).decode("utf-8")
        except Exception as exc:
            raise PanelError("No se puede descifrar el permiso de Google; comprueba la clave del panel") from exc

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
