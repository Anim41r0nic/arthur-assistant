"""Tests de la Fase 5. Usan programas, apps y Hyprland FALSOS: nunca tocan el escritorio real."""

import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from arthur.core.config import SecurityConfig
from arthur.core.events import EventBus
from arthur.core.router import ToolRouter
from arthur.integrations.desktop_entries import AppCatalog
from arthur.integrations.hyprland import HyprlandReader, HyprlandUnavailable
from arthur.security.audit import AuditLog
from arthur.security.policy import Policy
from arthur.tools import build_registry
from arthur.tools.base import Risk, ToolCall, ToolError

from .helpers import ScriptedConfirmer


class FakePrograms:
    """Apunta qué se habría ejecutado y responde lo que le preparemos."""

    def __init__(self):
        self.calls = []
        self.outputs = {}  # programa → salida (o excepción)

    def _answer(self, argv):
        out = self.outputs.get(argv[0], "")
        if isinstance(out, Exception):
            raise out
        return out

    def run(self, argv, *, input=None, timeout=10):
        self.calls.append(("run", argv))
        return self._answer(argv)

    def launch(self, argv, *, grace=2.0):
        self.calls.append(("launch", argv))

    def feed(self, argv, data, *, timeout=5):
        self.calls.append(("feed", argv, data))


DESKTOP = """[Desktop Entry]
Type=Application
Name={name}
{extra}
Exec={exec}
"""


def write_app(directory, app_id, name, exec_="prog %U", extra=""):
    path = directory / f"{app_id}.desktop"
    path.write_text(DESKTOP.format(name=name, exec=exec_, extra=extra))
    return path


class FakeHyprland:
    """Servidor en un socket Unix que imita las respuestas de Hyprland."""

    def __init__(self, directory, windows):
        self.path = Path(directory) / ".socket.sock"
        self.requests = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen()
        self.windows = windows
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                self.requests.append(conn.recv(1024).decode())
                conn.sendall(json.dumps(self.windows).encode())

    def close(self):
        self.server.close()


class DesktopEntriesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.user = Path(tmp.name) / "user"
        self.system = Path(tmp.name) / "system"
        for d in (self.user, self.system / "sub"):
            d.mkdir(parents=True)
        write_app(self.system, "firefox", "Firefox", extra="GenericName[es]=Navegador web\nKeywords=internet;web;")
        write_app(self.system, "codeblocks", "Code::Blocks IDE", extra="Categories=Development;IDE;GTK;")
        write_app(self.system, "gparted", "GParted", exec_="/usr/bin/pkexec gparted")
        write_app(self.system, "oculta", "Oculta", extra="NoDisplay=true")
        write_app(self.system / "sub", "anidada", "Anidada")
        (self.system / "rota.desktop").write_text("esto no es un desktop\x00")
        # La del usuario tiene prioridad sobre la del sistema.
        write_app(self.user, "firefox", "Firefox (mío)", extra="GenericName[es]=Navegador web\nKeywords=internet;web;")
        self.catalog = AppCatalog([self.user, self.system])

    def test_load(self):
        apps = self.catalog.load()
        self.assertEqual(sorted(apps), ["codeblocks", "firefox", "gparted", "sub-anidada"])
        self.assertEqual(apps["firefox"].name, "Firefox (mío)")
        self.assertTrue(apps["gparted"].privileged)
        self.assertFalse(apps["codeblocks"].privileged)

    def test_search(self):
        self.assertEqual([a.id for a in self.catalog.search("codeblocks")], ["codeblocks"])
        self.assertEqual([a.id for a in self.catalog.search("code::blocks ide")], ["codeblocks"])
        self.assertEqual([a.id for a in self.catalog.search("NAVEGADOR")], ["firefox"])
        self.assertEqual([a.id for a in self.catalog.search("internet")], ["firefox"])
        self.assertEqual([a.id for a in self.catalog.search("programar")], ["codeblocks"])
        self.assertEqual([a.id for a in self.catalog.search("Programación")], ["codeblocks"])
        self.assertEqual(self.catalog.search("photoshop"), [])
        self.assertEqual(len(self.catalog.search("")), 4)


