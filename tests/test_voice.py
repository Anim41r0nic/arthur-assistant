"""Tests de la voz. Usan un proceso de voz y un reproductor FALSOS: no suena nada."""

import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arthur.brain.base import BrainReply
from arthur.core.assistant import Assistant
from arthur.core.config import ConfigError, config_from_dict
from arthur.core.events import ALL, EventBus
from arthur.core.state import State, StateMachine
from arthur.voice.tts import PiperSpeaker, TtsError, rms_level, speakable

from .helpers import make_router

# Proceso de voz falso: responde con el mismo protocolo que piper_worker.py.
# Cada petición devuelve 0,3 s de silencio y 0,3 s de un tono fuerte (a 1.000 muestras/s).
FAKE_WORKER = r'''
import json, struct, sys
def send(kind, payload=b""):
    sys.stdout.buffer.write(kind + struct.pack(">I", len(payload)) + payload); sys.stdout.buffer.flush()
send(b"R", json.dumps({"sample_rate": 1000}).encode())
for line in sys.stdin:
    text = json.loads(line)["text"]
    if "romper" in text:
        send(b"X", b"frase rota"); continue
    if "morir" in text:
        sys.exit(1)
    send(b"A", b"\x00\x00" * 300)
    send(b"A", struct.pack("<300h", *([20000, -20000] * 150)))
    send(b"E")
'''
SILENT_PLAYER = lambda rate, volume: [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]  # noqa: E731


def fake_speaker(**kwargs):
    levels = []
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    tmp.write(FAKE_WORKER)
    tmp.close()
    speaker = PiperSpeaker(Path("/no/importa.onnx"), on_level=levels.append,
                           worker_cmd=[sys.executable, tmp.name], player_cmd=SILENT_PLAYER, **kwargs)
    return speaker, levels


class SpeakableTest(unittest.TestCase):
    def test_cleans_text(self):
        self.assertEqual(speakable("¡Hola! 😊 Soy **ARTHUR**."), "¡Hola! Soy ARTHUR.")
        self.assertEqual(speakable("Mira https://es.wikipedia.org/wiki/Linux ya"), "Mira el enlace ya")
        self.assertIn("te dejo el código en pantalla", speakable("Así:\n```\nls -la\n```"))
        self.assertEqual(speakable("Cuesta 5 € (un 20 %)"), "Cuesta 5 € (un 20 %)")
        self.assertEqual(speakable("😊🎉"), "")

    def test_long_text_is_cut_at_a_sentence(self):
        text = speakable("Frase número uno. " * 200)
        self.assertLess(len(text), 1300)
        self.assertTrue(text.endswith("El resto te lo dejo en pantalla."))

    def test_rms_level(self):
        self.assertEqual(rms_level(b""), 0.0)
        self.assertEqual(rms_level(b"\x00\x00" * 100), 0.0)
        self.assertGreater(rms_level(struct.pack("<100h", *([20000, -20000] * 50))), 0.9)


class PiperSpeakerTest(unittest.TestCase):
    def test_speaks_with_real_time_pacing_and_levels(self):
        speaker, levels = fake_speaker()
        self.addCleanup(speaker.close)
        start = time.monotonic()
        speaker.speak("Hola")
        elapsed = time.monotonic() - start
        self.assertGreater(elapsed, 0.4)  # 0,6 s de audio a ritmo real (menos el adelanto)
        self.assertEqual(levels[-1], 0.0)  # al acabar, el Orb vuelve a calma
        self.assertTrue(any(level == 0 for level in levels[:-1]))
        self.assertTrue(any(level > 0.9 for level in levels))
        speaker.speak("Otra frase")  # el proceso de voz se reutiliza

    def test_empty_text_is_not_sent(self):
        speaker, levels = fake_speaker()
        self.addCleanup(speaker.close)
        speaker.speak("😊")
        self.assertEqual(levels, [])
        self.assertIsNone(speaker._worker)

    def test_stop_cuts_the_voice(self):
        speaker, levels = fake_speaker()
        self.addCleanup(speaker.close)
        threading.Timer(0.1, speaker.stop).start()
        start = time.monotonic()
        speaker.speak("Hola")
        self.assertLess(time.monotonic() - start, 0.4)

    def test_worker_errors(self):
        speaker, _ = fake_speaker()
        self.addCleanup(speaker.close)
        with self.assertRaisesRegex(TtsError, "frase rota"):
            speaker.speak("romper")
        with self.assertRaisesRegex(TtsError, "cerrado inesperadamente"):
            speaker.speak("morir")
        speaker.speak("Hola")  # se vuelve a arrancar solo

    def test_check(self):
        speaker = PiperSpeaker(Path("/no/existe.onnx"), python=Path("/no/existe/python"))
        self.assertIn("entorno de voz", speaker.check())
        speaker = PiperSpeaker(Path("/no/existe.onnx"), python=Path(sys.executable))
        self.assertIn("No encuentro la voz", speaker.check())


class RecordingSpeaker:
    def __init__(self, fail=None):
        self.spoken, self.stopped, self.fail = [], False, fail

    def speak(self, text):
        self.spoken.append(text)
        if self.fail:
            raise self.fail

    def stop(self):
        self.stopped = True


class EchoBrain:
    name = label = "echo"

    def reply(self, conversation):
        return BrainReply(f"eco: {conversation[-1].content}")


class AssistantVoiceTest(unittest.TestCase):
    def make(self, speaker):
        bus = EventBus()
        events = []
        bus.subscribe(ALL, events.append)
        return Assistant(EchoBrain(), make_router([]), bus, StateMachine(bus), 20, speaker=speaker), events

    def test_speaks_after_showing_the_text(self):
        speaker = RecordingSpeaker()
        assistant, events = self.make(speaker)
        self.assertEqual(assistant.handle_text("hola"), "eco: hola")
        self.assertEqual(speaker.spoken, ["eco: hola"])
        types = [e.type for e in events]
        self.assertLess(types.index("assistant_message"), len(types) - 1)
        self.assertEqual(assistant.state, State.IDLE)

    def test_voice_failure_does_not_break_the_reply(self):
        assistant, _ = self.make(RecordingSpeaker(fail=OSError("sin audio")))
        with self.assertLogs("arthur.core.assistant", "ERROR"):
            self.assertEqual(assistant.handle_text("hola"), "eco: hola")
        self.assertEqual(assistant.state, State.IDLE)

    def test_ctrl_c_stops_the_voice(self):
        speaker = RecordingSpeaker(fail=KeyboardInterrupt())
        assistant, _ = self.make(speaker)
        self.assertEqual(assistant.handle_text("hola"), "eco: hola")
        self.assertTrue(speaker.stopped)
        self.assertEqual(assistant.state, State.IDLE)


class VoiceConfigTest(unittest.TestCase):
    def test_values(self):
        cfg = config_from_dict({"voice": {"tts": True, "speed": 1, "volume": 0.5}})
        self.assertEqual((cfg.voice.speed, cfg.voice.volume), (1.0, 0.5))
        self.assertEqual(cfg.voice.model_path.name, "es_ES-davefx-medium.onnx")
        for raw in ({"voice": "../../etc/x"}, {"speed": 3.0}, {"volume": 0}, {"voices_dir": "voces"}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                config_from_dict({"voice": raw})


if __name__ == "__main__":
    unittest.main()
