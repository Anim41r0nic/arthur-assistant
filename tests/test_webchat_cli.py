"""Órdenes del asistente de la web (webchat/cli.py): montaje desde el entorno, migración y reintentos. Sin red ni servidor."""

import io
import json
import logging
import sqlite3
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from arthur.webchat import cli
from arthur.webchat.clientes_link import ClientesLink
from arthur.webchat.limits import Limits

AHORA = lambda: datetime(2026, 10, 16, 10, tzinfo=timezone.utc)  # noqa: E731
CUENTA = "0123456789abcdef0123456789abcdef"
TOKEN = "TokEnSecReto_" + "x" * 27


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def build(self, extra=None):
        env = {"XDG_DATA_HOME": str(self.dir), **(extra or {})}
        return cli.build_runtime(env, now=AHORA)

    def test_defaults(self):
        rt = self.build({"ARTHUR_WEBCHAT_CF_TOKEN_FILE": str(self.dir / "no-existe")})
        self.assertEqual((rt.host, rt.port), ("127.0.0.1", 8776))
        self.assertEqual(rt.origin, "https://ejemplo.com")
        self.assertEqual(rt.knowledge.path, Path("/conocimiento/conocimiento.md"))
        self.assertEqual(rt.limits, Limits())
        self.assertIsNone(rt.notifier)
        self.assertIsNone(rt.clientes)   # sin ARTHUR_WEBCHAT_CLIENTES_URL: el contacto contestará 503

    def test_clientes_link_from_env(self):
        token = self.dir / "clientes_webchat"
        token.write_text(TOKEN + "\n", encoding="utf-8")
        rt = self.build({"ARTHUR_WEBCHAT_CLIENTES_URL": "http://clientes:8777",
                         "ARTHUR_WEBCHAT_CLIENTES_TOKEN_FILE": str(token)})
        self.assertIsInstance(rt.clientes, ClientesLink)
        self.assertEqual(rt.clientes.url, "http://clientes:8777/api/interno/contacto")
        self.assertEqual(rt.clientes.queue_dir, self.dir / "arthur" / "webchat" / "cola")

    def test_clientes_default_token_path(self):
        self.assertEqual(cli.DEFAULT_CLIENTES_TOKEN, "/run/secrets/clientes_webchat")

    def test_clientes_without_token_or_with_public_url_is_none_and_hides_token(self):
        token = self.dir / "clientes_webchat"
        token.write_text(TOKEN, encoding="utf-8")
        for extra in ({"ARTHUR_WEBCHAT_CLIENTES_URL": "http://clientes:8777",
                       "ARTHUR_WEBCHAT_CLIENTES_TOKEN_FILE": str(self.dir / "no-existe")},
                      {"ARTHUR_WEBCHAT_CLIENTES_URL": "https://clientes.example.com",
                       "ARTHUR_WEBCHAT_CLIENTES_TOKEN_FILE": str(token)}):
            with self.subTest(extra=extra), self.assertLogs("arthur.webchat", "WARNING") as cm:
                rt = self.build(extra)
            self.assertIsNone(rt.clientes)
            self.assertNotIn(TOKEN, "\n".join(cm.output))

    def test_everything_from_env(self):
        turnstile = self.dir / "ts"
        turnstile.write_text("secreto\n", encoding="utf-8")
        token = self.dir / "token"
        token.write_text("a" * 40, encoding="utf-8")
        rt = self.build({
            "ARTHUR_WEBCHAT_HOST": "0.0.0.0", "ARTHUR_WEBCHAT_PORT": "9000",
            "ARTHUR_WEBCHAT_ORIGIN": "https://otra.example",
            "ARTHUR_WEBCHAT_CONOCIMIENTO": str(self.dir / "c.md"),
            **self.cf(),
            "ARTHUR_WEBCHAT_TURNSTILE_FILE": str(turnstile),
            "ARTHUR_WEBCHAT_POR_HORA": "5",
            "ARTHUR_WEBCHAT_AVISOS_URL": "http://core:8770", "ARTHUR_WEBCHAT_AVISOS_TOKEN_FILE": str(token),
            "ARTHUR_OLLAMA_URL": "http://ollama:11434"})
        self.assertEqual((rt.host, rt.port, rt.origin), ("0.0.0.0", 9000, "https://otra.example"))
        self.assertEqual(rt.knowledge.path, self.dir / "c.md")
        self.assertEqual(rt.limits.per_hour, 5)
        self.assertEqual(rt.workers_ai.name, "workers-ai")
        self.assertEqual(rt.local.url, "http://ollama:11434")
        self.assertEqual(rt.notifier.url, "http://core:8770/api/v1/notice")

    def cf(self, extra=None):
        token = self.dir / "cf"
        token.write_text(TOKEN + "\n", encoding="utf-8")
        return {"ARTHUR_WEBCHAT_CF_ACCOUNT": CUENTA, "ARTHUR_WEBCHAT_CF_TOKEN_FILE": str(token), **(extra or {})}

    def test_workers_ai_from_env(self):
        rt = self.build(self.cf())
        self.assertEqual((rt.workers_ai.name, rt.workers_ai.account), ("workers-ai", CUENTA))
        self.assertEqual(rt.workers_ai.model, "@cf/google/gemma-4-26b-a4b-it")
        self.assertIs(rt.answerer._workers_ai, rt.workers_ai)

    def test_workers_ai_model_from_env(self):
        rt = self.build(self.cf({"ARTHUR_WEBCHAT_CF_MODEL": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"}))
        self.assertEqual(rt.workers_ai.model, "@cf/meta/llama-3.3-70b-instruct-fp8-fast")

    def test_without_account_or_token_still_builds(self):
        for extra in ({"ARTHUR_WEBCHAT_CF_TOKEN_FILE": str(self.dir / "no-existe")},
                      {"ARTHUR_WEBCHAT_CF_ACCOUNT": CUENTA, "ARTHUR_WEBCHAT_CF_TOKEN_FILE": str(self.dir / "no")}):
            with self.assertLogs("arthur.webchat", "WARNING"):
                rt = self.build(extra)
            self.assertIsNone(rt.workers_ai)
            self.assertEqual(rt.local.name, "local")  # la respuesta cae al modelo local

    def test_bad_config_is_not_fatal_and_hides_the_token(self):
        for extra in ({"ARTHUR_WEBCHAT_CF_ACCOUNT": "no-es-un-id"}, {"ARTHUR_WEBCHAT_CF_MODEL": "../../x"}):
            with self.assertLogs("arthur.webchat", "WARNING") as cm:
                rt = self.build(self.cf(extra))
            self.assertIsNone(rt.workers_ai)
            self.assertNotIn(TOKEN, "\n".join(cm.output))

    def test_unreadable_token_is_not_fatal(self):
        # Los secretos de compose son ficheros montados con los permisos del anfitrión: si el contenedor no puede
        # leerlo (PermissionError, que es OSError), la web arranca igual con el modelo local.
        env = self.cf()
        from unittest import mock
        with mock.patch("pathlib.Path.read_text", side_effect=PermissionError("denegado")), \
                self.assertLogs("arthur.webchat", "WARNING"):
            rt = self.build(env)
        self.assertIsNone(rt.workers_ai)

    def test_default_token_path(self):
        self.assertEqual(cli.DEFAULT_CF_TOKEN, "/run/secrets/workers_ai_token")

    def test_unreadable_turnstile_secret_means_no_secret(self):
        rt = self.build({"ARTHUR_WEBCHAT_TURNSTILE_FILE": str(self.dir / "no-existe"),
                         "ARTHUR_WEBCHAT_CF_TOKEN_FILE": str(self.dir / "x")})
        self.assertIsNone(rt.turnstile.verify("t", "1.2.3.4"))  # «no disponible», no «válido»


class FakeClientes:
    def __init__(self, answers):
        self.answers = list(answers)
        self.bodies = []

    def open(self, req, timeout=None):
        self.bodies.append(json.loads(req.data))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text, **options):
        self.sent.append((text, options))
        return True


class StartupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.env = {"XDG_DATA_HOME": str(self.dir)}

    def link(self, answers):
        self.fake = FakeClientes(answers)
        return ClientesLink("http://clientes:8777", TOKEN, cli.webchat_dir(self.env) / "cola", opener=self.fake)

    def test_startup_migrates_old_table_and_delivers_with_notice(self):
        old = cli.contacts_file(self.env)
        old.parent.mkdir(parents=True)
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                   "name TEXT NOT NULL, email TEXT NOT NULL, need TEXT NOT NULL, lang TEXT NOT NULL)")
        db.execute("INSERT INTO contacts (created_at, name, email, need, lang) "
                   "VALUES ('2026-10-16T12:00:00+02:00', 'Ana', 'ana@example.com', 'Una web', 'es')")
        db.commit()
        db.close()
        link = self.link([{"id": 1, "nuevo": True}])
        notifier = FakeNotifier()
        self.assertEqual(cli.migrate_and_flush(link, notifier, self.env), 1)
        self.assertFalse(old.exists())
        self.assertEqual(self.fake.bodies[0]["email"], "ana@example.com")
        self.assertEqual(notifier.sent, [("Cliente nuevo C-0001", {"destination": "clientes", "client": 1})])

    def test_retry_loop_flushes_every_minute_until_stopped(self):
        link = self.link([urllib.error.URLError("caído"), {"id": 3, "nuevo": False}])
        with self.assertLogs("arthur.webchat", logging.WARNING):
            self.assertIsNone(link.deliver({"name": "A", "email": "a@b.co", "phone": "", "need": "x", "lang": "es"}))
        notifier = FakeNotifier()
        waits = []

        class Stop:
            def wait(self, seconds):
                waits.append(seconds)
                return len(waits) >= 2   # para tras la segunda vuelta

        cli._retry_loop(link, notifier, Stop())
        self.assertEqual(waits, [60, 60])
        self.assertEqual(notifier.sent, [("C-0003 ha vuelto a escribir", {"destination": "clientes", "client": 3})])
        self.assertEqual(list((cli.webchat_dir(self.env) / "cola").glob("*.json")), [])

    def test_retry_loop_survives_unexpected_errors(self):
        class Rota:
            def flush(self, on_delivered=None):
                raise RuntimeError("roto")

        stop = threading.Event()
        calls = []

        class Stop:
            def wait(self, seconds):
                calls.append(seconds)
                return len(calls) >= 2

        with self.assertLogs("arthur.webchat", logging.ERROR):
            cli._retry_loop(Rota(), None, Stop())
        self.assertEqual(len(calls), 2)
        self.assertFalse(stop.is_set())

    def test_old_commands_are_gone(self):
        # Los contactos ya no se ven ni se borran aquí, sino en la página de clientes: argparse los rechaza (código 2)
        # antes de abrir nada.
        from arthur.__main__ import main
        for argv in (["--webchat-contactos"], ["--webchat-borrar", "7"]):
            with self.subTest(argv=argv), unittest.mock.patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                main(argv)
            self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
