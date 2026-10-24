"""Apuntes hace de intermediario con el cerebro (notes/web.py: POST /api/cerebro/ask). Sin red: cerebro falso."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from arthur.notes.store import NotesStore
from arthur.notes.web import WEB_DIR, NotesWeb
from arthur.notes.worker import Worker
from arthur.security.devices import DeviceRegistry

from .test_panel_cerebro import FakeCerebro


class NotesCerebroTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NotesStore(Path(self.tmp.name) / "notes")
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.key = self.devices.add("movil")
        self.cerebro_key = self.devices.add("cerebro")
        self.cerebro = FakeCerebro()
        worker = Worker(self.store, {}, scans_enabled=False)
        self.web = NotesWeb(self.store, self.devices, worker, "127.0.0.1", 0, cerebro=self.cerebro)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def call(self, body, *, key=None, headers=None):
        req = urllib.request.Request(self.web.url + "/api/cerebro/ask", method="POST", data=json.dumps(body).encode())
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_apuntes_reenvia(self):
        cookie = {"Cookie": f"apuntes_llave={self.key}"}
        code, data = self.call({"q": "¿qué es un límite?"}, headers={**cookie, "X-Arthur": "1"})
        self.assertEqual(code, 200)
        self.assertEqual(data["citations"][0]["url"], "http://a/#n1")
        self.assertNotIn("extra", data)
        self.assertEqual(self.cerebro.calls, [("/api/ask", {"q": "¿qué es un límite?"})])

    def test_misma_comprobacion_que_sus_otras_rutas(self):
        cookie = {"Cookie": f"apuntes_llave={self.key}"}
        self.assertEqual(self.call({"q": "hola"})[0], 401)
        self.assertEqual(self.call({"q": "hola"}, headers=cookie)[0], 401)  # sin X-Arthur (como sus otras rutas)
        self.assertEqual(self.call({"q": "hola"}, headers={**cookie, "X-Arthur": "1",
                                                            "Origin": "https://mala.example"})[0], 401)
        self.assertEqual(self.cerebro.calls, [])

    def test_el_cerebro_no_se_pregunta_a_si_mismo(self):
        self.assertEqual(self.call({"q": "hola"}, key=self.cerebro_key)[0], 403)
        self.assertEqual(self.cerebro.calls, [])

    def test_caido_o_sin_configurar(self):
        self.web.cerebro = FakeCerebro(fail=True)
        self.assertEqual(self.call({"q": "hola"}, key=self.key), (503, {"error": "El cerebro no está disponible ahora"}))
        self.web.cerebro = None
        self.assertEqual(self.call({"q": "hola"}, key=self.key)[0], 503)
        self.assertEqual(self.call({"q": ""}, key=self.key)[0], 400)

    def test_paginas_tienen_boton(self):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        js = (WEB_DIR / "apuntes.js").read_text(encoding="utf-8")
        self.assertIn("Preguntar a mi cerebro", html)
        for ident in ('id="show-brain"', 'id="brain-form"', 'id="brain-q"', 'id="brain-answer"',
                      'id="brain-sources"'):
            self.assertIn(ident, html)
        self.assertIn("/api/cerebro/ask", js)
        self.assertIn('rel = "noopener noreferrer"', js)
        self.assertNotIn(".innerHTML", js)  # el comentario de arriba lo nombra; usarlo, nunca
        self.assertIn('$("brain-answer").textContent', js)


if __name__ == "__main__":
    unittest.main()
