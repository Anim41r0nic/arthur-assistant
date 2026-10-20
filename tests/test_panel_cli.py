"""Órdenes del panel (panel/cli.py): alta de aparatos, autorizar Google y el montaje desde el entorno. Sin red."""

import contextlib
import io
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from arthur.integrations.google import GoogleError
from arthur.panel import cli
from arthur.panel.google_keyring import EncryptedFileKeyring
from arthur.panel.settings import load_settings
from arthur.panel.sources.google import PANEL_SCOPES
from arthur.panel.store import PanelStore
from arthur.security.devices import DeviceRegistry

MADRID = ZoneInfo("Europe/Madrid")


class FakeAuth:
    def __init__(self, connected=True, fail=False):
        self._connected, self._fail, self.calls = connected, fail, []

    def connected(self):
        return self._connected

    def authorize(self, scopes, open_browser, timeout=300, paste=None):
        self.calls.append((scopes, paste))
        open_browser("https://accounts.example/auth")
        if self._fail:
            raise GoogleError("la dirección no es válida")
        paste("http://127.0.0.1/?code=abc")


class DeviceTest(unittest.TestCase):
    def test_key_printed_once_and_stored_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "devices.json"
            shown = []
            self.assertEqual(cli.new_device("movil", DeviceRegistry(path), out=shown.append), 0)
            key = shown[0].split("\n\n")[1].strip()
            self.assertTrue(key)
            self.assertNotIn(key, path.read_text(encoding="utf-8"))  # solo queda el hash
            self.assertEqual(DeviceRegistry(path).check(key), "movil")
            # repetir el nombre no da otra llave
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.new_device("movil", DeviceRegistry(path), out=shown.append), 1)
            self.assertEqual(len(shown), 1)

    def test_command_wires_the_flag(self):
        args = mock.Mock(panel_clave_gemini=False, panel_dispositivo_nuevo="movil", panel_google=False, panel_web=False)
        with mock.patch.object(cli, "new_device", return_value=0) as nd:
            self.assertEqual(cli.panel_command(args, {}), 0)
        nd.assert_called_once_with("movil")


