"""Tests del modo servicio y del cliente que se conecta a él."""

import dataclasses
import io
import logging
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arthur.app import build_app
from arthur.api.control import send_command
from arthur.core.config import BrainConfig, Config
from arthur.interfaces.service import run_client, run_service, service_running
from arthur.security.audit import AuditLog
from arthur.security.confirm import DenyAll

from .helpers import fake_input
from .test_listen import FakeListener


class ServiceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="arthur-", dir="/tmp")  # ruta corta: los sockets tienen límite de longitud
        self.path = Path(tmp) / "control.sock"
        self.app = build_app(Config(brain=BrainConfig(provider="rules")), confirmer=DenyAll(), audit=AuditLog(None))
        self.app = dataclasses.replace(self.app, media=None, power_watch=False)  # nunca tocar la música ni el escritorio real
        self.stop = threading.Event()
        self.thread = threading.Thread(target=run_service, args=(self.app,), daemon=True, kwargs={
            "orb_url": lambda: "http://127.0.0.1:8765/?t=abc", "control_path": self.path, "stop": self.stop})
        self.thread.start()
        deadline = time.monotonic() + 3
        while not self.path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    def tearDown(self):
        self.stop.set()
        self.thread.join(3)
        self.assertFalse(self.path.exists())  # al parar, el buzón desaparece
        self.path.parent.rmdir()

    def test_status_ask_and_orb_url(self):
        self.assertTrue(service_running(self.path))
        self.assertEqual(send_command("ask hola", self.path), "Hola. ¿En qué puedo ayudarte?")
        self.assertEqual(send_command("orb-url", self.path), "http://127.0.0.1:8765/?t=abc")
        self.assertIn("error", send_command("ask", self.path))
        self.assertIn("error", send_command("toggle-listen", self.path))  # sin escucha configurada, no existe

    def test_client(self):
        out = io.StringIO()
        code = run_client(self.path, input_fn=fake_input(["¿quién eres?", "/memoria", "", "/salir"]), output=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("Conectado al ARTHUR que ya está en marcha", text)
        self.assertIn("arthur › Soy ARTHUR", text)
        self.assertIn("En modo conectado solo se puede conversar", text)


class ServiceLogPrivacyTest(unittest.TestCase):
    """En modo servicio el registro (journal y arthur.log) solo cuenta lo que pasa, nunca lo que dices ni lo que
    te responde: eso solo sale con el nivel DEBUG (como ya prometen el README y logging_setup)."""

    def run_service_and(self, interact, *, listener=None):
        records = []  # (nivel, mensaje) de todo lo que ARTHUR escribe en su registro

        class Capture(logging.Handler):
            def emit(self, record):
                records.append((record.levelno, record.getMessage()))

        logger = logging.getLogger("arthur")
        handler, old_level = Capture(), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (logger.removeHandler(handler), logger.setLevel(old_level)))
        tmp = tempfile.mkdtemp(prefix="arthur-", dir="/tmp")
        path = Path(tmp) / "control.sock"
        app = build_app(Config(brain=BrainConfig(provider="rules")), confirmer=DenyAll(), audit=AuditLog(None))
        app = dataclasses.replace(app, media=None, power_watch=False, listener=(listener, listener) if listener else None)
        stop = threading.Event()
        thread = threading.Thread(target=run_service, args=(app,), daemon=True, kwargs={
            "orb_url": lambda: None, "control_path": path, "stop": stop})
        thread.start()
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        interact(path, records)
        stop.set()
        thread.join(3)
        path.parent.rmdir()
        return records

    def assert_private(self, records, secrets):
        info = [text for level, text in records if level >= logging.INFO]
        everything = [text for _, text in records]
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertFalse(any(secret in text for text in info), f"{secret!r} está en el registro normal")
                self.assertTrue(any(secret in text for text in everything), f"{secret!r} ni siquiera sale en DEBUG")

    def test_replies_are_only_logged_at_debug(self):
        records = self.run_service_and(lambda path, _: send_command("ask quien eres", path))
        self.assert_private(records, ["Soy ARTHUR, tu asistente personal"])

    def test_what_you_say_by_voice_is_only_logged_at_debug(self):
        def talk(path, records):
            self.assertEqual(send_command("toggle-listen", path), "escuchando")
            self.assertEqual(send_command("toggle-listen", path), "procesando")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any("De nada" in text for _, text in list(records)):
                time.sleep(0.05)

        records = self.run_service_and(talk, listener=FakeListener("muchas gracias"))
        self.assert_private(records, ["muchas gracias", "De nada."])
        # Lo que sí sigue contando el registro normal: el estado, para poder diagnosticar (journalctl … | grep).
        self.assertTrue(any("Te escucho" in text for level, text in records if level >= logging.INFO))
        self.assertTrue(any("transcribiendo" in text for level, text in records if level >= logging.INFO))


class NoServiceTest(unittest.TestCase):
    def test_not_running(self):
        self.assertFalse(service_running(Path("/tmp/no-hay-arthur.sock")))

    def test_client_when_service_dies(self):
        out = io.StringIO()
        self.assertEqual(run_client(Path("/tmp/no-hay-arthur.sock"), input_fn=fake_input(["hola"]), output=out), 1)
        self.assertIn("ya no responde", out.getvalue())


if __name__ == "__main__":
    unittest.main()
