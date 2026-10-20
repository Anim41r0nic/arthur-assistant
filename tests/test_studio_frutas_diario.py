"""Frutinovelas diario (0.66.0): guion con Workers AI, planos, voces, montaje y revisión, todo con falsos.

Ni Workers AI, ni Kokoro, ni ffmpeg, ni YouTube de verdad: un motor de texto que contesta lo que se le diga, una voz
que escribe WAV mudos de la duración pedida y un ffmpeg falso que «crea» los ficheros."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from arthur.cerebro.budget import Budget
from arthur.notes.engines import EngineError, EngineRefused
from arthur.studio import autopilot, frutas_diario as fd, frutas_directo, project
from arthur.studio.jobs import JobRunner, StepFailed
from arthur.studio.review import CHECK_FILE, FRUTAS_FILE, ReviewBook
from arthur.studio.store import StudioStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b"\x00\x00" * int(24000 * seconds))


class FakeText:
    """Workers AI de texto falso: devuelve las respuestas en orden y apunta lo que se le pidió."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def ask(self, prompt, *, max_tokens=400, timeout=20):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


class FakeVoice:
    def __init__(self, seconds, log):
        self.seconds, self.log = seconds, log

    def check(self):
        pass

    def speak(self, text, output):
        self.log.append(text)
        write_wav(Path(output), self.seconds)

    def close(self):
        pass