class GoogleTest(unittest.TestCase):
    def test_pastes_the_address_and_asks_read_only_scopes(self):
        auth = FakeAuth()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.connect_google(auth, ask=lambda _: "x")
        self.assertEqual(code, 0)
        scopes, paste = auth.calls[0]
        self.assertEqual(scopes, PANEL_SCOPES)
        self.assertTrue(all(s.endswith(".readonly") for s in scopes))
        self.assertIsNotNone(paste)
        self.assertIn("Conectado", out.getvalue())

    def test_failure_is_reported(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.connect_google(FakeAuth(fail=True)), 1)
        self.assertIn("no es válida", out.getvalue())


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.settings = load_settings(self.dir / "ajustes.toml")

    def secret(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def build(self, env, **kw):
        return cli.build_runtime(env, self.settings, auth=kw.pop("auth", FakeAuth()),
                                 key_file=self.dir / "gemini-key", **kw)

    def test_defaults(self):
        rt = self.build({})
        self.assertEqual((rt.host, rt.port), ("127.0.0.1", 8775))
        self.assertEqual(rt.vault.root, Path("/vault"))
        self.assertIsNone(rt.studio_link)
        self.assertIsNone(rt.notifier)
        self.assertIsNotNone(rt.calendar)
        self.assertIsNotNone(rt.gmail)

    def test_google_usa_almacen_cifrado_del_panel(self):
        with mock.patch("arthur.integrations.google.GoogleAuth", return_value=FakeAuth(connected=False)) as auth:
            cli.build_runtime({}, self.settings, key_file=self.dir / "gemini-key")
        self.assertIsInstance(auth.call_args.kwargs["keyring"], EncryptedFileKeyring)

    def test_vault_receives_configured_file_allowlist(self):
        settings = SimpleNamespace(vault=self.dir, files=("00-Inicio/Tareas.md", "12-Estudio/Repaso.md"))
        with mock.patch("arthur.panel.sources.obsidian.ObsidianVault") as vault_class:
            cli.build_runtime({}, settings, auth=FakeAuth(), key_file=self.dir / "gemini-key")
        vault_class.assert_called_once_with(self.dir, files=settings.files)

    def test_google_not_authorized_skips_its_cards(self):
        rt = self.build({}, auth=FakeAuth(connected=False))
        self.assertEqual((rt.calendar, rt.gmail), (None, None))

    def test_everything_from_env(self):
        env = {"ARTHUR_PANEL_HOST": "100.1.2.3", "ARTHUR_PANEL_PORT": "9999", "ARTHUR_PANEL_VAULT": str(self.dir),
               "ARTHUR_PANEL_STUDIO_URL": "http://studio-web:8773",
               "ARTHUR_PANEL_STUDIO_LLAVE_FILE": self.secret("studio", "arthur_k\n"),
               "ARTHUR_PANEL_AVISOS_URL": "http://core:8770",
               "ARTHUR_PANEL_AVISOS_TOKEN_FILE": self.secret("avisos", "a" * 40),
               "ARTHUR_OLLAMA_URL": "http://ollama:11434"}
        rt = self.build(env)
        self.assertEqual((rt.host, rt.port, rt.vault.root), ("100.1.2.3", 9999, self.dir))
        self.assertEqual(rt.studio_link.url, "http://studio-web:8773")
        self.assertEqual(rt.notifier.url, "http://core:8770/api/v1/notice")

    def test_unreadable_studio_key_only_disables_studio(self):
        with self.assertLogs("arthur.panel", "ERROR"):
            rt = self.build({"ARTHUR_PANEL_STUDIO_URL": "http://studio-web:8773",
                             "ARTHUR_PANEL_STUDIO_LLAVE_FILE": "/no/existe"})
        self.assertIsNone(rt.studio_link)

    def test_cerebro_from_env(self):
        self.assertIsNone(self.build({}).cerebro)  # sin ARTHUR_CEREBRO_URL, sin caja
        rt = self.build({"ARTHUR_CEREBRO_URL": "http://cerebro:8778",
                         "ARTHUR_CEREBRO_LLAVE_FILE": self.secret("cerebro", "arthur_c\n")})
        self.assertEqual(rt.cerebro.url, "http://cerebro:8778")
        self.assertGreater(rt.cerebro.timeout, 60)  # Workers AI y, si falla, Ollama
        with self.assertLogs("arthur.panel", "ERROR"):
            rt = self.build({"ARTHUR_CEREBRO_URL": "http://cerebro:8778", "ARTHUR_CEREBRO_LLAVE_FILE": "/no/existe"})
        self.assertIsNone(rt.cerebro)

    def test_engines_without_gemini_workers_ai_then_local(self):
        # Marc no usa Gemini (sus condiciones piden plan de pago en el EEE): aunque quede una clave vieja, no se usa.
        (self.dir / "gemini-key").write_text("AIza" + "x" * 35, encoding="utf-8")
        rt = self.build({"ARTHUR_OLLAMA_URL": "http://ollama:11434"})
        self.assertEqual([e.name for e in rt.engines()], ["local"])
        self.assertEqual(rt.engines()[0].url, "http://ollama:11434")
        # Con la cuenta y el token de Workers AI (los mismos que el chat de la web): Workers AI y Ollama de respaldo
        env = {"ARTHUR_OLLAMA_URL": "http://ollama:11434", "ARTHUR_WEBCHAT_CF_ACCOUNT": "a" * 32,
               "ARTHUR_PANEL_CF_TOKEN_FILE": self.secret("workers", "t" * 40 + "\n")}
        engines = self.build(env).engines()
        self.assertEqual([e.name for e in engines], ["workers-ai", "local"])
        self.assertNotIn("t" * 40, repr(engines[0]))

    def test_workers_ai_missing_or_bad_token_falls_back_to_local(self):
        account = {"ARTHUR_WEBCHAT_CF_ACCOUNT": "a" * 32}
        with self.assertLogs("arthur.panel", "WARNING"):
            rt = self.build({**account, "ARTHUR_PANEL_CF_TOKEN_FILE": str(self.dir / "no-existe")})
        self.assertEqual([e.name for e in rt.engines()], ["local"])
        with self.assertLogs("arthur.panel", "WARNING") as logs:
            rt = self.build({**account, "ARTHUR_PANEL_CF_TOKEN_FILE": self.secret("malo", "con espacios no vale")})
        self.assertEqual([e.name for e in rt.engines()], ["local"])
        self.assertNotIn("con espacios", "\n".join(logs.output))
        rt = self.build({"ARTHUR_PANEL_CF_TOKEN_FILE": self.secret("workers", "t" * 40)})  # sin cuenta
        self.assertEqual([e.name for e in rt.engines()], ["local"])

    def test_gemini_key_command_no_longer_saves(self):
        args = mock.Mock(panel_clave_gemini=True, panel_dispositivo_nuevo=None, panel_google=False, panel_web=False)
        out = io.StringIO()
        with mock.patch("getpass.getpass") as ask, contextlib.redirect_stdout(out), \
                mock.patch.object(cli, "gemini_key_file", return_value=self.dir / "gemini-key"):
            self.assertEqual(cli.panel_command(args, {}), 0)
        ask.assert_not_called()
        self.assertFalse((self.dir / "gemini-key").exists())
        self.assertIn("Workers AI", out.getvalue())


class BriefingFnTest(unittest.TestCase):
    def test_redo_saves_without_notice_and_keeps_delivered(self):
        class Engine:
            name = "falso"

            def ask(self, prompt):
                return "Buenos días, Marc."

        store = PanelStore.in_memory()
        self.addCleanup(store.close)
        now = lambda: datetime(2026, 10, 16, 9, 0, tzinfo=MADRID)  # noqa: E731
        store.save_briefing(date(2026, 10, 16), "viejo", "plantilla", True)
        day = {"date": "2026-10-16", "now": "", "next": {"current": None, "following": None}, "tasks": [],
               "progress": {"done": 0, "total": 0, "percent": 0}, "events": [], "mail": [], "problems": []}
        redo = cli.make_briefing_fn(store, lambda: day, lambda: [Engine()], now)
        self.assertEqual(redo(), {"date": "2026-10-16", "text": "Buenos días, Marc.", "engine": "falso"})
        saved = store.last_briefing(date(2026, 10, 16))
        self.assertEqual((saved["text"], saved["delivered"]), ("Buenos días, Marc.", True))
        self.assertTrue(store.briefing_sent(date(2026, 10, 16)))


if __name__ == "__main__":
    unittest.main()
