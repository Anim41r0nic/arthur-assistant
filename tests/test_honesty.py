"""Que ARTHUR no diga que ha hecho algo que no ha hecho (fase S5, paso 4). Cerebro con guion: nada de Ollama."""

import unittest

from arthur.brain.base import BrainReply
from arthur.core.assistant import Assistant
from arthur.core.events import EventBus
from arthur.core.honesty import HONEST_REPLY, NUDGE, claims_action
from arthur.core.state import StateMachine
from arthur.tools.base import Param, Risk, Tool, ToolCall, ToolError

from .helpers import make_router

opened = []
LIST_APPS = Tool("list_applications", "Lista las apps.", Risk.READ, run=lambda a: "codeblocks, firefox")
NAME = (Param("name", str, "La app"),)
OPEN_APP = Tool("open_application", "Abre una app.", Risk.LOW, run=lambda a: opened.append(a) or "abierta",
                params=NAME)


def broken(_):
    raise ToolError("no está instalada")


BROKEN_OPEN = Tool("open_application", "Abre una app.", Risk.LOW, run=broken, params=NAME)


class ScriptBrain:
    """Contesta lo que le digas, en orden, y apunta lo que ve en cada vuelta."""
    name = "guion"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def reply(self, conversation):
        self.seen.append(list(conversation))
        return self.replies.pop(0)


def assistant_with(brain, *tools):
    bus = EventBus()
    return Assistant(brain, make_router(list(tools), bus=bus), bus, StateMachine(bus), 20)


def call(tool, **args):
    return BrainReply("", (ToolCall(tool, args),))


class ClaimsTest(unittest.TestCase):
    def test_claims(self):
        for text in ("Abro Code::Blocks.", "Vale, lo abro ahora.", "¡Hecho!", "Listo, he puesto el volumen al 50 %.",
                     "Te lo he apuntado.", "Recordatorio creado para mañana a las 9.", "Abriendo Code::Blocks…"):
            with self.subTest(text=text):
                self.assertTrue(claims_action(text))

    def test_not_claims(self):
        for text in ("Son las 13:47.", "No lo he abierto porque no está instalado.", "Creo que está en tu escritorio.",
                     "El cambio de hora es el domingo.", "Aún no te lo he apuntado: ¿a qué hora?",
                     "¿Quieres que lo abra?", "Nunca lo he instalado.", HONEST_REPLY):
            with self.subTest(text=text):
                self.assertFalse(claims_action(text))


class HonestTurnTest(unittest.TestCase):
    def setUp(self):
        opened.clear()

    def test_the_code_blocks_case_gets_fixed(self):
        # Lo que pasó de verdad: mira la lista, dice «lo abro» y no abre nada. Tras el aviso, la abre.
        brain = ScriptBrain(call("list_applications"), BrainReply("Abro Code::Blocks."),
                            call("open_application", name="codeblocks"), BrainReply("Abro Code::Blocks."))
        assistant = assistant_with(brain, LIST_APPS, OPEN_APP)
        self.assertEqual(assistant.handle_text("abre Code::Blocks", speak=False), "Abro Code::Blocks.")
        self.assertEqual(opened, [{"name": "codeblocks"}])
        self.assertEqual(brain.seen[2][-1].content, NUDGE)  # el aviso le llegó al modelo
        # …pero no se queda en la conversación que se recuerda.
        brain.replies.append(BrainReply("De nada."))
        assistant.handle_text("gracias", speak=False)
        remembered = [m.content for m in brain.seen[-1]]
        self.assertNotIn(NUDGE, remembered)
        self.assertEqual(remembered.count("Abro Code::Blocks."), 1)  # solo la respuesta final, no la falsa

    def test_if_it_insists_the_answer_is_honest(self):
        brain = ScriptBrain(BrainReply("¡Hecho! He abierto Code::Blocks."), BrainReply("Listo, abierto."))
        assistant = assistant_with(brain, OPEN_APP)
        self.assertEqual(assistant.handle_text("abre Code::Blocks", speak=False), HONEST_REPLY)
        self.assertEqual(opened, [])

    def test_a_failed_action_does_not_count_as_done(self):
        brain = ScriptBrain(call("open_application", name="codeblocks"), BrainReply("He abierto Code::Blocks."),
                            BrainReply("No he podido abrirlo: no está instalado."))
        assistant = assistant_with(brain, BROKEN_OPEN)
        self.assertEqual(assistant.handle_text("abre Code::Blocks", speak=False),
                         "No he podido abrirlo: no está instalado.")

    def test_real_actions_and_plain_answers_pass_without_warning(self):
        brain = ScriptBrain(call("open_application", name="firefox"), BrainReply("He abierto Firefox."))
        self.assertEqual(assistant_with(brain, OPEN_APP).handle_text("abre firefox", speak=False),
                         "He abierto Firefox.")
        self.assertEqual(len(brain.seen), 2)  # sin vuelta de más
        brain = ScriptBrain(BrainReply("Son las 13:47."))
        self.assertEqual(assistant_with(brain).handle_text("¿qué hora es?", speak=False), "Son las 13:47.")
        self.assertEqual(len(brain.seen), 1)


class HistoryTest(unittest.TestCase):
    def test_history_shows_what_was_said_without_tool_rounds(self):
        brain = ScriptBrain(call("open_application", name="firefox"), BrainReply("He abierto Firefox."),
                            BrainReply("Veo un gato."), BrainReply("Otra foto: un perro."))
        assistant = assistant_with(brain, OPEN_APP)
        assistant.handle_text("abre firefox", speak=False)
        assistant.handle_text("¿qué hay?", speak=False, images=("FOTO1",))
        assistant.handle_text("¿y aquí?", speak=False, images=("FOTO2",))  # la primera foto ya no se reenvía
        self.assertEqual(assistant.history(), [
            {"role": "user", "text": "abre firefox", "photo": False},
            {"role": "assistant", "text": "He abierto Firefox."},
            {"role": "user", "text": "¿qué hay?", "photo": True},
            {"role": "assistant", "text": "Veo un gato."},
            {"role": "user", "text": "¿y aquí?", "photo": True},
            {"role": "assistant", "text": "Otra foto: un perro."},
        ])


if __name__ == "__main__":
    unittest.main()
