"""Apuntes exporta su texto al cerebro: GET /api/export, solo con la llave del aparato «cerebro».

El cerebro no toca la base de datos de Apuntes: le pide por la API el texto de lo ya leído («listo»).
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from arthur.notes.store import NotesStore
from arthur.notes.web import NotesWeb
from arthur.notes.worker import Worker
from arthur.security.devices import DeviceRegistry


def ready(store, title, markdown, folder_id=0, changed="2026-10-15T10:00:00"):
    """Una nota ya leída, con la fecha de cambio que diga el test (sin esperar al reloj)."""
    note = store.create(title, "local", folder_id=folder_id)
    store.update(note.id, status="listo", markdown=markdown)
    store._q("UPDATE notes SET updated_at = ? WHERE id = ?", (changed, note.id))
    return note


class ExportStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NotesStore(Path(self.tmp.name) / "notes")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_export_solo_listos(self):
        calc = self.store.create_folder(0, "Cálculo")
        tema = self.store.create_folder(calc.id, "Tema 2")
        listo = ready(self.store, "Límites", "# Límite\n\n$$\\lim a_n$$\n", tema.id)
        self.store.create("A medio subir", "local")                       # borrador
        cola = self.store.create("En cola", "local")
        self.store.update(cola.id, status="en_cola")
        roto = self.store.create("Roto", "local")
        self.store.update(roto.id, status="error", markdown="medio texto")

        out = self.store.export_since(None)
        self.assertEqual([n["id"] for n in out], [listo.id])
        note = out[0]
        self.assertEqual(set(note), {"id", "title", "path", "changed_at", "text"})
        self.assertEqual((note["title"], note["path"]), ("Límites", ["Cálculo", "Tema 2"]))
        self.assertIn("$$\\lim a_n$$", note["text"])
        # Con zona: el cerebro compara fechas de varios sitios y una hora sin zona es ambigua.
        self.assertIsNotNone(datetime.fromisoformat(note["changed_at"]).tzinfo)
        self.assertEqual(self.store.ready_ids(), [listo.id])

    def test_texto_de_las_paginas_si_no_hay_markdown(self):
        note = self.store.create("Fotos", "local")
        self.store.set_pages(note.id, [Path("001.jpg"), Path("002.jpg")])
        self.store.set_page_text(note.id, 2, "segunda")
        self.store.set_page_text(note.id, 1, "primera")
        self.store.update(note.id, status="listo", markdown="")
        self.assertEqual(self.store.export_since(None)[0]["text"], "primera\n\nsegunda")

    def test_export_desde(self):
        viejo = ready(self.store, "Viejo", "a", changed="2026-10-10T10:00:00")
        nuevo = ready(self.store, "Nuevo", "b", changed="2026-10-15T10:00:00")
        self.assertEqual([n["id"] for n in self.store.export_since("2026-10-12T00:00:00")], [nuevo.id])
        # La fecha que devolvió una exportación vale como «desde» de la siguiente (con zona): sale otra vez
        # lo cambiado justo en ese segundo, mejor repetir uno que perderlo.
        since = self.store.export_since(None)[0]["changed_at"]
        self.assertEqual([n["id"] for n in self.store.export_since(since)], [viejo.id, nuevo.id])
        self.assertEqual(self.store.export_since("2026-10-20T00:00:00+00:00"), [])
        with self.assertRaises(ValueError):
            self.store.export_since("ayer")


class ExportWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NotesStore(Path(self.tmp.name) / "notes")
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.cerebro = self.devices.add("cerebro")
        self.movil = self.devices.add("movil")
        worker = Worker(self.store, {}, sleep=lambda s: None, scans_enabled=False)  # no se arranca: no lee nada
        self.web = NotesWeb(self.store, self.devices, worker, "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def get(self, path, key=None):
        req = urllib.request.Request(self.web.url + path)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_export_llave_cerebro(self):
        ready(self.store, "Límites", "texto")
        self.assertEqual(self.get("/api/export")[0], 401)
        self.assertEqual(self.get("/api/export", self.movil)[0], 403)  # el móvil no necesita el texto en bloque
        self.assertEqual(self.get("/api/export?ids=1", self.movil)[0], 403)
        status, data = self.get("/api/export", self.cerebro)
        self.assertEqual(status, 200)
        self.assertEqual([n["title"] for n in data["notes"]], ["Límites"])

    def send(self, path, key, body=None, method=None):
        req = urllib.request.Request(self.web.url + path, method=method or ("POST" if body is not None else "GET"))
        req.add_header("Authorization", f"Bearer {key}")
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Arthur", "1")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            with e:
                return e.code

    def test_llave_cerebro_solo_exporta(self):
        # Defensa en profundidad: si se filtra la llave del cerebro, con ella solo se lee la exportación (lo único que
        # el cerebro pide a Apuntes); no se crean, mueven ni borran apuntes ni se descargan sus ficheros.
        note = ready(self.store, "Límites", "texto")
        folder = self.store.create_folder(0, "Cálculo")
        for path in ("/api/notes", "/api/folders", f"/api/notes/{note.id}", f"/files/{note.id}/texto",
                     f"/files/{note.id}/original/1", "/api/ask/notes", "/api/repaso/hoy", "/api/otra"):
            self.assertEqual(self.send(path, self.cerebro), 403, path)
        for path, body in (("/api/notes", {"title": "colada"}), (f"/api/notes/{note.id}/delete", {}),
                           (f"/api/notes/{note.id}/move", {"folder": folder.id}), ("/api/folders", {"name": "x"}),
                           (f"/api/folders/{folder.id}/delete", {}), ("/api/ask", {"notes": []}),
                           ("/api/cerebro/ask", {"q": "hola"}), ("/api/repaso/respuesta", {"question_id": 1})):
            self.assertEqual(self.send(path, self.cerebro, body), 403, path)
        self.assertEqual([n.title for n, _ in self.store.ready_under(0)], ["Límites"])
        self.assertIsNotNone(self.store.get(note.id))
        self.assertEqual(self.send("/api/export", self.cerebro), 200)
        self.assertEqual(self.send("/api/export?ids=1", self.cerebro), 200)
        # El móvil sigue pudiendo todo lo suyo
        self.assertEqual(self.send("/api/notes", self.movil), 200)
        self.assertEqual(self.send(f"/api/notes/{note.id}", self.movil), 200)

    def test_export_desde_y_ids(self):
        viejo = ready(self.store, "Viejo", "a", changed="2026-10-10T10:00:00")
        nuevo = ready(self.store, "Nuevo", "b", changed="2026-10-15T10:00:00")
        since = urllib.parse.urlencode({"since": datetime(2026, 10, 12).astimezone().isoformat()})
        status, data = self.get(f"/api/export?{since}", self.cerebro)
        self.assertEqual((status, [n["id"] for n in data["notes"]]), (200, [nuevo.id]))
        self.assertEqual(self.get("/api/export?since=ayer", self.cerebro)[0], 400)
        self.assertEqual(self.get("/api/export?ids=1", self.cerebro), (200, {"ids": [viejo.id, nuevo.id]}))


if __name__ == "__main__":
    unittest.main()
