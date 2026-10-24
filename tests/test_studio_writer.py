"""Creator Studio, paso 6: escritor de guiones y piloto automático. Gemini y Commons FALSOS: nada sale a internet.

El montaje tampoco se hace: el paso «producir» se sustituye por uno falso (ffmpeg y Kokoro ya tienen sus tests).
"""

import json
import os
import tempfile
import threading
import tomllib
import unittest
from datetime import datetime, timedelta
from unittest import mock

from arthur.studio import autopilot, project, writer
from arthur.studio.gemini import GeminiError, GeminiRefused
from arthur.studio.jobs import JobRunner
from arthur.studio.store import StudioStore
from arthur.studio.review import ReviewBook

WORDS = " ".join(["palabra"] * 60)


def scene(n, **extra):
    data = {"texto": f"Escena {n}. {WORDS}", "fuentes": ["dion-casio"], "lugar": "LXXII, 22",
            "imagen_busqueda": "Marcus Aurelius bust"}
    data.update(extra)
    return data


def good_script(scenes=10):
    body = [scene(n) for n in range(1, scenes)]
    body.append({"texto": f"La lección. {WORDS}", "opinion": True, "imagen_busqueda": "stoa"})
    return {"titulo": "La traición de Avidio Casio", "descripcion": "Una historia real.", "etiquetas": ["estoicismo"],
            "miniatura_texto": "TRAICIÓN", "corto": {"escenas": [1, 2, 3], "titulo": "Le traicionó"},
            "escenas": body}


def sized_script(words):
    """Guion bueno de 10 escenas con `words` palabras en total."""
    data = good_script()
    per, extra = divmod(words, 10)
    for n, s in enumerate(data["escenas"]):
        s["texto"] = " ".join(["palabra"] * (per + (1 if n < extra else 0)))
    return data


class FakeGemini:
    """Contesta según la tarea que se ve en el texto. `reviews` = lo que dirá el revisor cada vez que se le pregunte."""

    def __init__(self, script=None, reviews=None, rewritten=None):
        self.script = script or good_script()
        # una reescritura o una lista (una por vuelta; la última se repite)
        self.rewritten = rewritten if isinstance(rewritten, list) else [rewritten or good_script()]
        self.reviews = list(reviews or [[]])
        self.idea = {"tema": "La traición de Avidio Casio", "protagonista": "Marco Aurelio", "gancho": "perdonó",
                     "fuentes": ["dion-casio"], "lugares": "LXXII 22-31"}
        self.calls = []
        self.prompts = []
        self.write_prompts = []
        self.fail = None
        self.changed = good_script()

    def generate_json(self, prompt, *, system="", temperature=0.7):
        if self.fail:
            raise self.fail
        if ("Propón la MEJOR" in prompt or "Propón UNA carta" in prompt) and "Escribe el guion" not in prompt:
            self.prompts.append(prompt)
            self.calls.append("idea")
            return dict(self.idea)
        if "historiador muy estricto" in prompt:
            self.calls.append("revisar")
            return {"problemas": self.reviews.pop(0) if self.reviews else []}
        if "pide cambios" in prompt:
            self.calls.append("cambiar")
            self.prompts.append(prompt)
            return json.loads(json.dumps(self.changed))
        if "tiene problemas" in prompt:
            self.calls.append("reescribir")
            self.prompts.append(prompt)
            out = self.rewritten.pop(0) if len(self.rewritten) > 1 else self.rewritten[0]
            return json.loads(json.dumps(out))
        if "Escribe el guion" in prompt:
            self.calls.append("escribir")
            self.write_prompts.append(prompt)
            return json.loads(json.dumps(self.script))
        if "Traduce al inglés" in prompt:
            self.calls.append("traducir")
            texts = json.loads(prompt[prompt.index("{"):])
            return {"titulo": "Betrayed by his best general", "descripcion": "A true story.",
                    "miniatura_texto": "BETRAYED", "corto_titulo": "He betrayed him",
                    "escenas": [f"Scene {n}. " + " ".join(["word"] * 60) for n in range(len(texts["escenas"]))]}
        raise AssertionError(f"petición inesperada: {prompt[:80]}")


