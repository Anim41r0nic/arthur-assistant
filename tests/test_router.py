import json
import stat
import tempfile
import unittest
from pathlib import Path

from arthur.core.events import EventBus
from arthur.security.audit import AuditLog
from arthur.tools.base import Param, Risk, Tool, ToolCall, ToolDenied

from .helpers import ScriptedConfirmer, make_router


class Recorder:
    """Herramienta falsa que apunta si se ha llegado a ejecutar."""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return "hecho"


def tool(risk=Risk.READ, run=None, check=None, params=()):
    return Tool("demo", "Herramienta de prueba", risk, run=run or Recorder(), params=params, check=check)


class RouterTest(unittest.TestCase):
    def test_unknown_tool(self):
        result = make_router([]).execute(ToolCall("borrar_todo", {}))
        self.assertEqual(result.status, "error")
        self.assertIn("no existe", result.output)

    def test_invalid_arguments_never_run(self):
        run = Recorder()
        t = tool(run=run, params=(Param("n", int, "número", maximum=10),))
        router = make_router([t])
        for args in ({}, {"n": "5"}, {"n": True}, {"n": 11}, {"n": 1, "extra": 2}):
            with self.subTest(args=args):
                self.assertEqual(router.execute(ToolCall("demo", args)).status, "error")
        self.assertEqual(run.calls, [])
        self.assertEqual(router.execute(ToolCall("demo", {"n": 5})).status, "ok")

    def test_check_can_deny(self):
        run = Recorder()

        def check(_):
            raise ToolDenied("no")

        result = make_router([tool(run=run, check=check)]).execute(ToolCall("demo", {}))
        self.assertEqual((result.status, result.output), ("denied", "no"))
        self.assertEqual(run.calls, [])

    def test_read_runs_without_confirmation(self):
        confirmer = ScriptedConfirmer()
        result = make_router([tool(Risk.READ)], confirmer=confirmer).execute(ToolCall("demo", {}))
        self.assertEqual(result.status, "ok")
        self.assertEqual(confirmer.requests, [])

    def test_write_needs_confirmation(self):
        run = Recorder()
        router = make_router([tool(Risk.WRITE, run=run)], confirmer=ScriptedConfirmer(False, True))
        self.assertEqual(router.execute(ToolCall("demo", {})).status, "cancelled")
        self.assertEqual(run.calls, [])
        self.assertEqual(router.execute(ToolCall("demo", {})).status, "ok")
        self.assertEqual(len(run.calls), 1)

    def test_write_without_confirmation_when_configured(self):
        confirmer = ScriptedConfirmer()
        router = make_router([tool(Risk.WRITE)], confirmer=confirmer, confirm_writes=False)
        self.assertEqual(router.execute(ToolCall("demo", {})).status, "ok")
        self.assertEqual(confirmer.requests, [])

    def test_dangerous_always_needs_confirmation(self):
        confirmer = ScriptedConfirmer(False)
        router = make_router([tool(Risk.DANGEROUS)], confirmer=confirmer, confirm_writes=False)
        self.assertEqual(router.execute(ToolCall("demo", {})).status, "cancelled")
        self.assertEqual(len(confirmer.requests), 1)

    def test_safe_mode_only_allows_reads(self):
        for risk, expected in ((Risk.READ, "ok"), (Risk.WRITE, "denied"), (Risk.DANGEROUS, "denied")):
            with self.subTest(risk=risk):
                confirmer = ScriptedConfirmer(True)
                router = make_router([tool(risk)], confirmer=confirmer, safe_mode=True)
                self.assertEqual(router.execute(ToolCall("demo", {})).status, expected)
                self.assertEqual(confirmer.requests, [])

    def test_crash_becomes_error(self):
        def boom(_):
            raise OSError("disco roto")

        with self.assertLogs("arthur.core.router", "ERROR"):
            result = make_router([tool(run=boom)]).execute(ToolCall("demo", {}))
        self.assertEqual(result.status, "error")
        self.assertIn("disco roto", result.output)

    def test_long_output_is_truncated(self):
        result = make_router([tool(run=lambda a: "x" * 50_000)]).execute(ToolCall("demo", {}))
        self.assertLess(len(result.output), 9_000)
        self.assertIn("recortado", result.output)

    def test_events(self):
        bus, got = EventBus(), []
        bus.subscribe("tool_started", got.append)
        bus.subscribe("tool_finished", got.append)
        make_router([tool()], bus=bus).execute(ToolCall("demo", {}))
        self.assertEqual([e.type for e in got], ["tool_started", "tool_finished"])

    def test_audit_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "audit.jsonl"
            t = tool(Risk.WRITE, params=(Param("text", str, "texto"),))
            router = make_router([t], audit=AuditLog(path), confirmer=ScriptedConfirmer(True))
            router.execute(ToolCall("demo", {"text": "token=ghp_abcdefghijklmnop " + "a" * 500}))
            router.execute(ToolCall("nope", {}))
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        first, second = entries
        self.assertEqual((first["tool"], first["decision"], first["status"]), ("demo", "confirmed", "ok"))
        self.assertNotIn("ghp_", first["arguments"]["text"])
        self.assertIn("[OCULTO]", first["arguments"]["text"])
        self.assertLess(len(first["arguments"]["text"]), 300)
        self.assertEqual((second["tool"], second["status"]), ("nope", "error"))


