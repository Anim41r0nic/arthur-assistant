"""Creator Studio, paso 4: revisión con aprobación atada a las huellas y la página para el móvil.

La página se prueba con un servidor real en 127.0.0.1 (puerto libre) y datos falsos en una carpeta temporal.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from arthur.security.devices import DeviceRegistry
from arthur.studio import project
from arthur.studio.review import CHANNELS, ReviewBook, ReviewError
from arthur.studio.store import StudioStore
from arthur.studio.web import StudioWeb

GUION = """
titulo = "Prueba <script>alert(1)</script>"
descripcion = "Una descripción."
idioma = "{idioma}"
etiquetas = ["estoicismo"]
[[fuentes]]
id = "f"
obra = "Obra"
autor = "Autor"
[[escenas]]
texto = "Uno."
imagen = "File:A.jpg"
fuentes = ["f"]
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.book = ReviewBook(self.store)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def make_project(self, name="marco", idioma="es", video=b"video-1" * 1000, ok=True):
        folder = project.project_dir(name)
        (folder / "salida").mkdir(parents=True, exist_ok=True)
        (folder / "guion.toml").write_text(GUION.format(idioma=idioma), encoding="utf-8")
        (folder / "salida" / "video.mp4").write_bytes(video)
        (folder / "salida" / "miniatura.jpg").write_bytes(b"miniatura")
        (folder / "salida" / "control.json").write_text(json.dumps({"ok": ok, "duracion": 61.0}))
        return folder


class ReviewTest(Base):
    def test_submit_requires_quality_check(self):
        self.make_project(ok=False)
        with self.assertRaises(ReviewError):
            self.book.submit("marco")

    def test_channel_follows_language(self):
        self.make_project("es-video", "es")
        self.make_project("en-video", "en")
        self.assertEqual(self.book.submit("es-video").channel, CHANNELS["es"])
        self.assertEqual(self.book.submit("en-video").channel, CHANNELS["en"])

    def test_submit_twice_does_not_duplicate_and_new_render_replaces(self):
        folder = self.make_project()
        first = self.book.submit("marco")
        self.assertEqual(self.book.submit("marco").id, first.id)
        (folder / "salida" / "video.mp4").write_bytes(b"otro montaje")
        second = self.book.submit("marco")
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(self.book.get(first.id).status, "replaced")

    def test_approval_is_tied_to_what_was_seen(self):
        folder = self.make_project()
        review = self.book.submit("marco")
        with self.assertRaises(ReviewError):
            self.book.approve(review.id, "otra-huella", "movil")
        (folder / "salida" / "miniatura.jpg").write_bytes(b"miniatura cambiada")
        with self.assertRaises(ReviewError):
            self.book.approve(review.id, review.video_sha256, "movil")
        (folder / "salida" / "miniatura.jpg").write_bytes(b"miniatura")
        approved = self.book.approve(review.id, review.video_sha256, "movil")
        self.assertEqual((approved.status, approved.decided_by), ("approved", "movil"))
        with self.assertRaises(ReviewError):
            self.book.discard(review.id, "movil")  # ya decidido

    def test_publish_check_catches_changes_after_approval(self):
        folder = self.make_project()
        review = self.book.submit("marco")
        self.book.approve(review.id, review.video_sha256, "movil")
        _, meta = self.book.ready_to_publish(review.id)
        self.assertEqual(meta["channel"], "@TuCanalEstoicismo")
        self.assertIn("Autor, Obra", meta["description"])
        text = (folder / "guion.toml").read_text().replace("Una descripción.", "Otra descripción.")
        (folder / "guion.toml").write_text(text)
        with self.assertRaises(ReviewError):
            self.book.ready_to_publish(review.id)

    def test_too_long_description_never_reaches_review(self):
        # YouTube rechaza (400) descripciones de más de 5.000 bytes: mejor saberlo al revisar que al publicar.
        self.make_project()
        with mock.patch("arthur.studio.review.full_description", return_value="é" * 2600):
            with self.assertRaises(ReviewError) as caught:
                self.book.submit("marco")
        self.assertIn("5000", str(caught.exception))

    def test_changes_need_text_and_notify_new(self):
        seen = []
        book = ReviewBook(self.store, on_new=seen.append)
        self.make_project()
        review = book.submit("marco")
        self.assertEqual([r.id for r in seen], [review.id])
        with self.assertRaises(ReviewError):
            book.request_changes(review.id, "   ", "movil")
        self.assertEqual(book.request_changes(review.id, "Más corto el final", "movil").feedback, "Más corto el final")


