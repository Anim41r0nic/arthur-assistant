import unittest
import urllib.error
from datetime import date

from arthur.panel.sources import SourceError
from arthur.panel.sources.studio import sync_studio
from arthur.panel.store import PanelStore
from arthur.studio.review import ReviewBook
from arthur.studio.store import StudioStore
from arthur.tools.base import ToolError

HOY = date(2026, 10, 16)


class FakeLink:
    def __init__(self, reviews=None, error=None, payload=None):
        self.reviews, self.error, self.payload = reviews or [], error, payload

    def call(self, path):
        assert path == "/api/panel/shorts"
        if self.error:
            raise self.error
        return self.payload if self.payload is not None else {"shorts": self.reviews}


def corto(id=7, title="Marco Aurelio", status="published", kind="corto", created="2026-10-16T09:00:00+02:00",
          published=None):
    return {"id": id, "kind": kind, "title": title, "status": status, "created_at": created,
            "published_at": published if published is not None else created}


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.store = PanelStore.in_memory()
        self.addCleanup(self.store.close)

    def test_corto_publicado_hoy_crea_dos_tareas(self):
        self.assertEqual(sync_studio(FakeLink([corto()]), self.store, HOY), 2)
        t = self.store.tasks_for(HOY)
        self.assertEqual({x.title for x in t}, {"Subir a Instagram: Marco Aurelio", "Subir a TikTok: Marco Aurelio"})
        self.assertEqual({x.studio_ref for x in t}, {"review-7:instagram", "review-7:tiktok"})
        self.assertTrue(all(x.source == "studio" and x.due == "2026-10-16" for x in t))

    def test_dos_veces_no_duplica(self):
        link = FakeLink([corto()])
        sync_studio(link, self.store, HOY)
        self.assertEqual(sync_studio(link, self.store, HOY), 0)
        self.assertEqual(len(self.store.tasks_for(HOY)), 2)

    def test_ayer_vale_y_antes_de_ayer_no(self):
        link = FakeLink([corto(1, published="2026-10-15T23:00:00"),
                         corto(2, published="2026-10-14T10:00:00+02:00")])
        self.assertEqual(sync_studio(link, self.store, HOY), 2)
        self.assertEqual({x.due for x in self.store.tasks_for(HOY)}, {"2026-10-15"})

    def test_largo_o_pendiente_no_cuentan(self):
        # La ruta de Studio solo devuelve cortos publicados; una respuesta inesperada no se usa.
        link = FakeLink([corto(1, published="2026-10-14T10:00:00+02:00")])
        self.assertEqual(sync_studio(link, self.store, HOY), 0)
        self.assertEqual(self.store.tasks_for(HOY), [])

    def test_studio_caido(self):
        for err in (OSError("x"), urllib.error.URLError("x"), ToolError("x")):
            with self.assertRaisesRegex(SourceError, "Studio no disponible ahora"):
                sync_studio(FakeLink(error=err), self.store, HOY)

    def test_respuesta_rara_no_rompe(self):
        self.assertEqual(sync_studio(FakeLink(payload={}), self.store, HOY), 0)
        self.assertEqual(sync_studio(FakeLink(payload={"shorts": "x"}), self.store, HOY), 0)
        link = FakeLink([corto(1, published="mal"), corto(2, published="mal"), "x", {"kind": "corto"}])
        self.assertEqual(sync_studio(link, self.store, HOY), 0)

    def test_titulo_largo_se_recorta(self):
        sync_studio(FakeLink([corto(title="a" * 250)]), self.store, HOY)
        for t in self.store.tasks_for(HOY):
            self.assertEqual(len(t.title), 200)
            self.assertTrue(t.title.endswith("…"))

    def test_publicado_hoy_aunque_creado_hace_tres_dias(self):
        review = corto(created="2026-10-13T09:00:00+02:00", published="2026-10-16T08:00:00+02:00")
        self.assertEqual(sync_studio(FakeLink([review]), self.store, HOY), 2)
        self.assertEqual({task.due for task in self.store.tasks_for(HOY)}, {"2026-10-16"})

    def test_consulta_de_studio_no_oculta_publicados_tras_30_pendientes(self):
        studio = StudioStore.in_memory()
        self.addCleanup(studio.close)
        for i in range(31):
            studio._db.execute(
                "INSERT INTO reviews (project, kind, language, channel, status, title, "
                "video_sha256, thumb_sha256, meta_sha256, created_at, updated_at) "
                "VALUES (?, 'corto', 'es', 'canal', 'pending', 'pendiente', 'v', 't', 'm', ?, ?)",
                (f"p{i}", "2026-10-16T07:00:00", "2026-10-16T07:00:00"))
        studio._db.execute(
            "INSERT INTO reviews (project, kind, language, channel, status, title, "
            "video_sha256, thumb_sha256, meta_sha256, created_at, updated_at) "
            "VALUES ('viejo', 'corto', 'es', 'canal', 'published', 'Séneca', 'v', 't', 'm', ?, ?)",
            ("2026-10-13T07:00:00", "2026-10-16T09:00:00"))
        studio._db.commit()
        book = ReviewBook(studio)
        self.assertEqual(len(book.list()), 30)
        shorts = book.published_shorts(HOY)
        self.assertEqual([(s["title"], s["published_at"]) for s in shorts],
                         [("Séneca", "2026-10-16T09:00:00")])


if __name__ == "__main__":
    unittest.main()
