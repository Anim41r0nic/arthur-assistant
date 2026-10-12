"""Comprobar una suscripción de Web Push (fase S6). Solo biblioteca estándar: la usan el core y arthur-push.

Cuando la web pide permiso para avisarte, el navegador devuelve una «suscripción»:
  {"endpoint": "https://web.push.apple.com/…", "keys": {"p256dh": "…", "auth": "…"}}
- endpoint: la dirección del servicio de avisos del fabricante (Apple, Google, Mozilla, Microsoft) para ESE
  navegador. Ahí se manda el aviso y el fabricante lo lleva al teléfono aunque la web esté cerrada.
- p256dh y auth: las llaves con las que se cifra el aviso. Solo el navegador puede descifrarlo: el fabricante
  lo transporta sin poder leerlo.

Por qué se comprueba tanto: el servidor va a hacer una petición a esa dirección. Si aceptara cualquiera, un
aparato con llave podría hacer que el servidor llamara a sitios de la red de casa (SSRF). Solo se admiten los
servicios de avisos conocidos, por https y sin puertos raros.
"""

from __future__ import annotations

import base64
import binascii
import urllib.parse

MAX_ENDPOINT = 1024
# Los servicios de avisos de los navegadores. Nombre exacto, o terminación (los de Microsoft llevan región).
PUSH_HOSTS = frozenset({
    "web.push.apple.com",                 # Safari en iPhone, iPad y Mac
    "fcm.googleapis.com",                 # Chrome, Android, Brave…
    "updates.push.services.mozilla.com",  # Firefox
})
PUSH_HOST_SUFFIXES = (".notify.windows.com",)  # Edge en Windows (p. ej. wns2-db5p.notify.windows.com)


class SubscriptionError(ValueError):
    pass


def b64url_decode(value: object) -> bytes:
    """Base64 «para direcciones» (sin relleno), como lo dan los navegadores."""
    if not isinstance(value, str) or len(value) > 200:
        raise SubscriptionError("llave con formato raro")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise SubscriptionError("llave con formato raro") from None


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def allowed_endpoint(url: object) -> bool:
    if not isinstance(url, str) or len(url) > MAX_ENDPOINT:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and port in (None, 443) and not parts.username and not parts.password
            and (host in PUSH_HOSTS or (host.endswith(PUSH_HOST_SUFFIXES) and host.count(".") >= 3)))


def check_subscription(value: object) -> dict:
    """La suscripción limpia ({"endpoint", "keys": {"p256dh", "auth"}}) o SubscriptionError."""
    if not isinstance(value, dict):
        raise SubscriptionError("la suscripción debe ser un objeto")
    endpoint, keys = value.get("endpoint"), value.get("keys")
    if not allowed_endpoint(endpoint):
        raise SubscriptionError("esa dirección no es la de un servicio de avisos conocido")
    if not isinstance(keys, dict):
        raise SubscriptionError("faltan las llaves de la suscripción")
    p256dh, auth = b64url_decode(keys.get("p256dh")), b64url_decode(keys.get("auth"))
    # p256dh: un punto de la curva P-256 sin comprimir (65 bytes, empieza por 4). auth: 16 bytes al azar.
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        raise SubscriptionError("las llaves de la suscripción no tienen el tamaño esperado")
    return {"endpoint": endpoint, "keys": {"p256dh": b64url_encode(p256dh), "auth": b64url_encode(auth)}}
