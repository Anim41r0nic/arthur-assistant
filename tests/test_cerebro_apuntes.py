"""El cerebro pide a Apuntes su texto (cerebro/sources/apuntes.py), contra un Apuntes de verdad en un puerto suelto."""

import tempfile
import threading
import unittest
from pathlib import Path

from arthur.cerebro.chunks import Doc
from arthur.cerebro.sources import apuntes
from arthur.notes.store import NotesStore
from arthur.notes.web import NotesWeb
from arthur.notes.worker import Worker
from arthur.security.devices import DeviceRegistry
from arthur.tools.base import ToolError
from arthur.tools.services import ServiceLink

from .test_notes_export import ready


class FakeLink:
    """Apunta qué rutas se piden y contesta lo que se le diga."""

    name = "Apuntes"

    def __init__(self, answer):
        self.answer = answer
        self.paths = []

    def call(self, path, body=None):
        self.paths.append(path)
        return self.answer


class ApuntesSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NotesStore(Path(self.tmp.name) / "notes")
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        worker = Worker(self.store, {}, sleep=lambda s: None, scans_enabled=False)
        self.web = NotesWeb(self.store, self.devices, worker, "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.link = ServiceLink("Apuntes", self.web.url, self.devices.add("cerebro"))

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def test_fetch_convierte_en_docs(self):
        calc = self.store.create_folder(0, "Cálculo")
        tema = self.store.create_folder(calc.id, "Tema 2")
        note = ready(self.store, "Límites", "# Límite\n\nUna sucesión…\n", tema.id)
        suelto = ready(self.store, "Suelto", "en la principal", changed="2026-10-16T09:00:00")

        docs = apuntes.fetch(self.link, None)
        self.assertEqual(len(docs), 2)
        doc = docs[0]
        self.assertIsInstance(doc, Doc)
        self.assertEqual((doc.source, doc.ref, doc.title, doc.subject), ("apuntes", f"apunte:{note.id}", "Límites",
                                                                          "Cálculo"))
        self.assertIn("Una sucesión", doc.text)
        self.assertEqual(doc.links, ())
        self.assertEqual(doc.changed_at, self.store.export_since(None)[0]["changed_at"])
        self.assertEqual(docs[1].subject, "")  # en la carpeta principal no hay asignatura
        self.assertEqual(apuntes.ids(self.link), {f"apunte:{note.id}", f"apunte:{suelto.id}"})

        # Solo lo cambiado desde la última pasada (el «+» de la zona tiene que llegar bien por la URL).
        self.assertEqual([d.ref for d in apuntes.fetch(self.link, docs[1].changed_at)], [f"apunte:{suelto.id}"])

    def test_llave_de_otro_aparato(self):
        movil = ServiceLink("Apuntes", self.web.url, self.devices.add("movil"))
        with self.assertRaises(ToolError):
            apuntes.fetch(movil, None)

    def test_respuesta_rara(self):
        # Si Apuntes contesta algo que no se esperaba, se dice: tomarlo por «no hay nada» borraría el índice.
        for answer in ({}, {"notes": "x"}, {"notes": [{"id": 1}]}):
            with self.assertRaises(ToolError):
                apuntes.fetch(FakeLink(answer), None)
        for answer in ({}, {"ids": [1, "x"]}, {"ids": None}):
            with self.assertRaises(ToolError):
                apuntes.ids(FakeLink(answer))


if __name__ == "__main__":
    unittest.main()
