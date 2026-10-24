"""Short directo de Frutinovelas: fotos y voces sintéticas, ffmpeg/ffprobe falsos. Ni Studio ni el escritorio."""

import io
import json
import os
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from arthur.studio import frutas_directo as frutas

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b"\x00\x00" * int(24000 * seconds))


class FakeMedia:
    """ffmpeg/ffprobe falsos: ffprobe dice lo que se configure y el resto «crea» el fichero de salida."""

    def __init__(self, duration=16.0, vcodec="h264", width=1080, height=1920):
        self.duration, self.vcodec, self.width, self.height = duration, vcodec, width, height
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps({"streams": [
                {"codec_type": "video", "codec_name": self.vcodec, "width": self.width, "height": self.height},
                {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": str(self.duration)}})
        joined = " ".join(args)
        if "print_format=json" in joined:   # primera pasada de loudnorm (montage.measure_loudness)
            return ('{"input_i": "-24.0", "input_tp": "-6.0", "input_lra": "3.0", "input_thresh": "-34.0", '
                    '"target_offset": "0.1"}')
        if "ebur128" in joined:
            return "    I:         -14.3 LUFS\n"
        Path(args[-1]).write_bytes(b"mp4 simulado")
        return ""

    def renders(self):
        return [a for a in self.calls if "-vf" in a]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "episodio"
        self.work.mkdir()
        (self.work / "foto.png").write_bytes(PNG)
        write_wav(self.work / "voz.wav", 1.0)
        self.manifest = self.work / "guion.json"
        self.scenes = [{"imagen": "foto.png", "voz": "voz.wav", "texto": "La receta sigue aquí.", "duracion": 2}
                       for _ in range(8)]
        self.save()
        self.font = self.root / "fuente.ttf"
        self.font.write_bytes(b"letra")
        self.fonts = mock.patch.object(frutas, "FONTS", (self.font,))
        self.fonts.start()

    def tearDown(self):
        self.fonts.stop()
        self.temp.cleanup()

    def save(self, data=None):
        self.manifest.write_text(json.dumps(data or {"escenas": self.scenes}), encoding="utf-8")


class ManifestTest(Base):
    def test_accepts_eight_to_twelve_explicit_scenes(self):
        self.assertEqual(len(frutas.load_manifest(self.manifest)), 8)
        self.scenes.extend(self.scenes[:4])
        self.save()
        self.assertEqual(len(frutas.load_manifest(self.manifest)), 12)
        self.scenes.append(self.scenes[0])
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "8 y 12"):
            frutas.load_manifest(self.manifest)

    def test_missing_resource_and_video_are_rejected(self):
        self.scenes[0] = {**self.scenes[0], "imagen": "no-existe.png"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "No existe imagen"):
            frutas.load_manifest(self.manifest)
        (self.work / "movimiento.mp4").write_bytes(b"x")
        self.scenes[0] = {**self.scenes[0], "imagen": "movimiento.mp4"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "no vídeo"):
            frutas.load_manifest(self.manifest)

    def test_image_must_really_be_an_image(self):
        # Una lista de reproducción disfrazada de .png haría que ffmpeg abriera otros ficheros o la red.
        (self.work / "lista.png").write_text("#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n", encoding="utf-8")
        self.scenes[0] = {**self.scenes[0], "imagen": "lista.png"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "no es de verdad una imagen PNG"):
            frutas.load_manifest(self.manifest)

    def test_paths_outside_the_script_folder_are_rejected(self):
        outside = self.root / "fuera.png"
        outside.write_bytes(PNG)
        for value in (str(outside), "../fuera.png"):
            self.scenes[0] = {**self.scenes[0], "imagen": value}
            self.save()
            with self.assertRaisesRegex(frutas.FrutasError, "fuera de las carpetas permitidas"):
                frutas.load_manifest(self.manifest)
        os.symlink(outside, self.work / "enlace.png")   # un enlace dentro que apunta fuera tampoco vale
        self.scenes[0] = {**self.scenes[0], "imagen": "enlace.png"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "fuera de las carpetas permitidas"):
            frutas.load_manifest(self.manifest)
        write_wav(self.root / "voz-fuera.wav", 1.0)
        self.scenes[0] = {**self.scenes[0], "imagen": "foto.png", "voz": "../voz-fuera.wav"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "voz está fuera"):
            frutas.load_manifest(self.manifest)

    def test_voice_must_fit_scene(self):
        write_wav(self.work / "voz-larga.wav", 2.0)
        self.scenes[0] = {**self.scenes[0], "duracion": 2, "voz": "voz-larga.wav"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "demasiado larga"):
            frutas.load_manifest(self.manifest)
        (self.work / "falsa.wav").write_bytes(b"no soy un wav")
        self.scenes[0] = {**self.scenes[0], "voz": "falsa.wav"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "WAV PCM"):
            frutas.load_manifest(self.manifest)

    def test_rejects_invalid_duration_and_unreadable_subtitle(self):
        for bad in (float("nan"), True, 1, 7, "4"):
            self.scenes[0] = {**self.scenes[0], "duracion": bad}
            self.save()
            with self.assertRaisesRegex(frutas.FrutasError, "duración"):
                frutas.load_manifest(self.manifest)
        self.scenes[0] = {**self.scenes[0], "duracion": 2, "texto": "Palabramuymuymuymuymuymuylarga"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "Palabra demasiado larga"):
            frutas.load_manifest(self.manifest)
        self.scenes[0] = {**self.scenes[0], "texto": "Dos\nlíneas"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "una línea"):
            frutas.load_manifest(self.manifest)

    def test_total_must_fit_a_short(self):
        write_wav(self.work / "voz.wav", 1.0)
        self.scenes = [{**self.scenes[0], "duracion": 6} for _ in range(11)]   # 66 s
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "supera 60"):
            frutas.load_manifest(self.manifest)


class CatalogTest(Base):
    """Con catálogo, el guion pasa por validar_guion y la foto sale del plano, no de lo que diga la escena."""

    def setUp(self):
        super().setUp()
        self.library = self.root / "biblioteca"
        (self.library / "recursos").mkdir(parents=True)
        planos = []
        for who in ("fina", "mora"):
            (self.library / "recursos" / f"{who}.png").write_bytes(PNG)
            planos.append({"id": who.upper(), "personaje": who, "archivo": f"recursos/{who}.png", "validado": True,
                           "gestos": ["sonrisa"], "objetos_visibles": [], "hechos_visibles": []})
        self.catalog = {"raiz_recursos": str(self.library), "estado_inicial": {"carpeta": "fina"},
                        "planos": planos}
        self.scenes = [{"numero": n, "personaje": who, "plano": who.upper(), "gesto": "sonrisa", "objetos": [],
                        "hechos": [], "estado_antes": {}, "cambios": [], "voz": "voz.wav",
                        "texto": "Cocinemos juntas.", "duracion": 2}
                       for n, who in enumerate(["fina", "mora"] * 4, 1)]
        self.save()

    def test_valid_script_takes_the_photo_from_the_shot(self):
        scenes = frutas.load_manifest(self.manifest, self.catalog)
        self.assertEqual(scenes[0].image, self.library / "recursos" / "fina.png")
        self.assertEqual(scenes[1].plano, "MORA")

    def test_invalid_script_is_held_with_readable_reasons_and_no_ffmpeg(self):
        self.scenes[2]["plano"] = "LOLA_INVENTADA"
        self.scenes[3]["objetos"] = ["anillo"]
        self.scenes[4]["estado_antes"] = {"carpeta": "vera"}
        self.save()
        media = FakeMedia()
        with self.assertRaises(frutas.FrutasError) as caught:
            frutas.produce(self.manifest, self.root / "salida", catalog=self.catalog, run=media)
        text = str(caught.exception)
        self.assertIn("validación editorial", text)
        self.assertIn("plano inexistente", text)
        self.assertIn("objeto 'anillo' no se ve", text)
        self.assertIn("continuidad rota", text)
        self.assertEqual(media.calls, [])
        self.assertFalse((self.root / "salida").exists())

    def test_scene_cannot_pick_another_photo_by_hand(self):
        self.scenes[0]["imagen"] = "foto.png"
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "quita «imagen»"):
            frutas.load_manifest(self.manifest, self.catalog)

    def test_shot_file_must_stay_inside_the_library(self):
        (self.root / "intrusa.png").write_bytes(PNG)
        self.catalog["planos"][0]["archivo"] = "../intrusa.png"
        with self.assertRaisesRegex(frutas.FrutasError, "fuera de las carpetas permitidas"):
            frutas.load_manifest(self.manifest, self.catalog)
        self.catalog["planos"][0]["archivo"] = "recursos/fina.png"
        self.catalog["raiz_recursos"] = "relativa/biblioteca"
        with self.assertRaisesRegex(frutas.FrutasError, "ruta absoluta"):
            frutas.load_manifest(self.manifest, self.catalog)


class ProduceTest(Base):
    def test_produces_checked_short_srt_and_control(self):
        media = FakeMedia(duration=16.0)
        report = frutas.produce(self.manifest, self.root / "salida", run=media)
        out = self.root / "salida"
        self.assertEqual((out / "video.mp4").read_bytes(), b"mp4 simulado")
        self.assertEqual(report["numero_escenas"], 8)
        self.assertEqual(report["lufs"], -14.3)
        self.assertFalse(report["validado_con_catalogo"])
        self.assertEqual(json.loads((out / "control.json").read_text())["duracion"], 16.0)
        subtitle = (out / "subtitulos.srt").read_text()
        self.assertEqual(subtitle.count("-->"), 8)
        self.assertIn("00:00:00,350 --> 00:00:01,500", subtitle)
        renders = media.renders()
        self.assertEqual(len(renders), 8)
        for args in renders:
            graph = args[args.index("-vf") + 1]
            # La foto se abre con el lector de imágenes, sin series «%d» ni otros formatos.
            self.assertEqual(args[args.index("-f") + 1:args.index("-f") + 4], ["image2", "-pattern_type", "none"])
            self.assertIn("zoompan=z='1+0.06*on/48'", graph)
            self.assertIn("x='iw/2-(iw/zoom/2)'", graph)          # acercamiento al centro, no a una esquina
            self.assertIn("textfile=", graph)
            self.assertNotIn("La receta", graph)                  # el texto nunca va dentro del filtro
            self.assertEqual(args[0], "ffmpeg")
        mix = next(a for a in media.calls if "amix" in " ".join(a))
        self.assertEqual(mix.count("wav"), 8)                     # cada voz se abre como WAV
        self.assertIn("adelay=2350:all=1", " ".join(mix))
        final = next(a for a in media.calls if "-f" in a and "concat" in a)
        self.assertEqual(final[final.index("-c:a") + 1], "aac")
        self.assertIn("loudnorm=I=-14", " ".join(final))
        self.assertEqual(media.calls[-2][0], "ffprobe")           # se comprueba como lo hará importer

    def test_wrong_result_leaves_no_video(self):
        for media, reason in ((FakeMedia(duration=20.0), "Duración final"),
                              (FakeMedia(vcodec="hevc"), "no vale como Short"),
                              (FakeMedia(width=1920, height=1080), "vertical")):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(frutas.FrutasError, reason):
                    frutas.produce(self.manifest, self.root / "salida", run=media)
                self.assertFalse((self.root / "salida" / "video.mp4").exists())

    def test_ffmpeg_failure_is_a_readable_error(self):
        def broken(args):
            raise frutas.MontageError("ffmpeg ha fallado: algo")
        with self.assertRaisesRegex(frutas.FrutasError, "ffmpeg ha fallado"):
            frutas.produce(self.manifest, self.root / "salida", run=broken)

    def test_refuses_to_overwrite_existing_video(self):
        output = self.root / "salida"
        output.mkdir()
        (output / "video.mp4").write_bytes(b"original")
        media = FakeMedia()
        with self.assertRaisesRegex(frutas.FrutasError, "Ya existe"):
            frutas.produce(self.manifest, output, run=media)
        self.assertEqual((output / "video.mp4").read_bytes(), b"original")
        self.assertEqual(media.calls, [])

    def test_font_path_with_filter_characters_is_refused(self):
        odd = self.root / "letra:rara,x.ttf"
        odd.write_bytes(b"letra")
        with mock.patch.object(frutas, "FONTS", (odd,)):
            with self.assertRaisesRegex(frutas.FrutasError, "incompatible con el filtro"):
                frutas.produce(self.manifest, self.root / "salida", run=FakeMedia())

    def test_command_line_explains_why_it_does_not_mount(self):
        self.scenes = self.scenes[:3]
        self.save()
        out = io.StringIO()
        with redirect_stdout(out):
            code = frutas.main([str(self.manifest), str(self.root / "salida")])
        self.assertEqual(code, 1)
        self.assertIn("No se monta: El Short necesita entre 8 y 12 escenas", out.getvalue())


class CameraTest(Base):
    """Opción 2 de Marc (18-oct): cada escena puede mover la cámara de otra forma; el cuerpo no se mueve."""

    def graphs(self, media):
        return [a[a.index("-vf") + 1] for a in media.renders()]

    def test_each_scene_can_choose_a_camera_move(self):
        moves = ["acercar", "alejar", "barrido_izq", "barrido_der", "empuje", "acercar", "alejar", "empuje"]
        for scene, move in zip(self.scenes, moves):
            scene["movimiento"] = move
        self.save()
        media = FakeMedia(duration=16.0)
        report = frutas.produce(self.manifest, self.root / "salida", run=media)
        graphs = self.graphs(media)
        self.assertIn("zoompan=z='1+0.06*on/48'", graphs[0])
        self.assertIn("zoompan=z='1+0.06*(1-on/48)'", graphs[1])
        self.assertIn("x='(iw-iw/zoom)*(1-on/48)'", graphs[2])
        self.assertIn("x='(iw-iw/zoom)*on/48'", graphs[3])
        self.assertIn("zoompan=z='1+0.15*on/48'", graphs[4])
        self.assertEqual([s["movimiento"] for s in report["escenas"]], moves)
        self.assertTrue(report["ok"])           # la página de revisión solo acepta un control con «ok»

    def test_without_move_it_keeps_the_slow_zoom(self):
        media = FakeMedia(duration=16.0)
        frutas.produce(self.manifest, self.root / "salida", run=media)
        self.assertTrue(all("zoompan=z='1+0.06*on/48'" in g for g in self.graphs(media)))

    def test_unknown_move_is_rejected_before_ffmpeg(self):
        self.scenes[0]["movimiento"] = "girar;rm"
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "movimiento de cámara"):
            frutas.load_manifest(self.manifest)

    def test_longer_voices_only_when_asked(self):
        write_wav(self.work / "voz-larga.wav", 4.0)
        self.scenes[0] = {**self.scenes[0], "duracion": 5, "voz": "voz-larga.wav"}
        self.save()
        with self.assertRaisesRegex(frutas.FrutasError, "demasiado larga"):
            frutas.load_manifest(self.manifest)
        self.assertEqual(frutas.load_manifest(self.manifest, max_voice=4.5)[0].voice_duration, 4.0)


if __name__ == "__main__":
    unittest.main()
