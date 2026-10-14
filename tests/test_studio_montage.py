"""Creator Studio, paso 3c: montaje, miniatura y control de calidad. ffmpeg FALSO: nada se codifica de verdad."""

import hashlib
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from arthur.studio import montage, project
from arthur.studio.store import Asset

GUION = """
titulo = "Prueba de montaje"
descripcion = "Prueba."
miniatura_texto = "LO TRAICIONÓ"
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


class FakeFfmpeg:
    def __init__(self, duration=12.5, lufs=-14.2, width=1920):
        self.calls = []
        self.duration, self.lufs, self.width = duration, lufs, width

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps({"format": {"duration": str(self.duration)}, "streams": [
                {"codec_type": "video", "width": self.width, "height": 1080}, {"codec_type": "audio"}]})
        if any("print_format=json" in a for a in args):
            return ('[Parsed_loudnorm_0] \n{\n "input_i" : "-19.80", "input_tp" : "-3.10", "input_lra" : "6.20", '
                    '"input_thresh" : "-30.10", "output_i" : "-14.0", "target_offset" : "0.30"\n}\n')
        if "ebur128=framelog=quiet" in args:
            return f"Summary:\n  Integrated loudness:\n    I:         {self.lufs} LUFS\n"
        Path(args[-1]).write_bytes(b"salida falsa")
        return ""


class MontageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        (self.folder / "recursos").mkdir()
        self.assets = []
        for title, name in (("File:A.jpg", "a.jpg"), ("File:B.jpg", "b.jpg")):
            data = f"imagen {name}".encode()
            (self.folder / "recursos" / name).write_bytes(data)
            self.assets.append(Asset("p", name, "imagen", "commons", f"https://commons/{name}", title, "pd", None,
                                     "Autor", False, hashlib.sha256(data).hexdigest(), "ok", "Public domain"))
        (self.folder / "voz").mkdir()
        (self.folder / "voz" / "narracion.wav").write_bytes(b"wav")
        (self.folder / "voz" / "linea_tiempo.json").write_text(json.dumps(
            {"duracion": 11.0, "escenas": [[1, 0.0, 3.0], [2, 3.6, 7.0], [3, 7.6, 11.0]]}))
        self.script = project.parse_script(tomllib.loads(GUION))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_covers_gaps_and_tail(self):
        images = montage.check_assets(self.script, self.assets, self.folder)
        timeline = json.loads((self.folder / "voz" / "linea_tiempo.json").read_text())
        plans = montage.plan(self.script, timeline, images)
        self.assertEqual([(p.start, p.duration) for p in plans], [(0.0, 3.6), (3.6, 4.0), (7.6, 4.9)])
        self.assertEqual(plans[2].image.name, "a.jpg")

    def test_full_production(self):
        fake = FakeFfmpeg()
        report = montage.produce(self.script, self.assets, self.folder, run=fake)
        self.assertTrue(report["ok"], report["problemas"])
        out = self.folder / "salida"
        for name in ("video.mp4", "miniatura.jpg", "descripcion.txt", "control.json"):
            self.assertTrue((out / name).exists(), name)
        renders = [c for c in fake.calls if "-loop" in c]
        self.assertEqual(len(renders), 3)
        self.assertTrue(all(c[:3] == ["nice", "-n", "19"] or c[0] == "ffmpeg" for c in renders))
        self.assertIn("-threads", renders[0])
        thumb = next(c for c in fake.calls if any("drawtext" in a for a in c))
        self.assertTrue(any("textfile=" in a for a in thumb))  # el texto nunca va dentro del filtro
        self.assertIn("Autor", (out / "descripcion.txt").read_text())
        # Repetir no vuelve a hacer las escenas.
        fake.calls.clear()
        montage.produce(self.script, self.assets, self.folder, run=fake)
        self.assertEqual([c for c in fake.calls if "-loop" in c], [])

    def test_blocked_or_changed_image_stops_everything(self):
        blocked = [self.assets[0], Asset(**{**self.assets[1].__dict__, "status": "blocked", "reason": "CC BY-SA"})]
        with self.assertRaises(montage.MontageError) as caught:
            montage.produce(self.script, blocked, self.folder, run=FakeFfmpeg())
        self.assertIn("CC BY-SA", str(caught.exception))
        (self.folder / "recursos" / "a.jpg").write_bytes(b"otra imagen")
        with self.assertRaises(montage.MontageError):
            montage.produce(self.script, self.assets, self.folder, run=FakeFfmpeg())

    def test_quality_problems_are_reported(self):
        report = montage.produce(self.script, self.assets, self.folder, run=FakeFfmpeg(duration=30, lufs=-20, width=1280))
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["problemas"]), 3)

    def test_loudness_uses_two_passes(self):
        fake = FakeFfmpeg()
        montage.produce(self.script, self.assets, self.folder, run=fake)
        final = next(c for c in fake.calls if "concat" in c)
        audio = final[final.index("-af") + 1]
        self.assertIn("measured_I=-19.80", audio)
        self.assertIn("linear=true", audio)

    def test_missing_voice(self):
        (self.folder / "voz" / "narracion.wav").unlink()
        with self.assertRaises(montage.MontageError):
            montage.produce(self.script, self.assets, self.folder, run=FakeFfmpeg())

    def test_thumbnail_uses_the_brand_colours(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeFfmpeg()
            out = montage.thumbnail(Path(tmp) / "img.jpg", "Una frase muy larga para la miniatura", Path(tmp) / "m.jpg", fake)
            vf = fake.calls[0][fake.calls[0].index("-vf") + 1]
            self.assertIn("color=0x111113@0.75", vf)        # franja obsidiana
            self.assertEqual(vf.count("drawtext"), 2)        # dos líneas...
            self.assertEqual(vf.count("fontcolor=0xC29A5B"), 1)  # ...y solo una en bronce
            self.assertTrue(out.exists())
            self.assertEqual(list(Path(tmp).glob("*.txt")), [])  # los textos temporales se borran

    def test_thumbnail_one_line_stays_white(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeFfmpeg()
            montage.thumbnail(Path(tmp) / "img.jpg", "Lo traicionó", Path(tmp) / "m.jpg", fake)
            vf = fake.calls[0][fake.calls[0].index("-vf") + 1]
            self.assertEqual(vf.count("drawtext"), 1)
            self.assertNotIn("fontcolor=0xC29A5B", vf)
            self.assertIn("h=6:color=0xC29A5B", vf)          # el filo de bronce sí está

    def test_thumbnail_lines(self):
        self.assertEqual(montage._thumb_lines("LO TRAICIONÓ"), ["LO TRAICIONÓ"])
        self.assertEqual(len(montage._thumb_lines("UNA FRASE MUY LARGA PARA UNA MINIATURA DE VERDAD")), 2)


if __name__ == "__main__":
    unittest.main()