class FakeCommons:
    def __init__(self, good=("File:Marcus.jpg",)):
        self.good = set(good)
        self.searches = []

    def search(self, query, limit=10):
        self.searches.append(query)
        return ["File:NoComercial.jpg", "File:Dibujo.svg", *sorted(self.good)]

    def info(self, title):
        if title == "File:Dibujo.svg":
            return {"title": title, "ok": True, "mime": "image/svg+xml"}
        return {"title": title, "ok": title in self.good, "mime": "image/jpeg"}


class ShapeTest(unittest.TestCase):
    def test_good_script_has_no_problems(self):
        self.assertEqual(writer.check_shape(good_script()), [])

    def test_what_the_machine_can_catch_without_a_model(self):
        bad = good_script()
        bad["escenas"][0]["fuentes"] = ["libro-inventado"]
        bad["escenas"][1]["lugar"] = ""
        bad["escenas"][2]["fuentes"] = []
        bad["escenas"][3]["fuentes"] = "dion-casio"   # texto en vez de lista
        problems = " ".join(writer.check_shape(bad))
        for expected in ("fuera del catálogo: libro-inventado", "escena 2 no dice", "escena 3 no cita",
                         "escena 4 deben ser una lista"):
            self.assertIn(expected, problems)
        self.assertIn("entre 10 y 18 escenas", writer.check_shape(good_script(5))[0])
        short = good_script()
        for s in short["escenas"]:
            s["texto"] = "Muy corto."
        self.assertIn("palabras", writer.check_shape(short)[0])
        self.assertEqual(writer.check_shape(sized_script(520)), [])   # un poco corto vale (3:40 más o menos)
        self.assertIn("palabras", writer.check_shape(sized_script(480))[0])

    def test_toml_takes_sources_from_the_catalog_and_escapes_everything(self):
        data = good_script()
        data["escenas"][0]["texto"] = 'Dijo "basta"\n[[fuentes]]\nid = "falsa"\\'
        data["escenas"][0]["url"] = "https://malo.example"   # Gemini no decide direcciones
        data["corto"]["escenas"] = [2, 99, 2, "1", 1]        # números que no existen o repetidos
        for s in data["escenas"]:
            s["imagen"] = "File:Marcus.jpg"
        text = writer.to_toml(data, "es", "kokoro:ef_dora")
        script = project.parse_script(tomllib.loads(text))
        self.assertEqual(len(script.escenas), 10)
        self.assertEqual([s.id for s in script.fuentes], ["dion-casio"])
        self.assertEqual(script.fuentes[0].url, writer.CATALOG["dion-casio"]["url"])
        self.assertIn("LXXII, 22", script.fuentes[0].obra)       # el lugar exacto sale en las fuentes
        self.assertIn('"basta"', script.escenas[0].texto)
        self.assertEqual(script.corto.escenas, (1, 2))
        self.assertNotIn("malo.example", text)


