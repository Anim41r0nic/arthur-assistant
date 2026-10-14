"""Creator Studio, paso 3b: voz frase a frase y subtítulos sincronizados. Piper FALSO (escribe silencio)."""

import json
import tempfile
import tomllib
import unittest
import wave
from pathlib import Path

from arthur.studio import project, voice

RATE = 22050


class FakeEngine:
    """Escribe silencio: 0,05 s por carácter, duración predecible."""

    def __init__(self, calls, id="falsa", ok=True):
        self.calls, self.id, self.ok, self.closed = calls, id, ok, False

    def check(self):
        if not self.ok:
            raise voice.VoiceError("falta la voz")

    def speak(self, text, output):
        self.calls.append(text)
        with wave.open(str(output), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"\x00\x00" * int(len(text) * 0.05 * RATE))

    def close(self):
        self.closed = True


FAKE_WORKER = """
import json, sys, wave
print(json.dumps({"ok": True, "ready": True}), flush=True)
for line in sys.stdin:
    job = json.loads(line)
    if job["text"] == "falla":
        print(json.dumps({"ok": False, "error": "voz rota"}), flush=True)
        continue
    with wave.open(job["out"], "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\\x00\\x00" * 2400)
    print(json.dumps({"ok": True, "seconds": 0.1, "voice": job["voice"], "lang": job["lang"]}), flush=True)
"""


GUION = """
titulo = "Prueba"
descripcion = "Prueba."
[[fuentes]]
id = "f"
obra = "Obra"
autor = "Autor"
[[escenas]]
texto = "Primera frase corta. Segunda frase, que también es corta."
imagen = "File:A.jpg"
fuentes = ["f"]
[[escenas]]
texto = "Una escena con una frase bastante larga que hay que partir porque no cabe en dos líneas de subtítulos, de ninguna manera, aunque lo intentemos."
imagen = "File:B.jpg"
fuentes = ["f"]
"""


class SplitTest(unittest.TestCase):
    def test_pieces_fit_two_subtitle_lines(self):
        text = tomllib.loads(GUION)["escenas"][1]["texto"]
        pieces = voice.split_text(text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= voice.MAX_CHARS for p in pieces))
        self.assertEqual(" ".join(pieces), text)  # no se pierde ni se inventa nada

    def test_sentences_are_kept_whole_when_short(self):
        self.assertEqual(voice.split_text("Hola. ¿Qué tal? Bien."), ["Hola.", "¿Qué tal?", "Bien."])

    def test_wrap_balances_lines(self):
        lines = voice.wrap("Una escena con una frase bastante larga que hay que partir").split("\n")
        self.assertEqual(len(lines), 2)
        self.assertLess(abs(len(lines[0]) - len(lines[1])), 12)
        self.assertEqual(voice.wrap("Corta"), "Corta")


class NarrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.script = project.parse_script(tomllib.loads(GUION))

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeline_and_srt_are_in_sync(self):
        calls = []
        narration = voice.narrate(self.script, self.folder, FakeEngine(calls))
        first, second = narration.chunks[0], narration.chunks[1]
        self.assertEqual(first.start, 0.0)
        self.assertAlmostEqual(first.end, len(first.text) * 0.05, places=2)
        self.assertAlmostEqual(second.start - first.end, voice.CHUNK_GAP, places=2)
        scene2 = next(c for c in narration.chunks if c.scene == 2)
        last1 = [c for c in narration.chunks if c.scene == 1][-1]
        self.assertAlmostEqual(scene2.start - last1.end, voice.SCENE_GAP, places=2)
        with wave.open(str(narration.path)) as w:
            self.assertAlmostEqual(w.getnframes() / RATE, narration.duration, places=2)
        timeline = json.loads((self.folder / "voz" / "linea_tiempo.json").read_text())
        self.assertEqual([s[0] for s in timeline["escenas"]], [1, 2])
        srt = voice.write_srt(narration, self.folder / "salida" / "subtitulos.srt").read_text()
        self.assertTrue(srt.startswith("1\n00:00:00,000 --> 00:00:01,"))
        self.assertIn("Segunda frase, que también es corta.", srt)

    def test_only_changed_text_is_regenerated(self):
        calls = []
        voice.narrate(self.script, self.folder, FakeEngine(calls))
        first_run = len(calls)
        voice.narrate(self.script, self.folder, FakeEngine(calls))
        self.assertEqual(len(calls), first_run)  # nada nuevo
        changed = project.parse_script(tomllib.loads(GUION.replace("Primera frase corta.", "Otra primera.")))
        voice.narrate(changed, self.folder, FakeEngine(calls))
        self.assertEqual(calls[first_run:], ["Otra primera."])

    def test_missing_voice_model(self):
        with self.assertRaises(voice.VoiceError):
            voice.narrate(self.script, self.folder, FakeEngine([], ok=False))

    def test_changing_the_voice_regenerates_everything(self):
        calls = []
        voice.narrate(self.script, self.folder, FakeEngine(calls, id="piper:x"))
        first = len(calls)
        voice.narrate(self.script, self.folder, FakeEngine(calls, id="kokoro:ef_dora"))
        self.assertEqual(len(calls), 2 * first)

    def test_engine_is_closed_even_on_error(self):
        class Broken(FakeEngine):
            def speak(self, text, output):
                raise voice.VoiceError("rota")
        engine = Broken([])
        with self.assertRaises(voice.VoiceError):
            voice.narrate(self.script, self.folder, engine)
        self.assertTrue(engine.closed)


class EngineChoiceTest(unittest.TestCase):
    def test_defaults_are_marcs_choices(self):
        self.assertEqual(voice.engine_for("", "es").id.split("|")[0], "kokoro:ef_dora")
        self.assertEqual(voice.engine_for("", "en").id.split("|")[0], "kokoro:af_heart")
        self.assertIsInstance(voice.engine_for("piper:es_ES-davefx-medium", "es"), voice.PiperEngine)
        with self.assertRaises(voice.VoiceError):
            voice.engine_for("", "fr")

    def test_kokoro_model_must_match_its_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "venv" / "bin").mkdir(parents=True)
            (base / "venv" / "bin" / "python").write_text("")
            for name in voice.KOKORO_FILES:
                (base / name).write_bytes(b"modelo cambiado")
            engine = voice.KokoroEngine("ef_dora", "es", base=base)
            engine.verify = True
            with self.assertRaises(voice.VoiceError) as caught:
                engine.check()
            self.assertIn("huella", str(caught.exception))

    def test_kokoro_needs_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(voice.VoiceError) as caught:
                voice.KokoroEngine("ef_dora", "es", base=Path(tmp)).check()
            self.assertIn("docs/studio.md", str(caught.exception))


class KokoroProcessTest(unittest.TestCase):
    """Un proceso falso con el mismo protocolo que kokoro_worker.py (sin cargar el modelo de verdad)."""

    def test_one_process_for_all_sentences(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "venv" / "bin").mkdir(parents=True)
            (base / "venv" / "bin" / "python").symlink_to(sys.executable)
            for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
                (base / name).write_bytes(b"x")
            worker = base / "worker.py"
            worker.write_text(FAKE_WORKER)
            engine = voice.KokoroEngine("ef_dora", "es", base=base)
            engine.worker = worker
            engine.check()
            engine.speak("Hola.", base / "a.wav")
            first = engine._proc
            engine.speak("Adiós.", base / "b.wav")
            self.assertIs(engine._proc, first)  # el modelo se carga una vez
            with wave.open(str(base / "b.wav")) as w:
                self.assertEqual(w.getframerate(), 24000)
            with self.assertRaises(voice.VoiceError):
                engine.speak("falla", base / "c.wav")
            engine.close()
            self.assertIsNone(engine._proc)

    def test_stamp(self):
        self.assertEqual(voice._stamp(3723.4567), "01:02:03,457")


if __name__ == "__main__":
    unittest.main()
