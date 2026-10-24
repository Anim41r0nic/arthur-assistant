"""Creator Studio: importar Shorts ya hechos fuera de Studio (Frutinovelas) a la página de revisión.

Todo con falsos: ffprobe/ffmpeg son una función que contesta lo que se le diga y YouTube nunca se toca.
"""

import dataclasses
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from arthur.__main__ import main
from arthur.security.devices import DeviceRegistry
from arthur.studio import autopilot, channels, importer, project, publish
from arthur.studio.review import CHECK_FILE, ReviewBook, ReviewError, project_meta
from arthur.studio.store import MIGRATIONS, StudioStore
from arthur.studio.web import Publisher, StudioWeb


class FakeMedia:
    """ffprobe/ffmpeg falsos. ffprobe dice lo que se configure; el fotograma se «saca» escribiendo el fichero."""

    def __init__(self, width=1080, height=1920, vcodec="h264", acodec="aac", duration=34.0, lufs=-15.2):
        self.width, self.height, self.vcodec, self.acodec = width, height, vcodec, acodec
        self.duration, self.lufs = duration, lufs
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "ffprobe":
            streams = [{"codec_type": "video", "codec_name": self.vcodec, "width": self.width, "height": self.height}]
            if self.acodec:
                streams.append({"codec_type": "audio", "codec_name": self.acodec})
            return json.dumps({"streams": streams, "format": {"duration": str(self.duration)}})
        if any("ebur128" in a for a in args):
            return f"  Integrated loudness:\n    I:         {self.lufs} LUFS\n"
        Path(args[-1]).write_bytes(b"fotograma")
        return ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.clock = [datetime(2026, 10, 17, 9)]
        self.book = ReviewBook(self.store, now=lambda: self.clock[0])
        self.media = FakeMedia()
        self.source = Path(self.tmp.name) / "fuera" / "episodio-02-el-plan-34s.mp4"
        self.source.parent.mkdir()
        self.source.write_bytes(b"mp4 de frutinovelas " * 500)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def bring(self, title="Había dos anillos. Nico ocultaba un plan | Frutinovelas 2", description="Fina pregunta.",
              profile="frutinovelas", source=None, srt=None):
        return importer.import_short(self.book, profile, source or self.source, title, description, srt,
                                     run=self.media)

    def enable_fruta(self):
        on = dataclasses.replace(channels.PROFILES["frutinovelas"], publishing=True)
        return mock.patch.dict(channels.PROFILES, {"frutinovelas": on})

    def disable_fruta(self):
        # Desde 0.66.0 la publicación de Frutinovelas está encendida; estas pruebas comprueban el interruptor.
        off = dataclasses.replace(channels.PROFILES["frutinovelas"], publishing=False)
        return mock.patch.dict(channels.PROFILES, {"frutinovelas": off})


