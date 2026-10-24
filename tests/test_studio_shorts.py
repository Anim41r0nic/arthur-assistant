"""Creator Studio: cortos verticales sacados del vídeo largo (Shorts, Reels, TikTok). ffmpeg FALSO."""

import hashlib
import json
import os
import tempfile
import tomllib
import unittest
import wave
from pathlib import Path
from unittest import mock

from arthur.studio import project, shorts
from arthur.studio.montage import MontageError
from arthur.studio.review import ReviewBook, metadata
from arthur.studio.store import Asset, StudioStore

RATE = 22050
GUION = """
titulo = "Largo"
descripcion = "La historia entera."
idioma = "es"
etiquetas = ["estoicismo"]
{corto}
[[fuentes]]
id = "f"
obra = "Obra"
autor = "Autor"
[[escenas]]
texto = "Uno."
imagen = "File:A.jpg"
fuentes = ["f"]
[[escenas]]
texto = "Dos."
imagen = "File:B.jpg"
fuentes = ["f"]
[[escenas]]
texto = "Tres."
imagen = "File:A.jpg"
fuentes = ["f"]
"""
# Escenas de 20 s cada una (con las pausas del largo entre medias).
TIMELINE = {"duracion": 61.2, "escenas": [[1, 0.0, 20.0], [2, 20.6, 40.6], [3, 41.2, 61.2]],
            "frases": [{"escena": 1, "texto": "Uno.", "empieza": 0.0, "acaba": 20.0},
                       {"escena": 2, "texto": "Dos.", "empieza": 20.6, "acaba": 40.6},
                       {"escena": 3, "texto": "Tres.", "empieza": 41.2, "acaba": 61.2}]}


class FakeFfmpeg:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps({"format": {"duration": "43.7"}, "streams": [
                {"codec_type": "video", "width": 1080, "height": 1920}, {"codec_type": "audio"}]})
        if any("print_format=json" in a for a in args):
            return '{"input_i": "-20", "input_tp": "-3", "input_lra": "5", "input_thresh": "-30", "target_offset": "0"}'
        if "ebur128=framelog=quiet" in args:
            return "I:  -14.2 LUFS"
        Path(args[-1]).write_bytes(b"salida")
        return ""


def script(corto=""):
    return project.parse_script(tomllib.loads(GUION.format(corto=corto)))


class PickTest(unittest.TestCase):
    def test_auto_takes_first_scenes_until_about_55_s(self):
        self.assertEqual(shorts.pick_scenes(script(), TIMELINE), [1, 2])

    def test_explicit_scenes(self):
        self.assertEqual(shorts.pick_scenes(script("[corto]\nescenas = [3, 1]"), TIMELINE), [3, 1])

    def test_too_long_drops_scenes_from_the_end(self):
        # El 14-10-2026 Gemini eligió escenas para 94 s y el corto no se hizo: ahora se quitan por el final.
        self.assertEqual(shorts.pick_scenes(script("[corto]\nescenas = [1, 2, 3]"), TIMELINE), [1, 2])

    def test_first_scene_alone_too_long_is_refused(self):
        long_first = {**TIMELINE, "escenas": [[1, 0.0, 60.0], [2, 60.6, 70.0], [3, 70.6, 80.0]]}
        with self.assertRaises(MontageError):
            shorts.pick_scenes(script("[corto]\nescenas = [1, 2]"), long_first)

    def test_bad_scene_numbers_in_script(self):
        with self.assertRaises(project.ScriptError):
            script("[corto]\nescenas = [9]")

    def test_lines_are_short(self):
        self.assertTrue(all(len(l) <= shorts.SUB_CHARS for l in shorts.lines("Una frase bastante larga para un corto vertical").split("\n")))


