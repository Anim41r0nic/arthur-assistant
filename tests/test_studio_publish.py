"""Creator Studio, paso 5: subir a YouTube solo lo aprobado, al canal correcto, en privado y sin duplicados.

YouTube FALSO (un «opener» que imita la subida reanudable, con cortes): nada sale a internet.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from arthur.studio import project, publish
from arthur.studio.review import ReviewBook, ReviewError
from arthur.studio.store import StudioStore
from arthur.studio.web import Publisher

GUION = """
titulo = "Prueba"
descripcion = "Descripción."
idioma = "{idioma}"
[[fuentes]]
id = "f"
obra = "Obra"
autor = "Autor"
[[escenas]]
texto = "Uno."
imagen = "File:A.jpg"
fuentes = ["f"]
"""


class FakeAuth:
    def access_token(self):
        return "token-falso"


class Response(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def http_error(url, code, headers=None, body=b""):
    return urllib.error.HTTPError(url, code, "x", headers or {}, io.BytesIO(body))


class FakeYouTube:
    def __init__(self, handle="@ArthuroEstoicismo"):
        self.handle = handle
        self.received = bytearray()
        self.size = None
        self.metadata = None
        self.cut_last = False      # se corta la conexión justo al mandar el último trozo (pero llegó)
        self.thumb_fails = False
        self.calls = []

    def open(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        self.calls.append((request.get_method(), url.path))
        assert request.get_header("Authorization") == "Bearer token-falso"
        if url.path.endswith("/channels"):
            return Response(json.dumps({"items": [{"id": "UC123", "snippet": {"title": "Canal",
                                                                                "customUrl": self.handle.lower()}}]}).encode())
        if url.path.endswith("/videos") and request.get_method() == "POST":
            self.metadata = json.loads(request.data)
            self.size = int(request.get_header("X-upload-content-length"))
            return Response(b"", {"Location": "https://upload.example/session/1"})
        if url.path.endswith("/session/1"):
            content_range = request.get_header("Content-range")
            if content_range.startswith("bytes */"):  # «¿cuánto tienes?»
                if len(self.received) == self.size:
                    return Response(json.dumps({"id": "VIDEO123"}).encode())
                raise http_error(request.full_url, 308, {"Range": f"bytes=0-{len(self.received) - 1}"}
                                 if self.received else {})
            start = int(content_range.split()[1].split("-")[0])
            assert start == len(self.received), "trozo fuera de orden"
            self.received.extend(request.data)
            if len(self.received) < self.size:
                raise http_error(request.full_url, 308, {"Range": f"bytes=0-{len(self.received) - 1}"})
            if self.cut_last:
                self.cut_last = False
                raise urllib.error.URLError("conexión cortada")
            return Response(json.dumps({"id": "VIDEO123"}).encode())
        if url.path.endswith("/thumbnails/set"):
            if self.thumb_fails:
                raise http_error(request.full_url, 500)
            return Response(b"{}")
        raise AssertionError(request.full_url)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.chunk = mock.patch.object(publish, "CHUNK", 1000)
        self.chunk.start()
        self.store = StudioStore.in_memory()
        self.book = ReviewBook(self.store)
        self.yt = FakeYouTube()

    def tearDown(self):
        self.chunk.stop()
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def approved(self, idioma="es", approve=True):
        folder = project.project_dir(f"video-{idioma}")
        (folder / "salida").mkdir(parents=True, exist_ok=True)
        (folder / "guion.toml").write_text(GUION.format(idioma=idioma))
        (folder / "salida" / "video.mp4").write_bytes(os.urandom(3500))
        (folder / "salida" / "miniatura.jpg").write_bytes(b"jpg")
        (folder / "salida" / "control.json").write_text('{"ok": true}')
        review = self.book.submit(f"video-{idioma}")
        if approve:
            self.book.approve(review.id, review.video_sha256, "movil")
        return review, folder

    def uploader(self, language):
        return publish.YouTubeUploader(language, auth=FakeAuth(), opener=self.yt,
                                       api="https://api.example/youtube/v3", upload="https://upload.example/youtube/v3")

    def test_autopilot_videos_go_public_and_hand_made_stay_private(self):
        review, folder = self.approved()
        (folder / "comprobacion.json").write_text('{"problemas": []}')
        publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertEqual(self.yt.metadata["status"]["privacyStatus"], "public")

    def test_video_with_doubts_needs_the_word_publicar(self):
        # 2026-10-13: «Séneca frente a la muerte» salió en público con 5 errores tras un «Aprobar» rápido.
        review, folder = self.approved(approve=False)
        (folder / "comprobacion.json").write_text('{"problemas": ["Paulina sobrevivió (Tácito, Anales 15.64)"]}')
        for word in ("", "si", "publica"):
            with self.subTest(word=word), self.assertRaises(ReviewError) as caught:
                self.book.approve(review.id, review.video_sha256, "movil", word)
            self.assertIn("PUBLICAR", str(caught.exception))
        self.assertEqual(self.book.get(review.id).status, "pending")
        self.assertEqual(self.book.approve(review.id, review.video_sha256, "movil", " publicar ").status, "approved")

    def test_arthur_is_told_when_a_video_goes_up_or_fails(self):
        told = []
        review, folder = self.approved()
        (folder / "comprobacion.json").write_text('{"problemas": []}')
        publish.publish(self.book, review.id, uploader_for=self.uploader, notify=told.append)
        self.assertEqual(len(told), 1)
        self.assertIn("en público", told[0])
        self.yt.handle = "@OtroCanal"
        review, _ = self.approved("en")
        with self.assertRaises(publish.PublishError):
            publish.publish(self.book, review.id, uploader_for=self.uploader, notify=told.append)
        self.assertIn("NO se ha podido subir", told[1])

    def test_uploads_privately_in_chunks_with_thumbnail(self):
        review, folder = self.approved()
        video_id, warning = publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertEqual((video_id, warning), ("VIDEO123", None))
        self.assertEqual(bytes(self.yt.received), (folder / "salida" / "video.mp4").read_bytes())
        self.assertEqual(self.yt.metadata["status"]["privacyStatus"], "private")
        self.assertIn("Autor, Obra", self.yt.metadata["snippet"]["description"])
        self.assertEqual(self.yt.metadata["snippet"]["defaultAudioLanguage"], "es")
        self.assertIn(("POST", "/youtube/v3/thumbnails/set"), self.yt.calls)
        done = self.book.get(review.id)
        self.assertEqual((done.status, done.youtube_id), ("published", "VIDEO123"))

    def test_only_approved_videos(self):
        review, _ = self.approved(approve=False)
        with self.assertRaises(publish.PublishError):
            publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertEqual(self.yt.calls, [])

    def test_wrong_channel_uploads_nothing(self):
        self.yt.handle = "@OtroCanal"
        review, _ = self.approved()
        with self.assertRaises(publish.PublishError) as caught:
            publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertIn("@ArthuroEstoicismo", str(caught.exception))
        self.assertIsNone(self.yt.metadata)
        self.assertEqual(self.book.get(review.id).status, "failed")

    def test_english_video_needs_english_channel(self):
        review, _ = self.approved("en")  # el YouTube falso es el canal en español
        with self.assertRaises(publish.PublishError):
            publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertIsNone(self.yt.metadata)

    def test_cut_at_the_end_does_not_duplicate(self):
        review, folder = self.approved()
        self.yt.cut_last = True
        video_id, _ = publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertEqual(video_id, "VIDEO123")
        self.assertEqual(len(self.yt.received), (folder / "salida" / "video.mp4").stat().st_size)  # no se mandó dos veces

    def test_thumbnail_failure_keeps_it_published(self):
        review, _ = self.approved()
        self.yt.thumb_fails = True
        video_id, warning = publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertIn("miniatura", warning)
        done = self.book.get(review.id)
        self.assertEqual(done.status, "published")  # si fuera «failed», un reintento lo subiría dos veces

    def test_change_after_approval_blocks_upload(self):
        review, folder = self.approved()
        (folder / "salida" / "miniatura.jpg").write_bytes(b"otra")
        with self.assertRaises(publish.PublishError):
            publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertEqual(self.yt.calls, [])

    def test_retry_after_failure(self):
        self.yt.handle = "@OtroCanal"
        review, _ = self.approved()
        with self.assertRaises(publish.PublishError):
            publish.publish(self.book, review.id, uploader_for=self.uploader)
        self.assertEqual(self.book.retry(review.id, "movil").status, "approved")
        self.yt.handle = "@ArthuroEstoicismo"
        self.assertEqual(publish.publish(self.book, review.id, uploader_for=self.uploader)[0], "VIDEO123")

    def test_publisher_picks_up_approved_on_start(self):
        review, _ = self.approved()
        published = []
        publisher = Publisher(self.book, lambda book, rid: published.append(rid))
        publisher.start()
        publisher.join()
        self.assertEqual(published, [review.id])

    def test_install_client_validates(self):
        bad = Path(self.tmp.name) / "malo.json"
        bad.write_text('{"web": {}}')
        with self.assertRaises(publish.PublishError):
            publish.install_client(bad)
        good = Path(self.tmp.name) / "bueno.json"
        good.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')
        target = publish.install_client(good)
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