class ImportTest(Base):
    def test_import_creates_a_pending_short_marked_as_imported(self):
        srt = self.source.with_name("subtitulos.srt")
        srt.write_text("1\n00:00:00,350 --> 00:00:03,800\nNico, mira este corazón.\n", encoding="utf-8")
        review = self.bring(srt=srt)
        self.assertEqual((review.status, review.kind, review.profile, review.channel),
                         ("pending", "corto", "frutinovelas", "@TuCanalFrutinovelas"))
        self.assertEqual(review.origin, str(self.source.resolve()))
        self.assertTrue(review.imported)
        folder = project.project_dir(review.project)
        out = folder / "salida" / "corto"
        self.assertEqual((out / "video.mp4").read_bytes(), self.source.read_bytes())  # copia, no el original
        self.assertEqual((out / "miniatura.jpg").read_bytes(), b"fotograma")
        self.assertIn("Nico, mira", (out / "subtitulos.srt").read_text(encoding="utf-8"))
        control = json.loads((out / "control.json").read_text())
        self.assertEqual((control["ok"], control["duracion"], control["ancho"], control["alto"], control["lufs"]),
                         (True, 34.0, 1080, 1920, -15.2))
        record = json.loads((folder / importer.IMPORT_FILE).read_text())
        self.assertEqual((record["perfil"], record["origen"], record["titulo"]),
                         ("frutinovelas", str(self.source.resolve()), review.title))
        self.assertFalse((folder / CHECK_FILE).exists())  # nunca es «del piloto»
        self.assertTrue(self.source.exists())  # el original sigue donde estaba
        self.assertEqual([r.id for r in self.book.list(profile="frutinovelas")], [review.id])
        self.assertEqual(self.book.list(profile="estoicismo_es"), [])

    def test_only_profiles_that_make_shorts(self):
        for name in ("estoicismo_es", "es", "estoicismo_en", "inventado"):
            with self.subTest(perfil=name), self.assertRaises(importer.ImportShortError):
                self.bring(profile=name)
        self.assertFalse(project.projects_dir().exists())

    def test_bad_videos_are_rejected_without_leaving_anything(self):
        cases = {"horizontal": dict(width=1920, height=1080), "hevc": dict(vcodec="hevc"),
                 "mp3": dict(acodec="mp3"), "mudo": dict(acodec=None), "largo": dict(duration=61.0),
                 "vacío": dict(duration=0)}
        for what, settings in cases.items():
            with self.subTest(what):
                self.media = FakeMedia(**settings)
                with self.assertRaises(importer.ImportShortError):
                    self.bring()
        self.media = FakeMedia(duration=60.0)
        self.bring()  # 60 s justos todavía es un Short
        missing = self.source.with_name("no-existe.mp4")
        with self.assertRaises(importer.ImportShortError):
            self.bring(source=missing)
        other = self.source.with_suffix(".mov")
        other.write_bytes(b"x")
        with self.assertRaises(importer.ImportShortError):
            self.bring(source=other)
        self.assertEqual(len(list(project.projects_dir().iterdir())), 1)

    def test_texts_are_checked_before_copying(self):
        for title in ("", "   ", "x" * 101, "Fina <3 Nico"):
            with self.subTest(titulo=title), self.assertRaises(importer.ImportShortError):
                self.bring(title=title)
        with self.assertRaises(importer.ImportShortError):
            self.bring(description="é" * 2600)  # más de 5.000 bytes
        with self.assertRaises(importer.ImportShortError):
            self.bring(description="a > b")  # YouTube no admite < ni > en los textos
        bad_srt = self.source.with_name("malo.srt")
        bad_srt.write_bytes(b"\xff\xfe\x00binario")
        with self.assertRaises(importer.ImportShortError):
            self.bring(srt=bad_srt)
        with self.assertRaises(importer.ImportShortError):
            self.bring(srt=self.source.with_name("falta.srt"))
        self.assertFalse(project.projects_dir().exists())

    def test_description_keeps_its_paragraphs(self):
        review = self.bring(description="Primer párrafo.\r\n\r\n#Frutinovelas #Shorts  ")
        detail = project_meta(self.store, review.project, "corto")
        self.assertEqual(detail["description"], "Primer párrafo.\n\n#Frutinovelas #Shorts")
        self.assertEqual(detail["tags"], ["shorts"])
        self.assertEqual(detail["language"], "es")

    def test_importing_again_does_not_duplicate_and_new_texts_replace(self):
        first = self.bring()
        self.assertEqual(self.bring().id, first.id)
        second = self.bring(title="Otro título | Frutinovelas 2")
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.project, first.project)  # mismo vídeo, misma carpeta
        self.assertEqual(self.book.get(first.id).status, "replaced")


