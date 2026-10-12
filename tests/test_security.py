import os
import shutil
import tempfile
import unittest
from pathlib import Path

from arthur.core.config import ConfigError, config_from_dict
from arthur.security.audit import redact
from arthur.security.commands import CommandDenied, CommandPolicy
from arthur.security.paths import PROJECT_DIR, PathDenied, PathPolicy
from arthur.security.sandbox import Sandbox


class TempHome(unittest.TestCase):
    """Crea una carpeta personal falsa con un proyecto, secretos y una carpeta de fuera."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.projects = self.home / "Proyectos"
        self.outside = self.home / "Privado"
        for d in (self.projects, self.outside, self.home / ".ssh"):
            d.mkdir()
        (self.projects / "notas.txt").write_text("hola")
        (self.projects / ".env").write_text("API_KEY=secreto")
        (self.outside / "diario.txt").write_text("privado")
        (self.home / ".ssh" / "id_ed25519").write_text("clave")
        self.paths = PathPolicy([self.projects], [self.projects], home=self.home)

    def tearDown(self):
        self._tmp.cleanup()


class PathPolicyTest(TempHome):
    def test_inside_root_is_allowed(self):
        self.assertEqual(self.paths.check_read(str(self.projects / "notas.txt")), self.projects / "notas.txt")

    def test_outside_root_is_denied(self):
        with self.assertRaisesRegex(PathDenied, "No tengo permiso"):
            self.paths.check_read(str(self.outside / "diario.txt"))

    def test_relative_paths_are_denied(self):
        with self.assertRaisesRegex(PathDenied, "absoluta"):
            self.paths.check_read("notas.txt")

    def test_dotdot_escape_is_denied(self):
        with self.assertRaises(PathDenied):
            self.paths.check_read(f"{self.projects}/../Privado/diario.txt")

    def test_symlink_escape_is_denied(self):
        link = self.projects / "atajo"
        link.symlink_to(self.outside)
        with self.assertRaises(PathDenied):
            self.paths.check_read(str(link / "diario.txt"))

    def test_sensitive_files_are_denied_even_inside_root(self):
        for name in (".env", "id_rsa", "servidor.pem", "claves.kdbx"):
            with self.subTest(name=name), self.assertRaisesRegex(PathDenied, "protegida"):
                self.paths.check_read(str(self.projects / name))

    def test_sensitive_dirs_are_denied_even_if_root_is_home(self):
        broad = PathPolicy([self.home], [self.home], home=self.home)
        with self.assertRaisesRegex(PathDenied, "protegida"):
            broad.check_read(str(self.home / ".ssh" / "id_ed25519"))

    def test_cannot_write_own_code(self):
        policy = PathPolicy([PROJECT_DIR.parent], [PROJECT_DIR.parent], home=self.home)
        with self.assertRaisesRegex(PathDenied, "propio código"):
            policy.check_write(str(PROJECT_DIR / "arthur" / "core" / "router.py"))


class CommandPolicyTest(TempHome):
    def setUp(self):
        super().setUp()
        # A propósito, "rm" y "bash" están en la lista permitida: la lista negra debe ganar.
        self.commands = CommandPolicy(("ls", "uname", "rm", "bash"), self.paths)

    def test_rm_rf_root_is_blocked(self):
        with self.assertRaisesRegex(CommandDenied, "bloqueado siempre"):
            self.commands.check(["rm", "-rf", "/"])

    def test_never_allowed_wins_over_allowlist(self):
        for argv in (["bash", "-c", "rm -rf ~"], ["sudo", "ls"], ["mkfs.ext4", "/dev/sda"], ["dd", "if=/dev/zero"]):
            with self.subTest(argv=argv), self.assertRaises(CommandDenied):
                self.commands.check(argv)

    def test_not_in_allowlist(self):
        with self.assertRaisesRegex(CommandDenied, "no está en la lista"):
            self.commands.check(["cat", "/etc/hostname"])

    def test_allowed_command_resolves_program(self):
        argv = self.commands.check(["uname", "-s"])
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertEqual(argv[1:], ["-s"])

    def test_path_arguments_go_through_path_policy(self):
        self.assertEqual(self.commands.check(["ls", str(self.projects)])[1], str(self.projects))
        for arg in (str(self.outside), str(self.home / ".ssh"), "/etc", "../Privado"):
            with self.subTest(arg=arg), self.assertRaises(CommandDenied):
                self.commands.check(["ls", arg])

    def test_shell_tricks_are_just_text(self):
        # Sin shell, ";" no separa comandos: "ls" recibiría el texto literal.
        self.assertEqual(self.commands.check(["ls", ";", "rm"])[1:], [";", "rm"])

    def test_malformed(self):
        for argv in ([], ["/usr/bin/ls"], ["ls"] * 40, ["ls", "a\0b"]):
            with self.subTest(argv=argv), self.assertRaises(CommandDenied):
                self.commands.check(argv)


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap no está instalado")
class SandboxTest(TempHome):
    def setUp(self):
        super().setUp()
        hidden = [self.projects / ".env", self.home / ".ssh"]
        self.sandbox = Sandbox([self.projects], hidden, home=self.home)

    def run_in_sandbox(self, *argv):
        return self.sandbox.run(list(argv), timeout=10)

    def test_can_read_allowed_root(self):
        out = self.run_in_sandbox("/usr/bin/cat", str(self.projects / "notas.txt"))
        self.assertEqual((out.returncode, out.stdout), (0, "hola"))

    def test_sensitive_file_inside_root_is_empty(self):
        out = self.run_in_sandbox("/usr/bin/cat", str(self.projects / ".env"))
        self.assertEqual(out.stdout, "")

    def test_rest_of_home_is_invisible(self):
        out = self.run_in_sandbox("/usr/bin/ls", "-A", str(self.home))
        self.assertEqual(out.stdout.split(), ["Proyectos"])
        out = self.run_in_sandbox("/usr/bin/cat", str(self.outside / "diario.txt"))
        self.assertNotEqual(out.returncode, 0)

    def test_everything_is_read_only(self):
        out = self.run_in_sandbox("/usr/bin/touch", str(self.projects / "nuevo"))
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse((self.projects / "nuevo").exists())

    def test_environment_is_empty(self):
        os.environ["ARTHUR_TEST_SECRET"] = "no-deberias-verme"
        try:
            out = self.run_in_sandbox("/usr/bin/env")
        finally:
            del os.environ["ARTHUR_TEST_SECRET"]
        self.assertNotIn("no-deberias-verme", out.stdout)
        self.assertEqual(sorted(line.split("=")[0] for line in out.stdout.split()), ["HOME", "LANG", "PATH", "PWD"])

    def test_run_is_empty(self):
        out = self.run_in_sandbox("/usr/bin/ls", "-A", "/run")
        self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_timeout(self):
        out = self.sandbox.run(["/usr/bin/sleep", "5"], timeout=1)
        self.assertTrue(out.timed_out)


class RedactTest(unittest.TestCase):
    def test_secrets_are_hidden(self):
        for text in ("ghp_0123456789abcdefXYZ", "sk-ant-api03-abcdefghijkl", "password=hunter2", "AKIAABCDEFGHIJKLMNOP"):
            with self.subTest(text=text):
                self.assertIn("[OCULTO]", redact(f"valor {text} fin"))

    def test_nested_values(self):
        self.assertEqual(redact({"a": ["token: xyz"], "n": 3}), {"a": ["[OCULTO]"], "n": 3})


class SecurityConfigTest(unittest.TestCase):
    def test_defaults_and_lists(self):
        cfg = config_from_dict({"security": {"read_roots": ["~/Proyectos", "/srv/datos"]}})
        self.assertEqual(cfg.security.read_roots, ("~/Proyectos", "/srv/datos"))

    def test_rejects_bad_values(self):
        for raw in (
            {"read_roots": "~/Proyectos"},
            {"read_roots": ["~/Proyectos", 3]},
            {"read_roots": ["~"]},
            {"write_roots": ["/"]},
            {"write_roots": ["Proyectos"]},
            {"command_allowlist": ["/usr/bin/ls"]},
            {"command_timeout": 0},
            {"safe_mode": "si"},
        ):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                config_from_dict({"security": raw})


if __name__ == "__main__":
    unittest.main()