class FakeMedia:
    """ffmpeg/ffprobe falsos. ffprobe dice la duración que tenga el guion montado."""

    def __init__(self):
        self.calls = []
        self.duration = 0.0

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps({"streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
                {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": str(self.duration)}})
        joined = " ".join(args)
        if "print_format=json" in joined:
            return ('{"input_i": "-24.0", "input_tp": "-6.0", "input_lra": "3.0", "input_thresh": "-34.0", '
                    '"target_offset": "0.1"}')
        if "ebur128" in joined:
            return "    I:         -14.2 LUFS\n"
        Path(args[-1]).write_bytes(PNG if args[-1].endswith(".png") else b"mp4 simulado")
        return ""


def episode(planos=("F1", "N1"), texts=None, **extra):
    texts = texts or ["Nico, ¿qué escondes?", "Nada, Fina, de verdad."]
    scenes = []
    for i in range(8):
        plano = planos[i % len(planos)]
        scenes.append({"personaje": "fina" if plano.startswith("F") else "nico", "plano": plano,
                       "gesto": "camara", "objetos": [], "hechos": [], "camara": "empuje" if i == 0 else "acercar",
                       "texto": texts[i % len(texts)]})
    data = {"titulo": "El secreto del jardín", "descripcion": "Fina sospecha de Nico.",
            "resumen": "Fina sospecha de Nico.", "etiquetas": ["drama"], "escenas": scenes}
    data.update(extra)
    return data


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(base / "datos")})
        self.env.start()
        self.lib = base / "biblioteca"
        (self.lib / "planos").mkdir(parents=True)
        (self.lib / "ref").mkdir()
        self.files = {"F1": PNG, "N1": JPG, "F2": PNG + b"2"}
        for pid, data in self.files.items():
            (self.lib / "planos" / f"{pid}.{'jpg' if data is JPG else 'png'}").write_bytes(data)
        (self.lib / "ref" / "fina.png").write_bytes(PNG)
        (self.lib / "ref" / "nico.png").write_bytes(PNG)
        self.catalog = {
            "version": 1, "estado_inicial": {},
            "personajes": {
                "fina": {"nombre": "Fina", "voz": "kokoro:ef_dora", "descripcion": "Fresa.", "papel": "Pregunta.",
                         "referencia": "ref/fina.png", "referencia_sha256": sha(PNG)},
                "nico": {"nombre": "Nico", "voz": "kokoro:em_alex", "descripcion": "Plátano.", "papel": "Ayuda.",
                         "referencia": "ref/nico.png", "referencia_sha256": sha(PNG)}},
            "planos": [self.plano(pid) for pid in self.files]}
        self.catalog_path = base / "catalogo.json"
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")
        self.store = StudioStore.in_memory()
        self.budget = Budget(base / "neuronas.json", daily=1500)
        self.media = FakeMedia()
        self.spoken = []
        self.seconds = 2.0
        font = base / "letra.ttf"
        font.write_bytes(b"letra")
        self.fonts = mock.patch.object(frutas_directo, "FONTS", (font,))
        self.fonts.start()
        self.images = []

    def plano(self, pid):
        data = self.files[pid]
        return {"id": pid, "personaje": "fina" if pid.startswith("F") else "nico",
                "archivo": f"planos/{pid}.{'jpg' if data is JPG else 'png'}", "sha256": sha(data),
                "encuadre": "medio", "emocion": "sorpresa", "validado": True, "gestos": ["camara"],
                "objetos_visibles": [], "hechos_visibles": [], "prompt": ""}

    def tearDown(self):
        self.fonts.stop()
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def steps(self, text):
        test = self

        class Images:
            def generate(self, prompt, reference_png):
                test.images.append(prompt)
                return JPG

        def voices(voice, speed):
            return FakeVoice(self.seconds if speed == 1 else self.fast_seconds(), self.spoken)

        return fd.frutas_steps(self.store, text_engine=lambda: text, image_engine=lambda: Images(),
                               budget=lambda: self.budget, catalog_path=self.catalog_path, root=lambda: self.lib,
                               voices=voices, run=self.media, now=lambda: datetime(2026, 10, 19, 10))

    def fast_seconds(self):
        return self.seconds

    def run_job(self, text):
        runner = JobRunner(self.store, {fd.KIND: self.steps(text)}, max_attempts=2,
                           backoff=timedelta(seconds=0), now=datetime.now)
        job = runner.submit(fd.KIND, {})
        self.media.duration = 8 * round(max(fd.MIN_SCENE, self.seconds + fd.SCENE_PAD), 2)
        runner.run_once()
        return self.store.get_job(job.id)


class NormalizeTest(Base):
    def test_valid_episode_passes_and_code_sets_numbers_states_and_moves(self):
        data = episode()
        data["escenas"][1]["camara"] = "girar"            # inventado: el código pone el de siempre
        result, reasons = fd.normalize(data, self.catalog, set(self.files))
        self.assertEqual(reasons, [])
        self.assertEqual([s["numero"] for s in result["escenas"]], list(range(1, 9)))
        self.assertEqual(result["escenas"][0]["movimiento"], "empuje")
        self.assertEqual(result["escenas"][1]["movimiento"], "acercar")
        self.assertTrue(all(s["cambios"] == [] and s["estado_antes"] == {} for s in result["escenas"]))

    def test_reasons_are_readable(self):
        data = episode(texts=["x" * 80])
        data["escenas"][2]["plano"] = "LOLA"
        data["escenas"][3]["objetos"] = ["anillo"]
        data["titulo"] = "<b>"
        _, reasons = fd.normalize(data, self.catalog, set(self.files))
        text = " ".join(reasons)
        for expected in ("título", "60 caracteres", "plano inexistente", "anillo"):
            self.assertIn(expected, text)

    def test_too_few_scenes_or_a_single_character(self):
        data = episode(planos=("F1",))
        _, reasons = fd.normalize(data, self.catalog, set(self.files))
        self.assertIn("Deben hablar al menos dos personajes.", reasons)
        data["escenas"] = data["escenas"][:5]
        _, reasons = fd.normalize(data, self.catalog, set(self.files))
        self.assertTrue(any("entre 8 y 10" in r for r in reasons))


class WriterTest(Base):
    def test_writer_retries_with_the_reasons_then_gives_up(self):
        bad = episode(texts=["x" * 90])
        text = FakeText("no es json", bad, bad)
        writer = fd.EpisodeWriter(text, self.budget, self.catalog, set(self.files))
        with self.assertRaisesRegex(StepFailed, "no pasa las comprobaciones"):
            writer.write([])
        self.assertEqual(len(text.prompts), 3)
        self.assertIn("JSON válido", text.prompts[1])
        self.assertIn("60 caracteres", text.prompts[2])

    def test_budget_is_checked_before_calling(self):
        tiny = Budget(Path(self.tmp.name) / "poco.json", daily=10)
        text = FakeText(episode())
        writer = fd.EpisodeWriter(text, tiny, self.catalog, set(self.files))
        with self.assertRaisesRegex(StepFailed, "Neurons"):
            writer.write([])
        self.assertEqual(text.prompts, [])

    def test_spending_is_recorded(self):
        writer = fd.EpisodeWriter(FakeText(episode()), self.budget, self.catalog, set(self.files))
        writer.write([])
        self.assertGreater(self.budget.spent_today(), 0)
        self.assertLess(self.budget.spent_today(), 200)

    def test_prompt_only_offers_usable_shots_and_cast(self):
        prompt = fd.write_prompt(self.catalog, {"F1", "N1"}, [{"titulo": "Ayer", "resumen": "pasó algo"}], [])
        self.assertIn("- F1:", prompt)
        self.assertNotIn("- F2:", prompt)
        self.assertIn("Ayer: pasó algo", prompt)


class JobTest(Base):
    def test_full_day_leaves_a_clean_pending_short_that_goes_public_at_six(self):
        text = FakeText(episode(), {"problemas": []})
        job = self.run_job(text)
        self.assertEqual(job.status, "done", job.error)
        outputs = {s.name: s.output for s in self.store.steps(job.id)}
        name = outputs["guion"]["proyecto"]
        self.assertTrue(name.startswith("20261019-frutas-el-secreto"))
        folder = project.project_dir(name)
        self.assertEqual(json.loads((folder / CHECK_FILE).read_text())["problemas"], [])
        texts = json.loads((folder / FRUTAS_FILE).read_text())
        self.assertIn("inteligencia artificial", texts["descripcion"])
        self.assertIn("frutinovelas", texts["etiquetas"])
        manifest = json.loads((folder / "guion.json").read_text())
        self.assertEqual(len(manifest["escenas"]), 8)
        self.assertEqual(manifest["escenas"][0]["voz"], "voz/01.wav")
        self.assertEqual(manifest["escenas"][0]["duracion"], round(2.0 + fd.SCENE_PAD, 2))
        self.assertTrue((folder / "planos" / "F1.png").exists())
        self.assertTrue((folder / "salida" / "corto" / "miniatura.jpg").exists())
        book = ReviewBook(self.store, now=lambda: datetime.now())
        review = book.get(outputs["montar"]["corto"])
        self.assertEqual((review.profile, review.imported, review.status), ("frutinovelas", False, "pending"))
        self.assertEqual(fd.job_review(self.store, job), review.id)
        self.assertIn("en público, a las 18:00", fd.job_notice(self.store, job))
        later = datetime.fromisoformat(review.created_at) + timedelta(hours=1)
        self.assertEqual([r.id for r in autopilot.auto_approvable(book, later)], [review.id])

    def test_reviewer_doubts_trigger_one_rewrite_and_remaining_doubts_block_auto_publish(self):
        text = FakeText(episode(), {"problemas": ["La historia no se entiende"]},
                        episode(texts=["Fina, te lo explico.", "Gracias, Nico."]), {"problemas": ["Sigue rara"]})
        job = self.run_job(text)
        self.assertEqual(job.status, "done", job.error)
        self.assertIn("La historia no se entiende", text.prompts[2])   # la reescritura lleva lo del revisor
        folder = project.project_dir({s.name: s.output for s in self.store.steps(job.id)}["guion"]["proyecto"])
        self.assertEqual(json.loads((folder / CHECK_FILE).read_text())["problemas"], ["Sigue rara"])
        self.assertIn("Fina, te lo explico.", self.spoken)
        self.assertIn("no se publica solo", fd.job_notice(self.store, job))
        book = ReviewBook(self.store)
        self.assertEqual(autopilot.auto_approvable(book, datetime.now() + timedelta(hours=1)), [])

    def test_missing_shot_is_generated_and_becomes_a_doubt(self):
        (self.lib / "planos" / "N1.jpg").unlink()
        text = FakeText(episode(), {"problemas": []})
        job = self.run_job(text)
        self.assertEqual(job.status, "done", job.error)
        self.assertEqual(len(self.images), 1)
        self.assertIn("Nico", self.images[0])
        self.assertTrue((fd.generated_dir() / "N1.jpg").exists())
        self.assertGreaterEqual(self.budget.spent_today(), fd.IMAGE_NEURONS)
        folder = project.project_dir({s.name: s.output for s in self.store.steps(job.id)}["guion"]["proyecto"])
        problems = json.loads((folder / CHECK_FILE).read_text())["problemas"]
        self.assertTrue(any("N1" in p for p in problems))

    def test_a_different_photo_with_the_same_name_is_not_used(self):
        (self.lib / "planos" / "F1.png").write_bytes(PNG + b"otra")
        found = fd.resolve_planos(self.catalog, self.lib, fd.generated_dir())
        self.assertIsNone(found["F1"])
        self.assertIsNotNone(found["N1"])

    def test_paths_cannot_escape_the_library(self):
        self.catalog["planos"][0]["archivo"] = "../../etc/passwd"
        self.assertIsNone(fd.resolve_planos(self.catalog, self.lib, fd.generated_dir())["F1"])

    def test_too_long_voice_is_retried_faster_then_fails(self):
        self.seconds = 5.0
        job = self.run_job(FakeText(episode(), {"problemas": []}))
        self.assertEqual(job.status, "failed")
        self.assertIn("dura 5.0 s", job.error)
        self.assertEqual(len(self.spoken), 2)   # normal y algo más deprisa
        self.assertIn("hoy no hay Frutinovelas", fd.job_notice(self.store, job))

    def test_refused_token_fails_without_retry_and_busy_is_retried(self):
        job = self.run_job(FakeText(EngineRefused("token malo")))
        self.assertEqual(job.status, "failed")
        job = self.run_job(FakeText(EngineError("ocupado")))
        self.assertEqual(job.status, "queued")

    def test_daily_loop_starts_one_frutas_job_per_day(self):
        submitted, stop = [], threading.Event()
        clock = [datetime(2026, 10, 19, 9, 59)]

        def submit():
            submitted.append(self.store.create_job(fd.KIND, {}, now=clock[0]))

        def tick(_):
            if clock[0].hour >= 11:
                stop.set()
            clock[0] += timedelta(minutes=30)

        with mock.patch.object(stop, "wait", side_effect=tick):
            autopilot.daily_loop(self.store, submit, stop, hour=10, now=lambda: clock[0], kind=fd.KIND)
        self.assertEqual(len(submitted), 1)


class ConfigTest(unittest.TestCase):
    def test_hour(self):
        self.assertIsNone(fd.hour_from_env({}))
        self.assertIsNone(fd.hour_from_env({"ARTHUR_STUDIO_FRUTAS_HORA": "no"}))
        self.assertEqual(fd.hour_from_env({"ARTHUR_STUDIO_FRUTAS_HORA": "10"}), 10)
        for bad in ("25", "diez", "-1"):
            with self.assertRaises(ValueError):
                fd.hour_from_env({"ARTHUR_STUDIO_FRUTAS_HORA": bad})

    def test_missing_account_or_token_is_a_clear_failure_without_secrets(self):
        with tempfile.NamedTemporaryFile("w") as f:
            f.write("t" * 40)
            f.flush()
            with self.assertRaisesRegex(StepFailed, "ARTHUR_STUDIO_CF_ACCOUNT"):
                fd.text_engine_from_env({"ARTHUR_STUDIO_CF_TOKEN_FILE": f.name})
            engine = fd.text_engine_from_env({"ARTHUR_STUDIO_CF_ACCOUNT": "a" * 32, "ARTHUR_STUDIO_CF_TOKEN_FILE": f.name})
            self.assertNotIn("t" * 40, repr(engine))

    def test_real_catalog_uses_only_the_operative_cast_with_images_and_hashes(self):
        catalog = fd.load_catalog()
        self.assertEqual(set(catalog["personajes"]), {"fina", "nico", "vera", "mora", "berto"})
        for plano in catalog["planos"]:
            self.assertIn(plano["personaje"], catalog["personajes"])
            self.assertRegex(plano["archivo"], r"\.(png|jpg)$")
            self.assertEqual(len(plano["sha256"]), 64)
            self.assertTrue(plano["validado"])
        voices = [c["voz"] for c in catalog["personajes"].values()]
        self.assertEqual(len(set(voices)), len(voices))            # una voz distinta por personaje
        self.assertTrue(all(v.startswith("kokoro:") for v in voices))


class ImageEngineTest(unittest.TestCase):
    def test_multipart_request_and_base64_answer(self):
        import base64
        import io

        seen = {}

        class Opener:
            def open(self, request, timeout):
                seen["url"], seen["body"] = request.full_url, request.data
                seen["type"] = request.get_header("Content-type")
                return io.BytesIO(json.dumps({"result": {"image": base64.b64encode(JPG).decode()}}).encode())

        engine = fd.ImageEngine("a" * 32, "t" * 40, opener=Opener())
        self.assertEqual(engine.generate("Fina sola", PNG), JPG)
        self.assertTrue(seen["url"].endswith("/ai/run/@cf/black-forest-labs/flux-2-klein-4b"))
        self.assertIn("multipart/form-data; boundary=", seen["type"])
        self.assertIn(b'name="input_image_0"', seen["body"])
        self.assertIn(b"Fina sola", seen["body"])
        self.assertNotIn("t" * 40, repr(engine))


class CliTest(unittest.TestCase):
    def test_frutas_now_runs_the_frutas_job_once(self):
        from arthur import __main__ as cli

        with mock.patch.object(cli, "studio_autopilot_command", return_value=0) as run:
            self.assertEqual(cli.main(["--studio-frutas-ahora"]), 0)
        run.assert_called_once_with(daily=False, kind="frutas")

    def test_bad_frutas_hour_stops_the_daily_service(self):
        from arthur import __main__ as cli

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp, "ARTHUR_STUDIO_FRUTAS_HORA": "las diez"}), \
                mock.patch("sys.stderr"):
            self.assertEqual(cli.studio_autopilot_command(daily=True), 2)


if __name__ == "__main__":
    unittest.main()