class HyprlandReaderTest(unittest.TestCase):
    def test_reads_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeHyprland(tmp, [{"class": "firefox", "title": "Hola"}])
            self.addCleanup(fake.close)
            reader = HyprlandReader(fake.path)
            self.assertEqual(reader.windows(), [{"class": "firefox", "title": "Hola"}])
            self.assertEqual(fake.requests, ["j/clients"])

    def test_only_read_requests_are_possible(self):
        reader = HyprlandReader(Path("/no/existe"))
        for request in ("dispatch exec rm -rf ~", "j/clients;dispatch exit", "eval hl.exec_cmd('x')", "keyword x y"):
            with self.subTest(request=request), self.assertRaises(ValueError):
                reader.query(request)

    def test_outside_hyprland(self):
        with self.assertRaises(HyprlandUnavailable):
            HyprlandReader(Path("/no/existe")).windows()


class DesktopToolsTest(DesktopEntriesTest):
    def setUp(self):
        super().setUp()
        # open_application abre con `uwsm app` solo si uwsm existe: los tests no pueden depender de que la
        # máquina que los ejecuta lo tenga instalado (en un equipo de verdad sí; en la nube no).
        which = mock.patch.object(shutil, "which", lambda name: "/usr/bin/uwsm" if name == "uwsm" else None)
        which.start()
        self.addCleanup(which.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hypr = FakeHyprland(tmp.name, [
            {"class": "firefox", "title": "Wikipedia", "workspace": {"name": "2"}},
            {"class": "Alacritty", "title": "~", "workspace": {"name": "1"}},
        ])
        self.addCleanup(self.hypr.close)
        self.programs = FakePrograms()
        self.confirmer = ScriptedConfirmer()
        cfg = SecurityConfig(trusted_apps=("firefox", "codeblocks"))
        registry = build_registry(cfg, programs=self.programs, catalog=self.catalog,
                                  hyprland=HyprlandReader(self.hypr.path))
        self.router = ToolRouter(registry, Policy(cfg), self.confirmer, AuditLog(None), EventBus())

    def call(self, tool, **args):
        return self.router.execute(ToolCall(tool, args))

    # --- aplicaciones ---
    def test_trusted_app_opens_without_confirmation(self):
        result = self.call("open_application", name="Firefox")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.confirmer.requests, [])
        self.assertEqual(self.programs.calls, [("launch", ["uwsm", "app", "--", "firefox.desktop"])])

    def test_other_app_needs_confirmation(self):
        result = self.call("open_application", name="anidada")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.programs.calls, [])
        self.assertEqual(self.confirmer.requests[0].risk, Risk.WRITE)
        self.assertIn("Abrir la aplicación Anidada", self.confirmer.requests[0].summary)

    def test_privileged_app_is_dangerous_even_if_trusted(self):
        cfg = SecurityConfig(trusted_apps=("gparted",))
        registry = build_registry(cfg, programs=self.programs, catalog=self.catalog)
        router = ToolRouter(registry, Policy(cfg), self.confirmer, AuditLog(None), EventBus())
        self.assertEqual(router.execute(ToolCall("open_application", {"name": "gparted"})).status, "cancelled")
        self.assertEqual(self.confirmer.requests[0].risk, Risk.DANGEROUS)
        self.assertIn("administrador", self.confirmer.requests[0].summary)

    def test_unknown_or_ambiguous_app(self):
        self.assertIn("No encuentro", self.call("open_application", name="photoshop").output)
        write_app(self.system, "firefox-dev", "Firefox Developer", extra="Keywords=web;")
        ambiguous = self.call("open_application", name="web")
        self.assertEqual(ambiguous.status, "error")
        self.assertIn("varias aplicaciones", ambiguous.output)
        self.assertEqual(self.programs.calls, [])

    def test_list_applications(self):
        out = self.call("list_applications").output
        self.assertIn("Firefox (mío) (id: firefox) · de confianza", out)
        self.assertNotIn("Oculta", out)

    # --- notificaciones y volumen ---
    def test_notification(self):
        self.assertEqual(self.call("send_notification", title="-rf", body="hola").status, "ok")
        self.assertEqual(self.programs.calls, [("run", ["notify-send", "--app-name=ARTHUR", "--", "-rf", "hola"])])
        self.assertEqual(self.confirmer.requests, [])

    def test_volume_without_arguments_just_reports(self):
        self.programs.outputs["wpctl"] = "Volume: 0.40 [MUTED]\n"
        self.assertEqual(self.call("volume").output, "Volumen al 40 % (silenciado)")

    def test_set_volume(self):
        self.assertEqual(self.call("volume", percent=35, muted=False).status, "ok")
        self.assertEqual(self.programs.calls, [
            ("run", ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.35"]),
            ("run", ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"]),
        ])
        for args in ({"percent": 150}, {"percent": -1}):
            with self.subTest(args=args):
                self.assertEqual(self.call("volume", **args).status, "error")

    # --- portapapeles ---
    def test_read_clipboard_always_asks(self):
        self.programs.outputs["wl-paste"] = "contraseña123"
        result = self.call("read_clipboard")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.programs.calls, [])
        self.assertEqual(self.confirmer.requests[0].risk, Risk.PRIVATE)
        self.confirmer.answers = [True]
        self.assertEqual(self.call("read_clipboard").output, "contraseña123")

    def test_empty_clipboard(self):
        self.programs.outputs["wl-paste"] = ToolError("No selection")
        self.confirmer.answers = [True]
        self.assertIn("vacío", self.call("read_clipboard").output)

    def test_write_clipboard(self):
        self.confirmer.answers = [True]
        self.assertEqual(self.call("write_clipboard", text="hola").status, "ok")
        self.assertEqual(self.programs.calls, [("feed", ["wl-copy"], "hola")])

    # --- enlaces ---
    def test_open_url(self):
        self.confirmer.answers = [True]
        self.assertEqual(self.call("open_url", url="https://es.wikipedia.org/wiki/Linux").status, "ok")
        self.assertEqual(self.programs.calls, [("launch", ["xdg-open", "https://es.wikipedia.org/wiki/Linux"])])

    def test_dangerous_urls_are_denied_without_asking(self):
        # «http://[::1» no es una dirección IPv6 válida: urlsplit lanza ValueError, y eso debe ser un «no», no un fallo.
        for url in ("file:///etc/passwd", "javascript:alert(1)", "apt:sl", "https://", "https://a.com/ x", "ftp://a.com",
                    "http://[::1", "https://[a.com]/"):
            with self.subTest(url=url):
                self.assertEqual(self.call("open_url", url=url).status, "denied")
        self.assertEqual(self.confirmer.requests, [])
        self.assertEqual(self.programs.calls, [])

    # --- modo seguro ---
    def test_safe_mode_allows_only_queries(self):
        cfg = SecurityConfig(safe_mode=True, trusted_apps=("firefox",))
        registry = build_registry(cfg, programs=self.programs, catalog=self.catalog,
                                  hyprland=HyprlandReader(self.hypr.path))
        router = ToolRouter(registry, Policy(cfg), self.confirmer, AuditLog(None), EventBus())
        self.assertEqual(router.execute(ToolCall("open_application", {"name": "firefox"})).status, "denied")
        self.assertEqual(router.execute(ToolCall("send_notification", {"title": "x"})).status, "denied")
        self.assertEqual(router.execute(ToolCall("list_applications", {})).status, "ok")
        self.assertEqual(self.programs.calls, [])


if __name__ == "__main__":
    unittest.main()


class ProgramsLaunchTest(unittest.TestCase):
    """Usa programas inofensivos del sistema (sleep, false) para probar la espera de arranque."""

    def test_long_running_program_counts_as_started(self):
        from arthur.tools.programs import Programs
        Programs().launch(["sleep", "3"], grace=0.3)

    def test_failing_program_is_reported(self):
        from arthur.tools.programs import Programs
        with self.assertRaisesRegex(ToolError, "no ha podido abrir"):
            Programs().launch(["false"], grace=2)
