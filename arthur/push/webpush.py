"""Cifrar y firmar un aviso de Web Push (fase S6). Solo lo usa arthur-push (Dockerfile.push).

Dos estándares, y nada inventado aquí:
  - RFC 8291 (cifrado «aes128gcm»): el aviso va cifrado para ESE navegador. Apple/Google/Mozilla lo llevan al
    teléfono sin poder leerlo. Se comprueba con el ejemplo oficial del propio RFC (tests/test_push.py).
  - RFC 8292 (VAPID): el servidor firma cada envío con su llave. La suscripción del navegador queda atada a esa
    llave: aunque alguien robara la dirección del iPhone, sin la llave privada no podría mandarle avisos.

Las operaciones de criptografía (curva P-256, AES-GCM, firmas ECDSA) las hace la biblioteca `cryptography`
(PyCA), con versión fija y huella en requirements-push.txt. Aquí solo se juntan las piezas como dicen los RFC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import time
import urllib.parse
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .subscription import b64url_encode

RECORD_SIZE = 4096
JWT_HOURS = 12   # el máximo que admiten los servicios es 24 h


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 (RFC 5869) de un solo bloque: todo lo que pide Web Push cabe en 32 bytes."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *,
            as_private: ec.EllipticCurvePrivateKey | None = None, salt: bytes | None = None) -> bytes:
    """El cuerpo cifrado del aviso (RFC 8291, sección 3). `as_private` y `salt` solo se pasan en los tests (el
    ejemplo del RFC); de normal son nuevos y al azar en CADA aviso."""
    if len(plaintext) > RECORD_SIZE - 16 - 1 - 86:
        raise ValueError("aviso demasiado largo")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _point(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, ecdh_secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    # Un único registro: el texto + 0x02 (marca de «último registro»), cifrado con AES-128-GCM.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack(">IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


class Vapid:
    """La llave con la que arthur-push firma los avisos (una sola, guardada en un fichero 600)."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._key = private_key
        self.public_key = b64url_encode(_point(private_key.public_key()))  # la que la web da al navegador

    @classmethod
    def load_or_create(cls, path: Path) -> Vapid:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            key = ec.generate_private_key(ec.SECP256R1())
            data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
            # O_EXCL: si dos arranques coinciden, uno falla en vez de pisar la llave del otro. 600 desde el inicio.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return cls(key)
        key = serialization.load_pem_private_key(data, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError(f"{path} no es una llave P-256")
        return cls(key)

    def authorization(self, endpoint: str, subject: str, *, now: float | None = None) -> str:
        """La cabecera Authorization de un envío: un JWT firmado (ES256) para el servicio de ese endpoint."""
        parts = urllib.parse.urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + JWT_HOURS * 3600),
                  "sub": subject}
        signing_input = ".".join(b64url_encode(json.dumps(p, separators=(",", ":")).encode())
                                 for p in ({"typ": "JWT", "alg": "ES256"}, claims))
        r, s = decode_dss_signature(self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # JWT quiere r||s, no el formato DER
        return f"vapid t={signing_input}.{b64url_encode(signature)}, k={self.public_key}"
