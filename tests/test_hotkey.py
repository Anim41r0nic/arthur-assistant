"""Tests del atajo de teclado: buzón de control y alternar escucha. Sin micrófono real."""

import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arthur.api.control import ControlServer, ControlUnavailable, send_command
from arthur.brain.base import BrainReply
from arthur.core.assistant import Assistant
from arthur.core.events import EventBus
from arthur.core.state import State, StateMachine
from arthur.interfaces.cli import CliConfirmer
from arthur.security.confirm import ConfirmationRequest
from arthur.tools.base import Risk
from arthur.voice.controller import VoiceController

from .helpers import make_router
from .test_listen import FakeListener


class ControlServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "arthur" / "control.sock"
        self.calls = []
        self.server = ControlServer({"toggle-listen": lambda _: self.calls.append(1) or "escuchando",
                                     "ask": lambda text: f"eco: {text}"}, self.path)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_command_and_permissions(self):
        self.assertEqual(send_command("toggle-listen", self.path), "escuchando")
        self.assertEqual(self.calls, [1])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_ask_with_text(self):
        self.assertEqual(send_command("ask ¿qué hora es?", self.path), "eco: ¿qué hora es?")
        self.assertIn("demasiado larga", send_command("ask " + "x" * 9000, self.path))

    def test_only_known_commands(self):
        for command in ("rm -rf ~", "abre firefox", "toggle-listen;ls", "x" * 500):
            with self.subTest(command=command[:20]):
                self.assertIn("error", send_command(command, self.path))
        self.assertEqual(self.calls, [])

    def test_second_server_is_refused_and_stale_socket_is_replaced(self):
        with self.assertRaisesRegex(ControlUnavailable, "otro ARTHUR"):
            ControlServer({}, self.path)
        self.server.stop()
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(self.path))  # socket abandonado, nadie escucha
        stale.close()
        replacement = ControlServer({"toggle-listen": lambda _: "ok"}, self.path)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.assertEqual(send_command("toggle-listen", self.path), "ok")

    def test_client_when_arthur_is_not_running(self):
        with self.assertRaisesRegex(ControlUnavailable, "no está en marcha"):
            send_command("toggle-listen", self.path.parent / "nada.sock")


class SlowBrain:
    name = label = "lento"

    def __init__(self):
        self.release = threading.Event()

    def reply(self, conversation):
        self.release.wait(5)
        return BrainReply(f"eco: {conversation[-1].content}")


class FakeSpeaker:
    def __init__(self):
        self.stopped = threading.Event()

    def speak(self, text):
        self.stopped.wait(5)  # "habla" hasta que le cortan

    def stop(self):
        self.stopped.set()


class ToggleTest(unittest.TestCase):
    def make(self, speaker=None):
        bus = EventBus()
        self.brain = SlowBrain()
        assistant = Assistant(self.brain, make_router([]), bus, StateMachine(bus), 20, speaker=speaker)
        self.said, self.notes, self.done = [], [], threading.Event()
        listener = FakeListener("hola")
        voice = VoiceController(assistant, listener, listener, say=self.said.append,
                                notify=self.notes.append, on_done=self.done.set)
        return assistant, voice

    def test_toggle_start_then_finish_in_background(self):
        assistant, voice = self.make()
        self.assertEqual(voice.toggle(), "escuchando")
        self.assertEqual(assistant.state, State.LISTENING)
        self.assertIn("Te escucho", self.notes[0])
        self.assertEqual(voice.toggle(), "procesando")  # vuelve al momento: no bloquea el atajo
        self.brain.release.set()
        self.assertTrue(self.done.wait(5))
        self.assertIn("tú (voz) › hola", self.said)
        self.assertEqual(assistant.state, State.IDLE)

    def test_busy_while_thinking(self):
        assistant, voice = self.make()
        worker = threading.Thread(target=assistant.handle_text, args=("algo",))
        worker.start()
        time.sleep(0.1)
        self.assertEqual(voice.toggle(), "ocupado")
        self.brain.release.set()
        worker.join(5)

    def test_hotkey_interrupts_speech(self):
        speaker = FakeSpeaker()
        assistant, voice = self.make(speaker)
        self.brain.release.set()
        worker = threading.Thread(target=assistant.handle_text, args=("algo",))
        worker.start()
        deadline = time.monotonic() + 3
        while assistant.state is not State.SPEAKING and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(voice.toggle(), "escuchando")
        self.assertTrue(speaker.stopped.is_set())
        self.assertEqual(assistant.state, State.LISTENING)
        worker.join(5)
        voice.cancel()


class ConfirmFromOtherThreadTest(unittest.TestCase):
    def test_requests_from_the_hotkey_are_not_confirmed(self):
        said, asked = [], []
        confirmer = CliConfirmer(lambda prompt: asked.append(prompt) or "s", said.append)
        request = ConfirmationRequest("write_file", Risk.WRITE, "Crear x")
        result = []
        thread = threading.Thread(target=lambda: result.append(confirmer.confirm(request)))
        thread.start()
        thread.join()
        self.assertEqual(result, [False])
        self.assertEqual(asked, [])  # ni siquiera pregunta
        self.assertIn("pídemelo desde la terminal", said[0])
        self.assertTrue(confirmer.confirm(request))  # desde el hilo principal sí pregunta


if __name__ == "__main__":
    unittest.main()