class VoiceTest(unittest.TestCase):
    def test_cuts_the_chosen_scenes_and_moves_their_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "narracion.wav"
            with wave.open(str(source), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(b"\x01\x00" * int(61.2 * RATE))
            duration, frases, spans = shorts.build_voice(source, TIMELINE, [3, 1], Path(tmp) / "corto.wav")
            self.assertAlmostEqual(duration, 2 * (20 + shorts.GAP), places=2)
            self.assertEqual([f["texto"] for f in frases], ["Tres.", "Uno."])
            self.assertEqual(frases[0]["empieza"], 0.0)
            self.assertAlmostEqual(frases[1]["empieza"], 20 + shorts.GAP, places=2)
            with wave.open(str(Path(tmp) / "corto.wav")) as w:
                self.assertAlmostEqual(w.getnframes() / RATE, duration, places=2)


class ProduceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.folder = project.project_dir("prueba")
        (self.folder / "recursos").mkdir(parents=True)
        (self.folder / "voz").mkdir()
        (self.folder / "guion.toml").write_text(GUION.format(corto='[corto]\ntitulo = "El corto"'))
        self.assets = []
        for title, name in (("File:A.jpg", "a.jpg"), ("File:B.jpg", "b.jpg")):
            data = name.encode()
            (self.folder / "recursos" / name).write_bytes(data)
            self.assets.append(Asset("prueba", name, "imagen", "commons", "https://c", title, "pd", None, None, False,
                                     hashlib.sha256(data).hexdigest(), "ok", None))
        with wave.open(str(self.folder / "voz" / "narracion.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"\x00\x00" * int(61.2 * RATE))
        (self.folder / "voz" / "linea_tiempo.json").write_text(json.dumps(TIMELINE))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_vertical_short_with_subtitles_and_end_card(self):
        fake = FakeFfmpeg()
        s = project.load_script(self.folder / "guion.toml")
        report = shorts.produce_short(s, self.assets, self.folder, run=fake)
        self.assertTrue(report["ok"], report["problemas"])
        self.assertEqual(report["escenas"], [1, 2])
        renders = [c for c in fake.calls if "-filter_complex" in c]
        self.assertTrue(all("s=1080x1920" in c[c.index("-filter_complex") + 1] for c in renders))
        final = next(c for c in fake.calls if "concat" in c)
        self.assertEqual(final[final.index("-vf") + 1].count("drawtext"), 2)  # un subtítulo por frase
        self.assertTrue(any("textfile=" in a and "final-0.txt" in a for c in fake.calls for a in c))  # rótulo final
        end = (self.folder / "salida" / "corto" / "canal.txt").read_text()
        self.assertEqual(end, "@TuCanalEstoicismo")
        card = next(c for c in fake.calls if any("canal.txt" in a for a in c))
        card_filter = card[card.index("-vf") + 1]
        self.assertIn("fontcolor=0xC29A5B", card_filter)  # el canal en bronce (kit de marca)
        self.assertIn("color=0x111113@0.6", card_filter)  # velo obsidiana

    def test_review_and_metadata_for_the_short(self):
        s = project.load_script(self.folder / "guion.toml")
        shorts.produce_short(s, self.assets, self.folder, run=FakeFfmpeg())
        meta = metadata(s, self.assets, "corto")
        self.assertEqual(meta["title"], "El corto")
        self.assertIn("Historia completa en el canal @TuCanalEstoicismo", meta["description"])
        self.assertIn("shorts", meta["tags"])
        store = StudioStore.in_memory()
        store.save_asset(self.assets[0])
        store.save_asset(self.assets[1])
        book = ReviewBook(store)
        review = book.submit("prueba", "corto")
        self.assertEqual((review.kind, review.title), ("corto", "El corto"))
        book.approve(review.id, review.video_sha256, "movil")
        _, upload = book.ready_to_publish(review.id)
        self.assertIsNone(upload["thumbnail"])  # un Short no lleva miniatura propia por la API
        self.assertTrue(upload["video"].endswith("salida/corto/video.mp4"))
        store.close()


if __name__ == "__main__":
    unittest.main()
