"""Frutinovelas diario (0.66.0): el episodio que monta Studio solo se aprueba a las 18:00 y sube en PÚBLICO con la
marca de contenido sintético. Todo con falsos: YouTube nunca se toca."""

import dataclasses
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from arthur.studio import autopilot, channels, importer, project, publish
from arthur.studio.review import CHECK_FILE, FRUTAS_FILE, ReviewBook, ReviewError, project_meta
from arthur.studio.store import StudioStore


def make_episode(name="20261019-frutas-el-secreto", problems=(), title="El secreto de Fina | Frutinovelas",
                 profile="frutinovelas"):
    folder = project.project_dir(name)
    out = folder / "salida" / "corto"
    out.mkdir(parents=True)
    (out / "video.mp4").write_bytes(b"mp4 frutas " * 100)
    (out / "miniatura.jpg").write_bytes(b"fotograma")
    (out / "control.json").write_text(json.dumps({"ok": True, "duracion": 40.0}))
    (folder / FRUTAS_FILE).write_text(json.dumps({
        "perfil": profile, "titulo": title, "descripcion": "Fina descubre algo.\n\nHecho con IA.",
        "etiquetas": ["frutinovelas", "shorts"], "resumen": "Fina descubre algo."}, ensure_ascii=False))
    (folder / CHECK_FILE).write_text(json.dumps({"problemas": list(problems)}))
    return name


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.clock = [datetime(2026, 10, 19, 11)]
        self.book = ReviewBook(self.store, now=lambda: self.clock[0])

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()


class MetadataTest(Base):
    def test_generated_episode_goes_to_frutinovelas_marked_as_synthetic(self):
        name = make_episode()
        meta = project_meta(self.store, name, "corto")
        self.assertEqual((meta["channel"], meta["profile"], meta["language"], meta["kind"], meta["synthetic"]),
                         ("@TuCanalFrutinovelas", "frutinovelas", "es", "corto", True))
        review = self.book.submit(name, "corto")
        self.assertEqual((review.profile, review.imported, review.status), ("frutinovelas", False, "pending"))

    def test_only_a_shorts_profile_can_have_a_generated_episode(self):
        name = make_episode(profile="estoicismo_es")
        with self.assertRaisesRegex(ReviewError, "Frutinovelas"):
            project_meta(self.store, name, "corto")

    def test_changed_texts_break_the_fingerprint(self):
        name = make_episode()
        review = self.book.submit(name, "corto")
        record = project.project_dir(name) / FRUTAS_FILE
        data = json.loads(record.read_text())
        data["titulo"] = "Otro"
        record.write_text(json.dumps(data))
        self.assertNotEqual(self.book.current(review).meta, review.meta_sha256)

    def test_ready_to_publish_is_public_and_synthetic(self):
        review = self.book.submit(make_episode(), "corto")
        self.book.approve(review.id, review.video_sha256, autopilot.AUTO_DEVICE)
        _, meta = self.book.ready_to_publish(review.id)
        self.assertEqual((meta["privacy"], meta["synthetic"], meta["thumbnail"]), ("public", True, None))


class AutoApproveTest(Base):
    def run_loop(self):
        enqueued, stop = [], threading.Event()
        with mock.patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            autopilot.auto_publish_loop(self.book, enqueued.append, stop, hour=18, now=lambda: self.clock[0])
        return enqueued

    def test_clean_episode_is_approved_at_six(self):
        review = self.book.submit(make_episode(), "corto")
        self.clock[0] = self.clock[0].replace(hour=17, minute=59)
        self.assertEqual(self.run_loop(), [])
        self.clock[0] = self.clock[0].replace(hour=18, minute=1)
        self.assertEqual(self.run_loop(), [review.id])
        self.assertEqual(self.book.get(review.id).decided_by, autopilot.AUTO_DEVICE)

    def test_episode_with_doubts_waits_for_marc(self):
        self.book.submit(make_episode(problems=["plano regenerado sin revisar"]), "corto")
        self.clock[0] = self.clock[0].replace(hour=18, minute=5)
        self.assertEqual(self.run_loop(), [])

    def test_imported_short_still_never_goes_alone(self):
        source = Path(self.tmp.name) / "ep.mp4"
        source.write_bytes(b"mp4 " * 500)

        def media(args):
            if args[0] == "ffprobe":
                return json.dumps({"streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
                    {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": "30"}})
            if any("ebur128" in a for a in args):
                return "    I:         -14.0 LUFS\n"
            Path(args[-1]).write_bytes(b"fotograma")
            return ""

        review = importer.import_short(self.book, "frutinovelas", source, "Episodio 28", "", None, run=media)
        folder = project.project_dir(review.project)
        (folder / CHECK_FILE).write_text(json.dumps({"problemas": []}))
        (folder / FRUTAS_FILE).write_text(json.dumps({"perfil": "frutinovelas"}))  # ni con esto
        self.clock[0] = self.clock[0].replace(hour=18, minute=5)
        self.assertEqual(self.run_loop(), [])

    def test_not_while_the_channel_publishing_is_off(self):
        self.book.submit(make_episode(), "corto")
        off = dataclasses.replace(channels.PROFILES["frutinovelas"], publishing=False)
        self.clock[0] = self.clock[0].replace(hour=18, minute=5)
        with mock.patch.dict(channels.PROFILES, {"frutinovelas": off}):
            self.assertEqual(self.run_loop(), [])

    def test_frutinovelas_is_the_only_channel_turned_on_now(self):
        self.assertTrue(channels.PROFILES["frutinovelas"].publishing)
        self.assertTrue(channels.PROFILES["estoicismo_es"].publishing)


class UploadTest(Base):
    def test_synthetic_flag_goes_to_youtube_only_when_set(self):
        sent = []
        uploader = publish.YouTubeUploader.__new__(publish.YouTubeUploader)
        uploader.upload_url = "https://www.googleapis.com/upload/youtube/v3"

        def fake_json(method, url, body=None, headers=None):
            sent.append(body)
            return {}, {"Location": "https://www.googleapis.com/upload/youtube/v3/x?upload_id=1"}

        uploader._json = fake_json
        uploader._send = lambda *a: "VIDEO12345"
        video = Path(self.tmp.name) / "v.mp4"
        video.write_bytes(b"x")
        base = {"video": str(video), "title": "t", "description": "d", "tags": [], "language": "es",
                "privacy": "public", "thumbnail": None}
        uploader.upload({**base, "synthetic": True})
        uploader.upload(base)
        self.assertIs(sent[0]["status"]["containsSyntheticMedia"], True)
        self.assertNotIn("containsSyntheticMedia", sent[1]["status"])


if __name__ == "__main__":
    unittest.main()
