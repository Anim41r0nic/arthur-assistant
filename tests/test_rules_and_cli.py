import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from arthur.app import build_app
from arthur.brain.base import Message
from arthur.brain.rules import RuleBrain, normalize
from arthur.core.config import BrainConfig, Config, SecurityConfig
from arthur.interfaces.cli import CliConfirmer, run_chat
from arthur.security.audit import AuditLog

from .helpers import fake_input

FIXED_NOW = datetime(2026, 10, 4, 9, 5)  # domingo


class RuleBrainTest(unittest.TestCase):
    def setUp(self):
        self.brain = RuleBrain("ARTHUR", now=lambda: FIXED_NOW)

    def ask(self, text):
        return self.brain.reply([Message("user", text)])

    def test_normalize(self):
        self.assertEqual(normalize("¿Qué HORA es?"), "¿que hora es?")

    def test_text_rules(self):
        self.assertEqual(self.ask("¿Qué hora es?").text, "Son las 09:05.")
        self.assertEqual(self.ask("qué día es hoy").text, "Hoy es domingo, 4 de octubre de 2026.")
        self.assertIn("Soy ARTHUR", self.ask("¿Quién eres?").text)
        self.assertIn("De momento entiendo", self.ask("¿qué puedes hacer?").text)
        self.assertEqual(self.ask("Buenos días").text, "Hola. ¿En qué puedo ayudarte?")
        self.assertEqual(self.ask("muchas gracias").text, "De nada.")

    def test_unknown(self):
        self.assertIn("Todavía no sé", self.ask("pinta la pared de azul").text)
        self.assertIn("Todavía no sé", self.ask("ahora no").text)  # "ahora" no es "hora"

    def tool_call(self, text):
        calls = self.ask(text).tool_calls
        self.assertEqual(len(calls), 1, text)
        return calls[0].name, dict(calls[0].arguments)

    def test_tool_rules(self):
        self.assertEqual(self.tool_call("Información del sistema"), ("get_system_info", {}))
        self.assertEqual(self.tool_call("abre Firefox"), ("open_application", {"name": "Firefox"}))
        self.assertEqual(self.tool_call("lanza la aplicación Code::Blocks"), ("open_application", {"name": "Code::Blocks"}))
        self.assertEqual(self.tool_call("lista ~/Proyectos"), ("list_directory", {"path": "~/Proyectos"}))
        self.assertEqual(self.tool_call("lee ~/Claves recuperacion/x.txt "),
                         ("read_file", {"path": "~/Claves recuperacion/x.txt"}))
        self.assertEqual(self.tool_call("lee el archivo ~/Proyectos/Notas.txt"),
                         ("read_file", {"path": "~/Proyectos/Notas.txt"}))
        self.assertEqual(self.tool_call("crea el archivo ~/Proyectos/a.txt con Hola Mundo"),
                         ("write_file", {"path": "~/Proyectos/a.txt", "content": "Hola Mundo"}))
        self.assertEqual(self.tool_call("sobrescribe el archivo ~/Proyectos/a.txt con adiós"),
                         ("write_file", {"path": "~/Proyectos/a.txt", "content": "adiós", "overwrite": True}))
        self.assertEqual(self.tool_call("ejecuta rm -rf /"), ("run_command", {"command": ["rm", "-rf", "/"]}))
        self.assertEqual(self.tool_call("ejecuta ls 'Mis cosas'"), ("run_command", {"command": ["ls", "Mis cosas"]}))

    def test_bad_quotes(self):
        self.assertIn("No entiendo ese comando", self.ask("ejecuta ls 'sin cerrar").text)

    def test_reports_tool_results(self):
        conversation = [Message("user", "x"), Message("assistant", "y"), Message("tool", "uno"), Message("tool", "dos")]
        self.assertEqual(self.brain.reply(conversation).text, "uno\n\ndos")


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, lines, **kwargs):
        out = io.StringIO()
        input_fn = fake_input(lines)
        say = lambda text: print(text, file=out)  # noqa: E731
        config = Config(
            brain=BrainConfig(provider="rules"),
            security=SecurityConfig(read_roots=(str(self.root),), write_roots=(str(self.root),)),
        )
        app = build_app(config, confirmer=CliConfirmer(input_fn, say), audit=AuditLog(None))
        code = run_chat(app, input_fn=input_fn, output=out, **kwargs)
        return code, out.getvalue()

    def test_conversation_and_commands(self):
        code, out = self.run_cli(["hola", "/estado", "/herramientas", "/foo", "/ayuda", "/salir", "no-llega"])
        self.assertEqual(code, 0)
        self.assertIn("arthur › Hola.", out)
        self.assertIn("Estado: IDLE", out)
        self.assertIn("run_command", out)
        self.assertIn("[PELIGROSA]", out)
        self.assertIn("Comando desconocido: /foo", out)
        self.assertNotIn("no-llega", out)
        self.assertTrue(out.rstrip().endswith("Hasta luego."))

    def test_eof_exits_cleanly(self):
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("Hasta luego.", out)

    def test_show_states(self):
        _, out = self.run_cli(["hola"], show_states=True)
        self.assertIn("IDLE → THINKING", out)
        self.assertIn("SPEAKING → IDLE", out)

    def test_write_with_confirmation(self):
        target = self.root / "a.txt"
        _, out = self.run_cli([f"crea el archivo {target} con hola", "s"])
        self.assertIn("⚠ Acción escritura", out)
        self.assertIn("Escritos 4 caracteres", out)
        self.assertEqual(target.read_text(), "hola")

    def test_anything_but_yes_is_no(self):
        target = self.root / "a.txt"
        _, out = self.run_cli([f"crea el archivo {target} con hola", "vale"])
        self.assertIn("[CANCELADO]", out)
        self.assertFalse(target.exists())

    def test_rm_rf_is_blocked(self):
        _, out = self.run_cli(["ejecuta rm -rf /"])
        self.assertIn("[DENEGADO] 'rm' está bloqueado siempre", out)
        self.assertNotIn("¿Lo permites?", out)


if __name__ == "__main__":
    unittest.main()
