"""Fotos (fase S5, paso 3): comprobar que son imágenes de verdad, que llegan al cerebro y que solo se recuerda
la última. Cerebro falso y servidor en 127.0.0.1: ni Ollama ni red de verdad."""

import base64
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from arthur.api.core_api import MAX_IMAGE, CoreServer, check_image
from arthur.brain.base import Message
from arthur.brain.ollama import to_ollama
from arthur.core.config import ConfigError, config_from_dict
from arthur.core.events import EventBus
from arthur.security.devices import DeviceRegistry

from .test_core import EchoBrain, make_assistant

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 100


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class CheckImageTest(unittest.TestCase):
    def test_real_images_pass(self):
        for raw in (JPEG, PNG, WEBP):
            with self.subTest(raw=raw[:4]):
                self.assertEqual(check_image(b64(raw)), b64(raw))

    def test_anything_else_is_rejected(self):
        cases = {
            "no es texto": 123,
            "base64 roto": "esto no es base64!!",
            "vacío": "",
            "un HTML disfrazado": b64(b"<html><script>alert(1)</script>"),
            "un ejecutable": b64(b"\x7fELF" + b"\x00" * 50),
            "demasiado grande": b64(b"\xff\xd8\xff" + b"\x00" * MAX_IMAGE),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(check_image(value))


class ImagesReachTheBrainTest(unittest.TestCase):
    def test_ollama_gets_the_photo_only_in_the_user_message(self):
        msgs = to_ollama([Message("user", "¿qué hay?", timestamp=datetime(2026, 10, 12, 13, 0), images=("AAAA",)),
                          Message("assistant", "Un gato.")])
        self.assertEqual(msgs[0]["images"], ["AAAA"])
        self.assertNotIn("images", msgs[1])
        self.assertNotIn("images", to_ollama([Message("user", "hola")])[0])

    def test_only_the_last_photo_is_kept(self):
        brain = EchoBrain()
        assistant, _ = make_assistant(brain)
        assistant.handle_text("mira esta", speak=False, images=("FOTO1",))
        self.assertEqual(brain.conversations[-1][-1].images, ("FOTO1",))
        assistant.handle_text("¿y de qué color es?", speak=False)  # sin foto nueva: la anterior sigue
        self.assertEqual([m.images for m in brain.conversations[-1] if m.images], [("FOTO1",)])
        assistant.handle_text("y esta otra", speak=False, images=("FOTO2",))
        seen = brain.conversations[-1]
        self.assertEqual([m.images for m in seen if m.images], [("FOTO2",)])  # la primera ya no se reenvía
        self.assertIn("ya no la tienes delante", seen[0].content)  # pero sabe que la hubo


class VisionConfigTest(unittest.TestCase):
    def test_off_by_default_and_must_be_true_or_false(self):
        self.assertFalse(config_from_dict({}).brain.vision)
        self.assertTrue(config_from_dict({"brain": {"vision": True}}).brain.vision)
        with self.assertRaises(ConfigError):
            config_from_dict({"brain": {"vision": "sí"}})


class ImageApiTest(unittest.TestCase):
    def start(self, vision):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        devices = DeviceRegistry(Path(self.dir.name) / "devices.json")
        self.key = devices.add("movil")
        self.got = []

        def handle_text(text, images=()):
            self.got.append((text, images))
            return "Veo un gato." if images else "Sin foto."

        server = CoreServer(bus=EventBus(), devices=devices, handle_text=handle_text, host="127.0.0.1", port=0,
                            vision=vision)
        server.start()
        self.addCleanup(server.stop)
        self.url = f"http://127.0.0.1:{server.port}"

    def post(self, body):
        req = urllib.request.Request(self.url + "/api/v1/message", json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_a_photo_goes_with_the_text(self):
        self.start(vision=True)
        photo = b64(JPEG + b"\x00" * 200_000)  # más de 16 KB: con visión, /message acepta fotos grandes
        self.assertEqual(self.post({"text": "¿qué ves?", "image": photo}), (200, {"reply": "Veo un gato."}))
        self.assertEqual(self.got, [("¿qué ves?", (photo,))])
        self.assertEqual(self.post({"text": "hola"}), (200, {"reply": "Sin foto."}))  # sin foto, como siempre

    def test_bad_photo_never_reaches_the_brain(self):
        self.start(vision=True)
        code, body = self.post({"text": "mira", "image": b64(b"<svg onload=alert(1)>")})
        self.assertEqual(code, 400)
        self.assertIn("JPEG", body["error"])
        self.assertEqual(self.got, [])

    def test_without_vision_photos_are_refused(self):
        self.start(vision=False)
        code, body = self.post({"text": "mira", "image": b64(JPEG)})
        self.assertEqual(code, 400)
        self.assertIn("no ve fotos", body["error"])
        # Y el límite de tamaño sigue siendo el pequeño (16 KB): nadie puede subir megas a un cerebro sin vista.
        self.assertEqual(self.post({"text": "x" * 10, "relleno": "a" * 20_000})[0], 413)
        self.assertEqual(self.got, [])


if __name__ == "__main__":
    unittest.main()
