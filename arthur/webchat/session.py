"""Pases de sesión firmados (HMAC) y verificación de Cloudflare Turnstile."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

PASE_SEGUNDOS = 3600
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _desb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _huella_ip(ip: str) -> str:
    # Solo un trozo del hash: el pase queda atado a la IP sin llevarla en claro.
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


class Passes:
    """Pase sin estado en el servidor: caduca solo y no se puede falsificar sin el secreto."""

    def __init__(self, secret: bytes, now: Callable[[], float]) -> None:
        self._secret = secret
        self._now = now

    def _firma(self, datos: str) -> bytes:
        return hmac.new(self._secret, datos.encode("ascii"), hashlib.sha256).digest()

    def issue(self, ip: str) -> tuple[str, int]:
        expira = int(self._now()) + PASE_SEGUNDOS
        datos = _b64(f"{expira}|{_huella_ip(ip)}".encode())
        return f"{datos}.{_b64(self._firma(datos))}", expira

    def check(self, token: object, ip: str) -> bool:
        if not isinstance(token, str) or token.count(".") != 1:
            return False
        datos, firma = token.split(".")
        try:
            firma_ok = hmac.compare_digest(_desb64(firma), self._firma(datos))
            expira_txt, huella = _desb64(datos).decode("ascii").split("|")
            expira = int(expira_txt)
        except (binascii.Error, ValueError, UnicodeError):
            return False
        if not firma_ok:
            return False
        return self._now() <= expira and hmac.compare_digest(huella, _huella_ip(ip))


class Turnstile:
    def __init__(self, secret: str | None, opener=urllib.request.urlopen) -> None:
        self._secret = secret
        self._opener = opener

    def verify(self, token: object, ip: str) -> bool | None:
        """True/False según Cloudflare; None si no hay secreto o el servicio falla
        (así quien llama decide si deja pasar o no, en vez de culpar al visitante)."""
        if not self._secret:
            return None
        if not isinstance(token, str) or not token:
            return False
        datos = urllib.parse.urlencode(
            {"secret": self._secret, "response": token, "remoteip": ip}
        ).encode()
        try:
            req = urllib.request.Request(TURNSTILE_URL, data=datos, method="POST")
            with self._opener(req, timeout=10) as resp:
                cuerpo = json.loads(resp.read().decode("utf-8"))
            return bool(cuerpo["success"])
        # HTTPException: una respuesta cortada a medias (IncompleteRead) no es OSError; sin ella acabaría en un 500.
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
            return None
