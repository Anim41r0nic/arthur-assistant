"""El enlace de arthur-core con arthur-voz (fase S4): transcribir lo que dices y poner voz a lo que responde.

arthur-voz es otro contenedor de la misma red interna de Docker (Dockerfile.voz, voice/voice_server.py).
Este módulo solo hace peticiones HTTP con la biblioteca estándar: el core no carga whisper ni Piper.

Se configura con variables de entorno del contenedor del core:
  ARTHUR_VOZ_URL         p. ej. http://voz:8771   (sin ella, el servidor funciona sin voz)
  ARTHUR_VOZ_TOKEN_FILE  la llave compartida con arthur-voz (fichero de secretos)
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STT_TIMEOUT = 120   # whisper con 60 s de audio en un procesador modesto
TTS_TIMEOUT = 60


class VoiceError(Exception):
    """arthur-voz no ha podido hacerlo. `status` es el código que conviene devolver al aparato."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def check_voice_url(url: str) -> str:
    """Solo dentro de la red interna: un servicio de Docker por su nombre (sin puntos), localhost o una IP
    privada. La llave viaja en cada petición y arthur-voz nunca debe estar en internet."""
    parts = urllib.parse.urlsplit(url.strip().rstrip("/"))
    host = parts.hostname or ""
    if parts.scheme != "http" or not host or parts.path not in ("", "/"):
        raise ValueError("ARTHUR_VOZ_URL debe ser como http://voz:8771")
    internal = "." not in host or host == "localhost"
    try:
        ip = ipaddress.ip_address(host)
        internal = ip.is_private or ip.is_loopback
    except ValueError:
        pass
    if not internal:
        raise ValueError("ARTHUR_VOZ_URL debe ser una dirección de la red interna (p. ej. http://voz:8771)")
    return f"http://{parts.netloc}"


class VoiceLink:
    def __init__(self, url: str, token: str, *, speed: float = 1.0, opener=None) -> None:
        self.url = check_voice_url(url)
        if len(token) < 32:
            raise ValueError("La llave de arthur-voz es demasiado corta (mínimo 32 caracteres)")
        self._token = token
        self.speed = speed
        # Sin proxies del sistema: va directo al contenedor vecino.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls, env, *, speed: float = 1.0) -> VoiceLink | None:
        url = env.get("ARTHUR_VOZ_URL", "").strip()
        if not url:
            return None
        path = env.get("ARTHUR_VOZ_TOKEN_FILE", "/run/secrets/voz_token")
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ValueError(f"No puedo leer la llave de arthur-voz ({path}): {e}") from e
        return cls(url, token, speed=speed)

    def _post(self, path: str, body: bytes, content_type: str, timeout: float) -> tuple[str, bytes]:
        req = urllib.request.Request(self.url + path, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", content_type)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            with e:
                try:
                    detail = json.loads(e.read()).get("error", "")
                except (ValueError, AttributeError):
                    detail = ""
            if e.code == 400:
                raise VoiceError(detail or "arthur-voz no acepta esa petición", 400) from None
            if e.code == 503:
                raise VoiceError("Estoy transcribiendo otra petición; prueba en un momento", 503) from None
            raise VoiceError(f"arthur-voz respondió {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise VoiceError(f"No puedo conectar con arthur-voz: {getattr(e, 'reason', e)}") from None

    def transcribe(self, wav: bytes) -> str:
        _, body = self._post("/stt", wav, "audio/wav", STT_TIMEOUT)
        try:
            text = json.loads(body)["text"]
        except (ValueError, KeyError, TypeError):
            raise VoiceError("arthur-voz devolvió una respuesta rara") from None
        if not isinstance(text, str):
            raise VoiceError("arthur-voz devolvió una respuesta rara")
        return text

    def speak(self, text: str) -> bytes:
        content_type, body = self._post("/tts", json.dumps({"text": text, "speed": self.speed}).encode(),
                                        "application/json", TTS_TIMEOUT)
        if content_type.split(";")[0].strip() != "audio/wav":
            raise VoiceError("arthur-voz no devolvió audio")
        return body
