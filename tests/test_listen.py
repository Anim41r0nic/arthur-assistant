"""Tests de la escucha. Micrófono y Whisper FALSOS: no se graba ni se transcribe nada real."""

import io
import os
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from arthur.app import build_app
from arthur.core.config import BrainConfig, Config, ConfigError, VoiceConfig, config_from_dict
from arthur.core.events import ALL
from arthur.core.state import State
from arthur.interfaces.cli import CliConfirmer, run_chat
from arthur.security.audit import AuditLog
from arthur.voice.recorder import SAMPLE_RATE, Recorder
from arthur.voice.stt import SttError, WhisperTranscriber, clean_transcript, is_silent

from .helpers import fake_input


def tone(seconds, amplitude=12000):
    n = int(seconds * SAMPLE_RATE)
    return struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2)))


SILENCE = b"\x00\x00" * SAMPLE_RATE


def executable(directory, name, code):
    path = Path(directory) / name
    path.write_text(f"#!{sys.executable}\n{code}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class RecorderTest(unittest.TestCase):
    def fake_mic(self, seconds):
        # "Micrófono" que emite N segundos de tono y luego se queda esperando, como pw-record.
        code = (f"import sys,time,struct; n={int(seconds * SAMPLE_RATE)}; "
                "sys.stdout.buffer.write(struct.pack(f'<{n}h', *([12000, -12000] * (n // 2)))); "
                "sys.stdout.buffer.flush(); time.sleep(30)")
        return [sys.executable, "-c", code]

    def test_records_until_stop(self):
        levels = []
        recorder = Recorder(on_level=levels.append, command=self.fake_mic(1))
        recorder.start()
        self.assertTrue(recorder.recording)
        import time
        time.sleep(0.6)
        audio = recorder.stop()
        self.assertFalse(recorder.recording)
        self.assertEqual(len(audio), SAMPLE_RATE * 2)
        self.assertGreater(max(levels), 0.5)
        self.assertEqual(levels[-1], 0.0)
        self.assertEqual(recorder.stop(), b"")

    def test_max_duration(self):
        recorder = Recorder(max_seconds=1, command=self.fake_mic(2))
        recorder.start()
        import time
        time.sleep(0.8)
        audio = recorder.stop()
        self.assertTrue(recorder.truncated)
        self.assertLessEqual(len(audio), SAMPLE_RATE * 2 + 1600)


class TranscriberTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.model = self.dir / "modelo.bin"
        self.model.write_text("x")
        self.log = self.dir / "args.txt"
        # whisper-cli falso: apunta sus argumentos y si el WAV existía, y "transcribe".
        self.fake = executable(self.dir, "whisper-cli", f'''
import sys, os, wave
args = sys.argv[1:]
wav = args[args.index("--file") + 1]
with wave.open(wav) as w:
    info = (w.getframerate(), w.getnchannels(), w.getsampwidth())
open({str(self.log)!r}, "w").write(repr((args, info, sorted(os.environ))))
print(os.environ.get("FAKE_TEXT", "  Abre Firefox, por favor.  "))
''')

    def transcriber(self):
        return WhisperTranscriber(self.model, binary=str(self.fake), threads=3, prompt="Vocabulario: Firefox.")

    def test_transcribes_with_the_right_format_and_cleans_up(self):
        self.assertEqual(self.transcriber().transcribe(tone(1)), "Abre Firefox, por favor.")
        args, info, env = eval(self.log.read_text())
        self.assertEqual(info, (16000, 1, 2))
        self.assertIn("--no-timestamps", args)
        self.assertEqual(args[args.index("--language") + 1], "es")
        self.assertEqual(args[args.index("--prompt") + 1], "Vocabulario: Firefox.")
        self.assertEqual(args[args.index("--beam-size") + 1], "1")
        self.assertEqual(env, ["LANG", "PATH"])  # no hereda nuestras variables de entorno
        wav = Path(args[args.index("--file") + 1])
        self.assertFalse(wav.exists())  # el audio se borra al terminar
        self.assertFalse(wav.parent.exists())

    def test_short_context_only_when_the_sentence_fits(self):
        def args_for(seconds, audio_ctx):
            WhisperTranscriber(self.model, binary=str(self.fake), audio_ctx=audio_ctx).transcribe(tone(seconds))
            return eval(self.log.read_text())[0]

        self.assertNotIn("--audio-ctx", args_for(3, 0))  # desactivado: como siempre (el portátil)
        args = args_for(3, 768)
        self.assertEqual(args[args.index("--audio-ctx") + 1], "768")
        self.assertNotIn("--audio-ctx", args_for(14.5, 768))  # 768 ≈ 15,4 s: sin margen, modo completo

    def test_silence_is_not_transcribed(self):
        self.assertEqual(self.transcriber().transcribe(SILENCE), "")
        self.assertFalse(self.log.exists())
        self.assertTrue(is_silent(tone(0.2)))  # demasiado corto

    def test_failure(self):
        bad = executable(self.dir, "roto", "import sys; sys.stderr.write('modelo corrupto'); sys.exit(3)")
        with self.assertRaisesRegex(SttError, "modelo corrupto"):
            WhisperTranscriber(self.model, binary=str(bad)).transcribe(tone(1))

    def test_check(self):
        self.assertIn("whisper-cpp", WhisperTranscriber(self.model, binary="no-existe-xyz").check())
        self.assertIn("modelo", WhisperTranscriber(self.dir / "no.bin", binary=str(self.fake)).check())
        self.assertIsNone(self.transcriber().check())

    def test_build_prompt(self):
        from arthur.voice.stt import build_prompt
        prompt = build_prompt("ARTHUR", ["firefox", "dbeaver"])
        self.assertIn("ARTHUR", prompt)
        self.assertIn(", dbeaver.", prompt)
        self.assertEqual(prompt.lower().count("firefox"), 1)  # sin repetir palabras

    def test_clean_transcript(self):
        self.assertEqual(clean_transcript(" [Música] Hola (risas) qué tal "), "Hola qué tal")
        self.assertEqual(clean_transcript("Subtítulos realizados por la comunidad de Amara.org"), "")
        self.assertEqual(clean_transcript("Pon música"), "Pon música")


class FakeListener:
    """Micrófono y transcriptor falsos para probar la terminal."""

    def __init__(self, text="¿qué hora es?", error=None):
        self.text, self.error, self.truncated, self.started = text, error, False, 0
        self.recording = False

    def start(self):
        self.started += 1
        self.recording = True

    def stop(self):
        self.recording = False
        return b"audio"

    def check(self):
        return None

    def transcribe(self, audio):
        if self.error:
            raise self.error
        return self.text


class CliVoiceTest(unittest.TestCase):
    def run_cli(self, lines, listener):
        out = io.StringIO()
        input_fn = fake_input(lines)
        say = lambda text: print(text, file=out)  # noqa: E731
        config = Config(brain=BrainConfig(provider="rules"))
        app = build_app(config, confirmer=CliConfirmer(input_fn, say), audit=AuditLog(None))
        object.__setattr__(app, "listener", (listener, listener))
        states = []
        app.bus.subscribe("state_changed", lambda e: states.append(e.data["to"]))
        run_chat(app, input_fn=input_fn, output=out)
        return out.getvalue(), states, app

    def test_push_to_talk(self):
        out, states, _ = self.run_cli(["", ""], FakeListener("¿Qué hora es?"))
        self.assertIn("🎙 Te escucho", out)
        self.assertIn("tú (voz) › ¿Qué hora es?", out)
        self.assertIn("arthur › Son las", out)
        self.assertEqual(states[:3], [State.LISTENING, State.THINKING, State.SPEAKING])

    def test_not_understood_goes_back_to_idle(self):
        out, states, app = self.run_cli(["", ""], FakeListener(""))
        self.assertIn("No te he entendido", out)
        self.assertEqual(states, [State.LISTENING, State.IDLE])

    def test_stt_error(self):
        out, states, _ = self.run_cli(["", ""], FakeListener(error=SttError("sin modelo")))
        self.assertIn("⚠ sin modelo", out)
        self.assertEqual(states, [State.LISTENING, State.IDLE])

    def test_ctrl_d_while_recording_cancels(self):
        out, states, _ = self.run_cli([""], FakeListener())
        self.assertIn("grabación cancelada", out)
        self.assertEqual(states, [State.LISTENING, State.IDLE])

    def test_without_voice_empty_enter_does_nothing(self):
        out = io.StringIO()
        config = Config(brain=BrainConfig(provider="rules"))
        input_fn = fake_input(["", "/salir"])
        app = build_app(config, confirmer=CliConfirmer(input_fn, print), audit=AuditLog(None))
        run_chat(app, input_fn=input_fn, output=out)
        self.assertNotIn("🎙", out.getvalue())


class SttConfigTest(unittest.TestCase):
    def test_values(self):
        cfg = config_from_dict({"voice": {"stt": True, "stt_model": "ggml-base-q5_1"}})
        self.assertEqual(cfg.voice.stt_model_path.name, "ggml-base-q5_1.bin")
        for raw in ({"stt_model": "../x"}, {"language": "español"}, {"max_record_seconds": 1}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                config_from_dict({"voice": raw})


if __name__ == "__main__":
    unittest.main()
