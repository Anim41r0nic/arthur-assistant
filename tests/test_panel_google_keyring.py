"""Almacén cifrado del panel: clave separada, escritura privada y rechazo de datos alterados.

La prueba de AES-GCM real se ejecuta donde cryptography está instalado (imagen panel-web); aquí se puede
verificar el cableado con un cifrador falso sin instalar paquetes en el equipo.
"""

import hashlib
import hmac
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arthur.panel.google_keyring import EncryptedFileKeyring
from arthur.panel.store import PanelError


class FakeAEAD:
    def __init__(self, key):
        self.key = key

    def encrypt(self, nonce, data, aad):
        tag = hashlib.sha256(self.key + nonce + aad + data).digest()[:16]
        return data[::-1] + tag

    def decrypt(self, nonce, payload, aad):
        data, tag = payload[:-16][::-1], payload[-16:]
        expected = hashlib.sha256(self.key + nonce + aad + data).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("tag")
        return data


class KeyringTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key_file = self.root / "secrets" / "google-key"
        self.key_file.parent.mkdir()
        self.key_file.write_text("ab" * 32)
        self.cipher_file = self.root / "volume" / "google.enc"
        self.ring = EncryptedFileKeyring(self.cipher_file, self.key_file)

    def test_clave_separada_y_token_no_aparece_en_el_volumen(self):
        with patch("arthur.panel.google_keyring._aesgcm", side_effect=FakeAEAD):
            self.assertIsNone(self.ring.load())
            self.ring.store("permiso-secreto-de-prueba")
            self.assertNotIn(b"permiso-secreto-de-prueba", self.cipher_file.read_bytes())
            self.assertEqual(stat.S_IMODE(self.cipher_file.stat().st_mode), 0o600)
            self.assertEqual(self.ring.load(), "permiso-secreto-de-prueba")
            self.ring.clear()
            self.assertIsNone(self.ring.load())
        self.assertTrue(self.key_file.exists())

    def test_dato_o_clave_cambiada_no_se_acepta(self):
        with patch("arthur.panel.google_keyring._aesgcm", side_effect=FakeAEAD):
            self.ring.store("permiso")
            ciphertext = bytearray(self.cipher_file.read_bytes())
            ciphertext[-1] ^= 1
            self.cipher_file.write_bytes(ciphertext)
            with self.assertRaises(PanelError):
                self.ring.load()
            self.ring.store("permiso")
            self.key_file.write_text("cd" * 32)
            with self.assertRaises(PanelError):
                self.ring.load()

    def test_falta_clave_o_formato_malo(self):
        self.key_file.unlink()
        with self.assertRaises(PanelError):
            self.ring.ensure_ready()
        self.key_file.write_text("corta")
        with self.assertRaises(PanelError):
            self.ring.store("permiso")

    def test_clave_no_puede_vivir_junto_al_cifrado(self):
        keyring = EncryptedFileKeyring(self.cipher_file, self.cipher_file.parent / "key")
        with self.assertRaisesRegex(PanelError, "fuera del volumen"):
            keyring.store("permiso")


try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
except ImportError:
    HAS_CRYPTO = False
else:
    HAS_CRYPTO = True


@unittest.skipUnless(HAS_CRYPTO, "se ejecuta en la imagen del panel")
class RealAEADTests(KeyringTests):
    def test_aes_gcm_real_roundtrip_y_rechaza_alteracion(self):
        self.ring.store("permiso-real-de-prueba")
        self.assertEqual(self.ring.load(), "permiso-real-de-prueba")
        data = bytearray(self.cipher_file.read_bytes())
        data[-1] ^= 1
        self.cipher_file.write_bytes(data)
        with self.assertRaises(PanelError):
            self.ring.load()