class ApproveAndPublishTest(Base):
    def test_imported_short_is_never_auto_approved(self):
        review = self.bring()
        # Aunque alguien deje un comprobacion.json vacío en su carpeta (lo que marca «del piloto, sin dudas»).
        (project.project_dir(review.project) / CHECK_FILE).write_text(json.dumps({"problemas": []}))
        self.assertEqual(autopilot.auto_approvable(self.book, self.clock[0] + timedelta(hours=1)), [])
        enqueued, stop = [], threading.Event()
        self.clock[0] = self.clock[0].replace(hour=18, minute=30)
        with mock.patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            autopilot.auto_publish_loop(self.book, enqueued.append, stop, hour=18, now=lambda: self.clock[0])
        self.assertEqual(enqueued, [])
        self.assertEqual(self.book.get(review.id).status, "pending")

    def test_explicit_approval_and_publish_check(self):
        review = self.bring()
        with self.assertRaises(ReviewError):
            self.book.approve(review.id, "otra-huella", "movil")
        approved = self.book.approve(review.id, review.video_sha256, "movil")
        self.assertEqual(approved.status, "approved")
        _, meta = self.book.ready_to_publish(review.id)
        # Frutinovelas es un canal de un vídeo al día: lo importado que aprueba Marc sale en público (19-oct-2026)
        # y declarado como hecho con IA.
        self.assertEqual((meta["channel"], meta["profile"], meta["kind"], meta["privacy"], meta["thumbnail"]),
                         ("@TuCanalFrutinovelas", "frutinovelas", "corto", "public", None))
        self.assertTrue(meta["synthetic"])
        self.assertEqual(Path(meta["video"]).read_bytes(), self.source.read_bytes())
        self.assertTrue(Path(meta["video"]).is_relative_to(project.projects_dir()))

    def test_changed_texts_after_approval_block_publishing(self):
        review = self.bring()
        self.book.approve(review.id, review.video_sha256, "movil")
        record = project.project_dir(review.project) / importer.IMPORT_FILE
        data = json.loads(record.read_text())
        data["titulo"] = "Cambiado a escondidas"
        record.write_text(json.dumps(data))
        with self.assertRaises(ReviewError):
            self.book.ready_to_publish(review.id)

    def test_nothing_is_uploaded_while_frutinovelas_publishing_is_off(self):
        review = self.bring()
        self.book.approve(review.id, review.video_sha256, "movil")
        asked = []
        with self.disable_fruta(), self.assertRaises(publish.PublishError) as caught:
            publish.publish(self.book, review.id, uploader_for=asked.append)
        self.assertIn("desactivada", str(caught.exception))
        self.assertEqual(asked, [])

    def test_uploads_through_the_normal_path_when_enabled(self):
        review = self.bring()
        self.book.approve(review.id, review.video_sha256, "movil")

        class Uploader:
            profile = channels.PROFILES["frutinovelas"]

            def check_channel(self):
                return {"handle": "@TuCanalFrutinovelas", "id": "UCfruta", "title": "Frutinovelas"}

            def upload(self, meta, progress):
                sent.append(meta)
                return "VIDEOFRUTA1", None

        sent = []
        with self.enable_fruta():
            Uploader.profile = channels.PROFILES["frutinovelas"]
            publish.publish(self.book, review.id, uploader_for=lambda name: Uploader())
        self.assertEqual(sent[0]["privacy"], "public")
        self.assertEqual(sent[0]["title"], review.title)
        self.assertEqual(self.book.get(review.id).status, "published")

    def test_publisher_waits_while_the_channel_is_off(self):
        review = self.bring()
        self.book.approve(review.id, review.video_sha256, "movil")
        done = []
        with self.disable_fruta():
            publisher = Publisher(self.book, lambda book, n: done.append(n))
            publisher.start()
            publisher.join()
        self.assertEqual(done, [])  # sigue «aprobado»: se sube cuando se active la publicación
        self.assertEqual(self.book.get(review.id).status, "approved")
        with self.enable_fruta():
            on = Publisher(self.book, lambda book, n: done.append(n))
            on.start()
            on.join()
        # Encendido tampoco sale al arrancar: es un canal de un vídeo al día y lo saca drip_due a su hora.
        self.assertEqual(done, [])

    def test_imported_from_a_channel_without_daily_limit_stays_private(self):
        review = self.bring()
        self.book.approve(review.id, review.video_sha256, "movil")
        free = dataclasses.replace(channels.PROFILES["frutinovelas"], per_day=0)
        with mock.patch.dict(channels.PROFILES, {"frutinovelas": free}):
            _, meta = self.book.ready_to_publish(review.id)
        self.assertEqual(meta["privacy"], "private")
        self.assertNotIn("synthetic", meta)