class WriterTest(unittest.TestCase):
    def test_idea_must_cite_the_catalog(self):
        fake = FakeGemini()
        w = writer.Writer(fake, FakeCommons())
        self.assertEqual(w.idea(["Otro vídeo"])["tema"], "La traición de Avidio Casio")
        for sources in (["libro-inventado"], "dion-casio", [{"id": "x"}], []):
            fake.idea["fuentes"] = sources
            with self.assertRaises(writer.WriterError):
                w.idea([])

    def test_idea_can_follow_the_topic_marc_asked_for(self):
        fake = FakeGemini()
        writer.Writer(fake, FakeCommons()).idea([], tema="Séneca y la ira")
        self.assertIn("Marc ha pedido este tema", fake.prompts[-1])
        self.assertIn("Séneca y la ira", fake.prompts[-1])
        writer.Writer(fake, FakeCommons()).idea([])
        self.assertNotIn("Marc ha pedido", fake.prompts[-1])

    def test_letter_format_must_cite_the_letters_to_lucilius(self):
        fake = FakeGemini()
        w = writer.Writer(fake, FakeCommons())
        with self.assertRaises(writer.WriterError):  # la idea falsa cita a Dion Casio, no las cartas
            w.idea([], formato="carta")
        fake.idea["fuentes"] = ["seneca-cartas"]
        idea = w.idea([], formato="carta")
        self.assertEqual(idea["formato"], "carta")
        self.assertIn("Cartas a Lucilio", fake.prompts[-1])
        with self.assertRaises(writer.WriterError):
            w.idea([], formato="inventado")

    def test_the_format_reaches_the_script_prompt(self):
        fake = FakeGemini()
        writer.Writer(fake, FakeCommons())._write({"tema": "x", "formato": "carta"})
        self.assertEqual(fake.calls, ["escribir"])
        self.assertIn("Formato del vídeo: Propón UNA carta", fake.write_prompts[-1])

    def test_pilot_alternates_formats_by_weekday(self):
        from datetime import datetime
        from arthur.studio import autopilot
        days = [autopilot.format_for(datetime(2026, 10, d)) for d in range(12, 19)]  # lunes 12 → domingo 18
        self.assertEqual(days, ["prueba", "prueba", "carta", "prueba", "prueba", "prueba", "carta"])

    def test_clean_script_is_reviewed_once(self):
        fake = FakeGemini()
        data, problems = writer.Writer(fake, FakeCommons()).script({"tema": "x"})
        self.assertEqual(problems, [])
        self.assertEqual(fake.calls, ["escribir", "revisar"])

    def test_doubts_trigger_one_rewrite_and_what_remains_goes_to_marc(self):
        fake = FakeGemini(reviews=[["La fecha 175 es dudosa"], ["Sigue sin estar claro el lugar"]])
        data, problems = writer.Writer(fake, FakeCommons()).script({"tema": "x"})
        self.assertEqual(fake.calls, ["escribir", "revisar", "reescribir", "revisar"])
        self.assertEqual(problems, ["Sigue sin estar claro el lugar"])

    def test_broken_shape_is_rewritten_and_refused_if_still_broken(self):
        fake = FakeGemini(script=good_script(4))
        writer.Writer(fake, FakeCommons()).script({"tema": "x"})
        self.assertEqual(fake.calls, ["escribir", "reescribir", "revisar"])
        fake = FakeGemini(script=good_script(4), rewritten=good_script(3))
        with self.assertRaises(writer.WriterError):
            writer.Writer(fake, FakeCommons()).script({"tema": "x"})

    def test_rewrite_that_shortens_the_script_gets_one_more_try(self):
        # Lo que pasó en la 4.ª tanda: el revisor pidió cambios y la reescritura dejó 543 palabras
        fake = FakeGemini(reviews=[["Fecha dudosa"], ["Sigue dudosa"], []], rewritten=[sized_script(400), good_script()])
        data, problems = writer.Writer(fake, FakeCommons()).script({"tema": "x"})
        self.assertEqual(fake.calls, ["escribir", "revisar", "reescribir", "reescribir", "revisar"])
        self.assertEqual(writer.check_shape(data), [])
        self.assertEqual(problems, ["Sigue dudosa"])
        # la reescritura recuerda la longitud y dice cuánto tiene ahora
        self.assertIn(f"{writer.TARGET_WORDS[0]}-{writer.TARGET_WORDS[1]} palabras", fake.prompts[0])
        self.assertIn("ahora tiene 400", fake.prompts[1])

    def test_images_only_with_valid_license_or_the_fallback(self):
        commons = FakeCommons()
        data = writer.Writer(FakeGemini(), commons).images(good_script())
        self.assertEqual(data["escenas"][0]["imagen"], "File:Marcus.jpg")
        self.assertEqual(commons.searches, ["Marcus Aurelius bust", "stoa"])   # una búsqueda por consulta distinta
        data = writer.Writer(FakeGemini(), FakeCommons(good=())).images(good_script())
        self.assertEqual({s["imagen"] for s in data["escenas"]}, {writer.FALLBACK_IMAGE})

    def test_draft_and_translation_keep_scenes_images_and_sources(self):
        fake = FakeGemini()
        w = writer.Writer(fake, FakeCommons())
        draft = w.draft(fake.idea)
        self.assertEqual(draft.script.voz, "kokoro:ef_dora")
        english = project.parse_script(tomllib.loads(w.translate(draft.toml)))
        self.assertEqual(english.idioma, "en")
        self.assertEqual(english.voz, "kokoro:af_heart")
        self.assertEqual(english.titulo, "Betrayed by his best general")
        self.assertEqual([s.imagen for s in english.escenas], [s.imagen for s in draft.script.escenas])
        self.assertEqual(english.fuentes, draft.script.fuentes)
        self.assertEqual(english.corto.escenas, draft.script.corto.escenas)
        self.assertTrue(english.escenas[0].texto.startswith("Scene 0."))

    def test_translation_with_missing_scenes_is_refused(self):
        fake = FakeGemini()
        w = writer.Writer(fake, FakeCommons())
        draft = w.draft(fake.idea)
        with mock.patch.object(fake, "generate_json", return_value={"escenas": ["Only one."]}):
            with self.assertRaises(writer.WriterError):
                w.translate(draft.toml)


class AutopilotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.fake = FakeGemini(reviews=[["La fecha es dudosa"], ["La fecha sigue siendo dudosa"]])
        self.produced = []

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def runner(self):
        def produce(store, name, progress):
            self.produced.append(name)
            return {"largo": len(self.produced)}
        steps = autopilot.autopilot_steps(self.store, gemini=lambda: self.fake, commons=FakeCommons,
                                          now=lambda: datetime(2026, 10, 13, 6), produce_fn=produce)
        return JobRunner(self.store, {autopilot.KIND: steps}, backoff=timedelta(0))

    def test_one_story_in_both_languages_ready_for_review_never_published(self):
        runner = self.runner()
        job = runner.submit(autopilot.KIND, {})
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "done")
        name = "20261013-la-traicion-de-avidio-casio"
        self.assertEqual(self.produced, [name, f"{name}-en"])
        for folder in (project.project_dir(name), project.project_dir(f"{name}-en")):
            project.load_script(folder / "guion.toml")
            self.assertEqual(ReviewBook(self.store).warnings(folder.name), ["La fecha sigue siendo dudosa"])
        self.assertEqual(self.fake.calls, ["idea", "escribir", "revisar", "reescribir", "revisar", "traducir"])

    def test_same_title_never_overwrites_a_project(self):
        name = "20261013-la-traicion-de-avidio-casio"
        project.project_dir(name).mkdir(parents=True)
        runner = self.runner()
        runner.submit(autopilot.KIND, {})
        runner.run_once()
        self.assertEqual(self.produced[0], f"{name}-2")

    def test_refusal_fails_at_once_and_busy_gemini_is_retried(self):
        runner = self.runner()
        self.fake.fail = GeminiRefused("Hoy ya se han hecho 18 peticiones")
        job = runner.submit(autopilot.KIND, {})
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "failed")
        self.assertIn("18 peticiones", self.store.get_job(job.id).error)
        self.fake.fail = GeminiError("ocupado")
        job = runner.submit(autopilot.KIND, {})
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "queued")
        self.fake.fail = None
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "done")

    def test_daily_loop_launches_once_a_day_from_the_set_hour(self):
        clock = [datetime(2026, 10, 13, 2, 0)]
        submitted = []

        def submit():
            submitted.append(clock[0])
            self.store.create_job(autopilot.KIND, {}, now=clock[0])

        stop = threading.Event()

        def tick(_):
            # Cada vuelta del bucle avanza el reloj falso una hora; tras tres días, se para.
            clock[0] += timedelta(hours=1)
            if clock[0] > datetime(2026, 10, 15, 23):
                stop.set()
            return stop.is_set()

        with mock.patch.object(stop, "wait", side_effect=tick):
            autopilot.daily_loop(self.store, submit, stop, hour=6, now=lambda: clock[0], poll=0)
        self.assertEqual([d.date().isoformat() for d in submitted], ["2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertEqual({d.hour for d in submitted}, {6})


class WarningsTest(unittest.TestCase):
    def test_bad_or_missing_check_file_is_not_an_autopilot_project(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            store = StudioStore.in_memory()
            book = ReviewBook(store)
            self.assertIsNone(book.warnings("no-existe"))
            folder = project.project_dir("roto")
            folder.mkdir(parents=True)
            (folder / "comprobacion.json").write_text("{no es json")
            self.assertIsNone(book.warnings("roto"))
            (folder / "comprobacion.json").write_text(json.dumps({"problemas": ["x" * 999] * 20}))
            self.assertEqual(len(book.warnings("roto")), 10)
            self.assertEqual(len(book.warnings("roto")[0]), 300)
            self.assertIsNone(book.warnings("../../etc"))   # nombre no válido: no se lee nada fuera de proyectos
            store.close()


class AutoPublishTest(unittest.TestCase):
    """A las 18:00 se aprueba solo lo del piloto, reciente y sin dudas. Lo demás espera a Marc."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.clock = [datetime(2026, 10, 13, 9)]
        self.book = ReviewBook(self.store, now=lambda: self.clock[0])

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def make(self, name, problems=None):
        data = good_script()
        for s in data["escenas"]:
            s["imagen"] = "File:Marcus.jpg"
        folder = project.project_dir(name)
        (folder / "salida").mkdir(parents=True)
        (folder / "guion.toml").write_text(writer.to_toml(data, "es", "kokoro:ef_dora"))
        (folder / "salida" / "video.mp4").write_bytes(name.encode() * 100)
        (folder / "salida" / "miniatura.jpg").write_bytes(b"jpg")
        (folder / "salida" / "control.json").write_text('{"ok": true}')
        if problems is not None:
            (folder / "comprobacion.json").write_text(json.dumps({"problemas": problems}))
        return self.book.submit(name)

    def run_loop_at(self, hour):
        self.clock[0] = self.clock[0].replace(hour=hour)
        enqueued, stop = [], threading.Event()
        with mock.patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            autopilot.auto_publish_loop(self.book, enqueued.append, stop, hour=18, now=lambda: self.clock[0])
        return enqueued

    def test_only_recent_autopilot_videos_without_doubts(self):
        clean = self.make("limpio", problems=[])
        doubtful = self.make("dudoso", problems=["La fecha es dudosa"])
        by_hand = self.make("a-mano")
        discarded = self.make("descartado", problems=[])
        self.book.discard(discarded.id, "movil")
        self.clock[0] -= timedelta(days=2)
        old = self.make("viejo", problems=[])
        self.clock[0] += timedelta(days=2)

        self.assertEqual(self.run_loop_at(17), [])            # antes de la hora, nada
        self.assertEqual(self.run_loop_at(18), [clean.id])
        self.assertEqual(self.book.get(clean.id).decided_by, autopilot.AUTO_DEVICE)
        for review in (doubtful, by_hand, old):
            self.assertEqual(self.book.get(review.id).status, "pending")
        self.assertEqual(self.run_loop_at(19), [])            # ya aprobado: no se manda dos veces

    def test_a_video_changed_after_review_is_not_approved(self):
        review = self.make("cambiado", problems=[])
        (project.project_dir("cambiado") / "salida" / "video.mp4").write_bytes(b"otro montaje")
        self.assertEqual(self.run_loop_at(18), [])
        self.assertEqual(self.book.get(review.id).status, "pending")


if __name__ == "__main__":
    unittest.main()


class ChangesTest(unittest.TestCase):
    """«Pedir cambios»: Gemini rehace el guion de ese vídeo con lo que escribió Marc y se vuelve a montar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.fake = FakeGemini()
        self.commons = FakeCommons(good=("File:Marcus.jpg", "File:Stoa.jpg"))
        self.produced = []

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def english_toml(self):
        draft = writer.Writer(FakeGemini(), FakeCommons()).draft(dict(FakeGemini().idea))
        return writer.Writer(FakeGemini(), FakeCommons()).translate(draft.toml)

    def make_project(self, name, check=True):
        folder = project.project_dir(name)
        folder.mkdir(parents=True)
        (folder / "guion.toml").write_text(self.english_toml(), encoding="utf-8")
        if check:
            (folder / "comprobacion.json").write_text(json.dumps({"problemas": ["Paulina sobrevivió"]}),
                                                      encoding="utf-8")
        return folder

    def test_revise_follows_marc_keeps_language_and_valid_images(self):
        changed = good_script()
        for s in changed["escenas"]:
            s.pop("imagen_busqueda")
            s["imagen"] = "File:Marcus.jpg"
        changed["escenas"][4]["imagen"] = "File:Inventada.jpg"         # una imagen que no tenía el vídeo
        changed["escenas"][5]["imagen_busqueda"] = "Stoa of Athens"    # otra que pide buscar
        self.fake.changed = changed
        self.fake.reviews = [["Sigue diciendo que murió"], []]
        text, problems = writer.Writer(self.fake, self.commons).revise(self.english_toml(), "Quita lo de Paulina")
        data = tomllib.loads(text)
        self.assertEqual((data["idioma"], data["voz"]), ("en", "kokoro:af_heart"))
        self.assertEqual(problems, [])
        self.assertEqual(self.fake.calls, ["cambiar", "revisar", "reescribir", "revisar"])
        self.assertIn("Quita lo de Paulina", self.fake.prompts[0])
        self.assertIn("(en)", self.fake.prompts[0])
        self.assertIn("Lo que pidió quien revisa (respétalo): Quita lo de Paulina", self.fake.prompts[1])
        self.assertEqual(self.commons.searches, ["Marcus Aurelius bust"] * 9 + ["stoa"])  # la reescritura las pide

    def test_revise_keeps_images_the_video_already_had(self):
        original = tomllib.loads(self.english_toml())
        changed = good_script()
        for s, old in zip(changed["escenas"], original["escenas"]):
            s.pop("imagen_busqueda")
            s["imagen"] = old["imagen"]
        changed["escenas"][5]["imagen"] = "File:Inventada.jpg"
        self.fake.changed = changed
        text, _ = writer.Writer(self.fake, self.commons).revise(self.english_toml(), "Más corto el final")
        images = [s["imagen"] for s in tomllib.loads(text)["escenas"]]
        self.assertEqual(images[:5], [s["imagen"] for s in original["escenas"][:5]])
        self.assertEqual(self.commons.searches, ["stoic philosopher bust"])  # solo la inventada se busca de nuevo

    def run_changes(self, name, text="Quita lo de Paulina"):
        def produce(store, project_name, progress):
            self.produced.append(project_name)
            return {"largo": 9}
        steps = autopilot.changes_steps(self.store, gemini=lambda: self.fake, commons=lambda: self.commons,
                                        produce_fn=produce)
        runner = JobRunner(self.store, {autopilot.CHANGES_KIND: steps}, backoff=timedelta(0))
        job = runner.submit(autopilot.CHANGES_KIND, {"revision": 7, "proyecto": name, "texto": text, "por": "movil"})
        runner.run_once()
        return self.store.get_job(job.id)

    def test_job_rewrites_keeps_the_old_script_and_remounts(self):
        folder = self.make_project("seneca-en")
        before = (folder / "guion.toml").read_text(encoding="utf-8")
        job = self.run_changes("seneca-en")
        self.assertEqual(job.status, "done")
        self.assertEqual((folder / "guion.anterior.toml").read_text(encoding="utf-8"), before)
        self.assertEqual(json.loads((folder / "comprobacion.json").read_text())["problemas"], [])
        self.assertEqual(self.produced, ["seneca-en"])
        notice = autopilot.job_notice(self.store, job)
        self.assertIn("rehecho con tus cambios", notice)

    def test_hand_made_project_stays_hand_made(self):
        folder = self.make_project("marco-casio", check=False)
        self.assertEqual(self.run_changes("marco-casio").status, "done")
        self.assertFalse((folder / "comprobacion.json").exists())  # sin él no se publica solo ni en público

    def test_failure_tells_marc(self):
        self.make_project("seneca-en")
        self.fake.fail = GeminiRefused("Hoy ya se han hecho 18 peticiones")
        job = self.run_changes("seneca-en")
        self.assertEqual(job.status, "failed")
        self.assertIn("no se han podido hacer los cambios del vídeo #7", autopilot.job_notice(self.store, job))
