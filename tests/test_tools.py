import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from arthur.core.config import SecurityConfig
from arthur.core.events import EventBus
from arthur.core.router import ToolRouter
from arthur.security.audit import AuditLog
from arthur.security.policy import Policy
from arthur.tools import build_registry
from arthur.tools.base import ToolCall

from .helpers import ScriptedConfirmer


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "notas.txt").write_text("hola mundo")
        (self.root / ".env").write_text("API_KEY=secreto")
        (self.root / "sub").mkdir()
        self.confirmer = ScriptedConfirmer()
        cfg = SecurityConfig(read_roots=(str(self.root),), write_roots=(str(self.root),))
        self.router = ToolRouter(build_registry(cfg), Policy(cfg), self.confirmer, AuditLog(None), EventBus())

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, name, **args):
        return self.router.execute(ToolCall(name, args))

    def test_registry_and_schemas(self):
        names = [t.name for t in self.router.registry.all()]
        self.assertEqual(names, sorted([
            "calendar_events", "close_window", "compose_email", "compose_whatsapp", "create_event", "delete_event", "focus_window",
            "forget_contact",
            "gmail_inbox", "gmail_read", "install_package", "search_packages",
            "get_system_info", "list_applications", "list_contacts", "list_directory",
            "list_workspaces", "media", "move_window", "open_application", "open_url",
            "read_clipboard", "read_file", "reply_email", "run_command", "save_contact", "search_music", "send_notification", "volume",
            "switch_workspace", "update_event", "write_clipboard", "write_file",
        ]))
        schema = self.router.registry.get("write_file").json_schema()
        self.assertEqual(schema["input_schema"]["required"], ["path", "content"])

    def test_system_info(self):
        result = self.call("get_system_info")
        self.assertEqual(result.status, "ok")
        self.assertIn("Kernel:", result.output)

    def test_list_directory_hides_sensitive_entries(self):
        result = self.call("list_directory", path=str(self.root))
        self.assertEqual(result.status, "ok")
        self.assertIn("sub/", result.output)
        self.assertIn("notas.txt", result.output)
        self.assertNotIn(".env", result.output)

    def test_read_file(self):
        self.assertEqual(self.call("read_file", path=str(self.root / "notas.txt")).output, "hola mundo")
        cut = self.call("read_file", path=str(self.root / "notas.txt"), max_bytes=4).output
        self.assertTrue(cut.startswith("hola\n…"))
        self.assertEqual(self.call("read_file", path=str(self.root / ".env")).status, "denied")
        self.assertEqual(self.call("read_file", path="/etc/hostname").status, "denied")

    def test_read_binary_file_is_refused(self):
        (self.root / "foto.bin").write_bytes(b"\x89PNG\0\0")
        result = self.call("read_file", path=str(self.root / "foto.bin"))
        self.assertEqual(result.status, "error")
        self.assertIn("binario", result.output)

    def test_write_file_creates_after_confirmation(self):
        self.confirmer.answers = [True]
        result = self.call("write_file", path=str(self.root / "nuevo.txt"), content="hola")
        self.assertEqual(result.status, "ok")
        self.assertEqual((self.root / "nuevo.txt").read_text(), "hola")
        self.assertIn("Crear", self.confirmer.requests[0].summary)
        self.assertEqual(stat.S_IMODE((self.root / "nuevo.txt").stat().st_mode), 0o666 & ~0o022)

    def test_write_file_cancelled(self):
        self.confirmer.answers = [False]
        self.assertEqual(self.call("write_file", path=str(self.root / "nuevo.txt"), content="x").status, "cancelled")
        self.assertFalse((self.root / "nuevo.txt").exists())

    def test_write_file_refuses_existing_without_overwrite(self):
        result = self.call("write_file", path=str(self.root / "notas.txt"), content="adiós")
        self.assertEqual(result.status, "denied")
        self.assertEqual(self.confirmer.requests, [])  # ni siquiera llega a preguntar
        self.assertEqual((self.root / "notas.txt").read_text(), "hola mundo")

    def test_overwrite_keeps_backup_and_permissions(self):
        (self.root / "notas.txt").chmod(0o640)
        self.confirmer.answers = [True]
        result = self.call("write_file", path=str(self.root / "notas.txt"), content="adiós", overwrite=True)
        self.assertEqual(result.status, "ok")
        self.assertEqual((self.root / "notas.txt").read_text(), "adiós")
        backups = list(self.root.glob("notas.txt.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "hola mundo")
        self.assertEqual(stat.S_IMODE((self.root / "notas.txt").stat().st_mode), 0o640)

    def test_write_outside_root_is_denied(self):
        self.assertEqual(self.call("write_file", path="/etc/arthur.txt", content="x").status, "denied")

    def test_rm_rf_is_denied_without_asking(self):
        result = self.call("run_command", command=["rm", "-rf", "/"])
        self.assertEqual(result.status, "denied")
        self.assertEqual(self.confirmer.requests, [])

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap no está instalado")
    def test_run_command_after_confirmation(self):
        self.confirmer.answers = [True]
        result = self.call("run_command", command=["ls", str(self.root)])
        self.assertEqual(result.status, "ok")
        self.assertIn("notas.txt", result.output)
        self.assertIn("código de salida 0", result.output)
        self.assertIn("Ejecutar en sandbox: ls", self.confirmer.requests[0].summary)


if __name__ == "__main__":
    unittest.main()