class DailyDripTest(Base):
    """Frutinovelas: un vídeo al día a la hora de publicar, el aprobado más antiguo primero (Marc, 19-oct-2026)."""

    def approve_three(self):
        reviews = []
        for n in range(3):
            source = Path(self.tmp.name) / "fuera" / f"episodio-{n}.mp4"
            source.write_bytes(f"mp4 {n} ".encode() * 500)
            review = self.bring(title=f"Frutinovelas {n + 1}", source=source)
            reviews.append(self.book.approve(review.id, review.video_sha256, "movil"))
        return reviews

    def loop_once(self, at):
        enqueued, stop = [], threading.Event()
        self.clock[0] = at
        with mock.patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            autopilot.auto_publish_loop(self.book, enqueued.append, stop, hour=18, now=lambda: self.clock[0])
        return enqueued

    def test_nothing_before_the_publishing_hour(self):
        self.approve_three()
        self.assertEqual(self.loop_once(datetime(2026, 10, 19, 17, 59)), [])

    def test_one_a_day_oldest_first(self):
        first, second, _ = self.approve_three()
        self.assertEqual(autopilot.waiting(self.book, "frutinovelas"), 3)
        self.assertEqual(self.loop_once(datetime(2026, 10, 19, 18, 0)), [first.id])
        self.book.mark_published(first.id, "YT1")
        # El mismo día, ya hecho el de hoy, no sale otro (aunque se reinicie la página).
        self.assertEqual(self.loop_once(datetime(2026, 10, 19, 21, 0)), [])
        self.assertEqual(self.loop_once(datetime(2026, 10, 20, 18, 5)), [second.id])
        self.assertEqual(autopilot.waiting(self.book, "frutinovelas"), 2)

    def test_only_one_while_it_is_still_uploading(self):
        first, _, _ = self.approve_three()
        enqueued, sent = [], set()
        at = datetime(2026, 10, 19, 18, 0)
        for minute in range(3):  # el bucle vuelve cada minuto; la subida aún no ha terminado
            for review in autopilot.drip_due(self.book, at + timedelta(minutes=minute), sent):
                sent.add((review.target.id, at.date()))
                enqueued.append(review.id)
        self.assertEqual(enqueued, [first.id])

    def test_a_failed_upload_uses_up_the_day(self):
        # Sin cuota de YouTube, uno falla: no se tiran detrás todos los de la cola el mismo día.
        first, second, _ = self.approve_three()
        self.clock[0] = datetime(2026, 10, 19, 18, 1)
        self.book.mark_failed(first.id, "límite diario")
        self.assertEqual(autopilot.drip_due(self.book, datetime(2026, 10, 19, 18, 2)), [])
        self.assertEqual(autopilot.drip_due(self.book, datetime(2026, 10, 20, 18, 0)), [second])

    def test_nothing_while_publishing_is_off(self):
        self.approve_three()
        with self.disable_fruta():
            self.assertEqual(autopilot.drip_due(self.book, datetime(2026, 10, 19, 18, 0)), [])