class WebTest(Base):
    def setUp(self):
        super().setUp()
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.key = self.devices.add("movil")
        self.web = StudioWeb(self.book, self.devices, "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.folder = self.make_project()
        self.review = self.book.submit("marco")

    def tearDown(self):
        self.web.shutdown()
        super().tearDown()

    def call(self, path, body=None, key=True, headers=None, method=None):
        req = urllib.request.Request(self.web.url + path, method=method or ("POST" if body is not None else "GET"))
        if key:
            req.add_header("Authorization", f"Bearer {self.key}")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def test_pilot_can_be_asked_for_now_but_not_twice(self):
        self.assertEqual(json.loads(self.call("/api/pilot")[2]), {"job": None})
        status, _, body = self.call("/api/pilot", {"tema": "  Séneca   y la ira "})
        self.assertEqual(status, 200)
        job = json.loads(body)["job"]
        self.assertEqual((job["status"], job["tema"]), ("queued", "Séneca y la ira"))
        stored = self.book.store.list_jobs()[0]
        self.assertEqual((stored.kind, stored.params), ("piloto", {"tema": "Séneca y la ira", "por": "movil"}))
        status, _, body = self.call("/api/pilot", {})
        self.assertEqual(status, 409)  # ya hay uno en cola: no se lanza otro
        self.assertIn(f"#{stored.id}", json.loads(body)["error"])
        self.assertEqual(self.call("/api/pilot", {"tema": "x" * 201})[0], 400)
        self.assertEqual(self.call("/api/pilot", {}, key=False)[0], 401)

    def test_page_is_public_but_data_needs_a_key(self):
        status, headers, body = self.call("/", key=False)
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.call("/api/reviews", key=False)[0], 401)
        self.assertEqual(self.call(f"/media/{self.review.id}/video", key=False)[0], 401)

    def test_list_detail_and_media(self):
        status, _, body = self.call("/api/reviews")
        data = json.loads(body)
        self.assertEqual(data["reviews"][0]["title"], "Prueba <script>alert(1)</script>")  # la página lo pinta como texto
        detail = json.loads(self.call(f"/api/reviews/{self.review.id}")[2])
        self.assertEqual(detail["video_sha256"], self.review.video_sha256)
        self.assertIn("Autor, Obra", detail["sources"])
        status, headers, body = self.call(f"/media/{self.review.id}/video", headers={"Range": "bytes=0-99"})
        self.assertEqual((status, len(body)), (206, 100))
        self.assertTrue(headers["Content-Range"].startswith("bytes 0-99/"))
        status, _, body = self.call(f"/media/{self.review.id}/video")
        self.assertEqual((status, body), (200, (self.folder / "salida" / "video.mp4").read_bytes()))
        self.assertEqual(self.call("/media/../../etc/passwd")[0], 404)
        status, headers, body = self.call(f"/media/{self.review.id}/download")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="marco-largo.mp4"')
        self.assertEqual(self.call(f"/media/{self.review.id}/download", key=False)[0], 401)

    def test_approve_through_the_page(self):
        status, _, body = self.call(f"/api/reviews/{self.review.id}/approve", {"video_sha256": "mala"})
        self.assertEqual(status, 409)
        status, _, body = self.call(f"/api/reviews/{self.review.id}/approve", {"video_sha256": self.review.video_sha256})
        self.assertEqual((status, json.loads(body)["status"]), (200, "approved"))

    def test_asking_for_changes_queues_the_remake_once(self):
        # Antes el texto se guardaba y no pasaba nada (13-10-2026): ahora pone el vídeo a rehacer en la cola.
        status, _, body = self.call(f"/api/reviews/{self.review.id}/changes", {"text": "  Quita lo  de Paulina "})
        self.assertEqual((status, json.loads(body)["status"]), (200, "changes"))
        job = self.book.store.list_jobs()[0]
        self.assertEqual((job.kind, job.params), ("cambios", {"revision": self.review.id, "proyecto": "marco",
                                                              "texto": "Quita lo de Paulina", "por": "movil"}))
        self.assertEqual(self.call(f"/api/reviews/{self.review.id}/changes", {"text": "otra"})[0], 409)
        self.assertEqual(len(self.book.store.list_jobs()), 1)

    def test_cookie_login_and_csrf(self):
        # Sin X-Arthur no se puede entrar (otra web no puede poner esa cabecera).
        self.assertEqual(self.call("/api/login", {"key": self.key}, key=False)[0], 403)
        self.assertEqual(self.call("/api/login", {"key": "mala"}, key=False, headers={"X-Arthur": "1"})[0], 401)
        status, headers, _ = self.call("/api/login", {"key": self.key}, key=False, headers={"X-Arthur": "1"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";")[0]
        # Con cookie se puede leer, pero decidir sin X-Arthur (o desde otro origen) no.
        self.assertEqual(self.call("/api/reviews", key=False, headers={"Cookie": cookie})[0], 200)
        url = f"/api/reviews/{self.review.id}/discard"
        self.assertEqual(self.call(url, {}, key=False, headers={"Cookie": cookie})[0], 401)
        self.assertEqual(self.call(url, {}, key=False, headers={"Cookie": cookie, "X-Arthur": "1",
                                                               "Origin": "http://malo.example"})[0], 401)
        status, _, body = self.call(url, {}, key=False, headers={"Cookie": cookie, "X-Arthur": "1"})
        self.assertEqual((status, json.loads(body)["status"]), (200, "discarded"))


if __name__ == "__main__":
    unittest.main()