class RecordingAudit:
    """Auditoría falsa: guarda lo que se habría escrito en audit.jsonl."""

    def __init__(self):
        self.records = []

    def record(self, **fields):
        self.records.append(fields)


class UnexpectedErrorsTest(unittest.TestCase):
    """Un fallo inesperado ANTES de ejecutar (comprobación, riesgo, resumen o confirmación) no puede tumbar
    la petición ni quedar sin registrar, y la herramienta no se ejecuta: ante la duda, no se hace."""

    def run_broken(self, *, risk=Risk.WRITE, confirmer=None, **tool_parts):
        run, audit = Recorder(), RecordingAudit()
        t = Tool("demo", "Herramienta de prueba", risk, run=run, **tool_parts)
        router = make_router([t], confirmer=confirmer or ScriptedConfirmer(True), audit=audit)
        with self.assertLogs("arthur.core.router", "ERROR"):
            result = router.execute(ToolCall("demo", {}))
        return result, run, audit

    def assert_blocked(self, result, run, audit, *, decision):
        self.assertEqual(result.status, "error")
        self.assertEqual(run.calls, [])  # no se ha ejecutado
        self.assertEqual([(r["tool"], r["status"], r["decision"]) for r in audit.records],
                         [("demo", "error", decision)])  # y el intento ha quedado en la auditoría

    def test_check_that_crashes(self):
        def check(_):
            raise ValueError("Invalid IPv6 URL")  # p. ej. urlsplit("http://[::1") con un enlace raro del modelo

        result, run, audit = self.run_broken(check=check)
        self.assert_blocked(result, run, audit, decision="invalid")
        self.assertIn("Invalid IPv6 URL", result.output)

    def test_risk_for_that_crashes(self):
        def risk_for(_):
            raise KeyError("app")

        result, run, audit = self.run_broken(risk_for=risk_for)
        self.assert_blocked(result, run, audit, decision="invalid")

    def test_summary_that_crashes_means_no_confirmation_and_no_execution(self):
        def summarize(_):
            raise RuntimeError("la ventana ya no existe")

        confirmer = ScriptedConfirmer(True)
        result, run, audit = self.run_broken(summarize=summarize, confirmer=confirmer)
        self.assert_blocked(result, run, audit, decision="rejected")
        self.assertEqual(confirmer.requests, [])  # no se pregunta por algo que no se sabe describir

    def test_confirmer_that_crashes_means_no(self):
        class BrokenConfirmer:
            def confirm(self, request):
                raise OSError("el Orb se ha cerrado")

        result, run, audit = self.run_broken(confirmer=BrokenConfirmer())
        self.assert_blocked(result, run, audit, decision="rejected")
        self.assertIn("no se ha hecho nada", result.output)


class ConfirmationTextTest(unittest.TestCase):
    """Lo que ves al confirmar tiene que ser LO QUE SE VA A HACER: el resumen incluye texto que escribe el modelo
    (o un tercero, vía el modelo) y no puede llevar caracteres que cambien lo que muestra la terminal."""

    DISGUISES = {
        "\r": "retorno de carro: la línea siguiente pisaría el principio de la pregunta",
        "\x1b[2K": "secuencia de escape: borra la línea",
        "\n": "salto de línea: inventaría una línea nueva",
        "\u202e": "inversión de texto (derecha a izquierda)",
        "\u200b": "espacio de ancho cero: texto invisible",
    }

    def ask(self, summary):
        confirmer = ScriptedConfirmer(False)
        t = Tool("demo", "x", Risk.WRITE, run=Recorder(), summarize=lambda a: summary)
        make_router([t], confirmer=confirmer).execute(ToolCall("demo", {}))
        return confirmer.requests[0].summary

    def test_control_characters_are_shown_instead_of_obeyed(self):
        for char, why in self.DISGUISES.items():
            with self.subTest(why):
                shown = self.ask(f"Copiar al portapapeles: «hola{char}adiós»")
                self.assertTrue(shown.isprintable(), repr(shown))
                self.assertTrue(shown.startswith("Copiar al portapapeles: «hola"))
                self.assertTrue(shown.endswith("adiós»"))
                self.assertNotIn(char, shown)

    def test_what_the_terminal_prints_has_no_control_characters(self):
        from arthur.interfaces.cli import CliConfirmer

        printed = []
        confirmer = CliConfirmer(lambda _: "n", printed.append)
        t = Tool("demo", "x", Risk.WRITE, run=Recorder(),
                 summarize=lambda a: "Copiar: «\r  ⚠ Acción lectura: listar una carpeta\x1b[2K»")
        make_router([t], confirmer=confirmer).execute(ToolCall("demo", {}))
        self.assertTrue(all(line.isprintable() for line in printed), printed)

    def test_normal_text_is_left_alone(self):
        text = "Abrir la aplicación Café Ñandú [cafe] · «ñ» 🙂 — a/b"
        self.assertEqual(self.ask(text), text)

    def test_the_orb_event_carries_the_same_text(self):
        bus, got = EventBus(), []
        bus.subscribe("confirmation_requested", got.append)
        t = Tool("demo", "x", Risk.WRITE, run=Recorder(), summarize=lambda a: "a\rb")
        make_router([t], confirmer=ScriptedConfirmer(False), bus=bus).execute(ToolCall("demo", {}))
        self.assertEqual(got[0].data["summary"], "a\\rb")


if __name__ == "__main__":
    unittest.main()