class WebTest(Base):
    def setUp(self):
        super().setUp()
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.key = self.devices.add("movil")
        self.enqueued = []
        self.publisher = mock.Mock()
        self.publisher.enqueue.side_effect = self.enqueued.append
        self.web = StudioWeb(self.book, self.devices, "127.0.0.1", 0, publisher=self.publisher)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.review = self.bring()

    def tearDown(self):
        self.web.shutdown()
        super().tearDown()

    def call(self, path, body=None):
        req = urllib.request.Request(self.web.url + path, method="POST" if body is not None else "GET")
        req.add_header("Authorization", f"Bearer {self.key}")
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_list_and_detail_show_it_as_imported(self):
        status, data = self.call("/api/reviews?perfil=frutinovelas")
        self.assertEqual(status, 200)
        row = data["reviews"][0]
        self.assertEqual((row["id"], row["imported"], row["publishing"], row["kind"]),
                         (self.review.id, True, True, "corto"))
        status, detail = self.call(f"/api/reviews/{self.review.id}")
        self.assertEqual(status, 200)
        self.assertNotIn("problem", detail)
        self.assertEqual((detail["description"], detail["sources"], detail["origin"], detail["autopilot"]),
                         ("Fina pregunta.", [], str(self.source.resolve()), False))
        self.assertEqual(detail["duration"], 34.0)

    def test_approve_in_the_page_but_no_upload_while_off(self):
        with self.disable_fruta():
            status, body = self.call(f"/api/reviews/{self.review.id}/approve",
                                     {"video_sha256": self.review.video_sha256})
        self.assertEqual((status, body["status"]), (200, "approved"))
        self.assertEqual(self.enqueued, [])

    def test_approve_waits_for_its_day_on_a_daily_channel(self):
        with self.enable_fruta():
            status, body = self.call(f"/api/reviews/{self.review.id}/approve",
                                     {"video_sha256": self.review.video_sha256})
        self.assertEqual((status, body["status"], self.enqueued), (200, "approved", []))

    def test_approve_queues_the_upload_on_a_channel_without_daily_limit(self):
        free = dataclasses.replace(channels.PROFILES["frutinovelas"], publishing=True, per_day=0)
        with mock.patch.dict(channels.PROFILES, {"frutinovelas": free}):
            status, _ = self.call(f"/api/reviews/{self.review.id}/approve", {"video_sha256": self.review.video_sha256})
        self.assertEqual((status, self.enqueued), (200, [self.review.id]))

    def test_asking_for_changes_does_not_start_a_remake(self):
        # No hay guion que rehacer: se apunta lo que hay que cambiar y Marc rehace el vídeo fuera de Studio.
        status, body = self.call(f"/api/reviews/{self.review.id}/changes", {"text": "Sube el volumen"})
        self.assertEqual((status, body["status"]), (200, "changes"))
        self.assertEqual(self.store.list_jobs(), [])


class CommandTest(Base):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("arthur.studio.montage.run_ffmpeg", self.media), redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_command_imports_into_the_review_page(self):
        code, out, err = self.run_main("--studio-importar-short", "frutinovelas", str(self.source),
                                       "--titulo", "Fina vio el anillo | Frutinovelas 1",
                                       "--descripcion", "Fina reconoce un anillo.")
        self.assertEqual(code, 0, err)
        self.assertIn("importado", out.lower())
        store = StudioStore()
        try:
            reviews = ReviewBook(store).list(profile="frutinovelas")
        finally:
            store.close()
        self.assertEqual([(r.title, r.status, r.imported) for r in reviews],
                         [("Fina vio el anillo | Frutinovelas 1", "pending", True)])

    def test_command_errors(self):
        code, _, err = self.run_main("--studio-importar-short", "estoicismo_es", str(self.source), "--titulo", "X")
        self.assertEqual(code, 1)
        self.assertIn("corto", err.lower())
        with self.assertRaises(SystemExit):  # sin --titulo: argparse lo dice y no se hace nada
            self.run_main("--studio-importar-short", "frutinovelas", str(self.source))


class SchemaTest(unittest.TestCase):
    def test_reviews_have_an_origin_column(self):
        store = StudioStore.in_memory()
        try:
            columns = [row[1] for row in store._db.execute("PRAGMA table_info(reviews)")]
            self.assertIn("origin", columns)
            self.assertEqual(store.schema_version, len(MIGRATIONS))
        finally:
            store.close()

    def test_profiles_say_what_they_make(self):
        self.assertEqual(channels.PROFILES["frutinovelas"].product, "corto")
        self.assertEqual(channels.PROFILES["estoicismo_es"].product, "largo")
        self.assertTrue(channels.PROFILES["frutinovelas"].publishing)  # encendida por Marc el 18-oct-2026


if __name__ == "__main__":
    unittest.main()
