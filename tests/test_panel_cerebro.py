"""El panel hace de intermediario con el cerebro (panel/web.py: /api/cerebro/ask y /api/cerebro/diario).

Servidor del panel de verdad en 127.0.0.1:0; el cerebro es un ServiceLink falso que apunta lo que le llega.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.panel.store import PanelStore
from arthur.panel.web import WEB_DIR, PanelWeb
from arthur.security.devices import DeviceRegistry
from arthur.tools.base import ToolError

AHORA = datetime(2026, 10, 17, 9, 0, tzinfo=ZoneInfo("Europe/Madrid"))
RESPUESTA = {"answer": "Se usa L'Hôpital [n1]", "engine": "workers-ai", "found": True, "extra": "fuera",
             "citations": [{"id": "n1", "title": "Derivadas", "source": "apuntes", "url": "http://a/#n1", "x": 1},
                           "basura", {"id": 3}]}


class FakeCerebro:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer or RESPUESTA, fail, []

    def call(self, path, body=None):
        self.calls.append((path, body))
        if self.fail:
            raise ToolError("Cerebro no contesta (Connection refused): ¿está en marcha?")
        return self.answer


class PanelCerebroTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PanelStore.in_memory(now=lambda: AHORA)
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.key = self.devices.add("movil")
        self.portatil = self.devices.add("portatil")
        self.cerebro = FakeCerebro()
        self.start(self.cerebro)

    def start(self, cerebro):
        self.web = PanelWeb(self.store, self.devices, lambda: {}, None, lambda: {}, host="127.0.0.1", port=0,
                            now=lambda: AHORA, sleep=lambda s: None, cerebro=cerebro)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def call(self, path, body, *, key=None, headers=None):
        req = urllib.request.Request(self.web.url + path, method="POST", data=json.dumps(body).encode())
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

    def test_panel_reenvia(self):
        code, data = self.call("/api/cerebro/ask", {"q": "  ¿L'Hôpital?  "}, key=self.key)
        self.assertEqual(code, 200)
        self.assertEqual(self.cerebro.calls, [("/api/ask", {"q": "¿L'Hôpital?"})])
        # Solo lo que la página necesita, con los tipos esperados: lo raro del cerebro no pasa al navegador.
        self.assertEqual(data, {"answer": "Se usa L'Hôpital [n1]", "engine": "workers-ai", "found": True,
                                "citations": [{"id": "n1", "title": "Derivadas", "source": "apuntes",
                                               "url": "http://a/#n1"}]})

    def test_misma_comprobacion_que_sus_otras_rutas(self):
        cookie = {"Cookie": f"panel_llave={self.key}"}
        self.assertEqual(self.call("/api/cerebro/ask", {"q": "hola"})[0], 401)
        self.assertEqual(self.call("/api/cerebro/ask", {"q": "hola"}, headers=cookie)[0], 403)  # sin X-Arthur
        self.assertEqual(self.call("/api/cerebro/ask", {"q": "hola"},
                                   headers={**cookie, "X-Arthur": "1", "Origin": "https://mala.example"})[0], 403)
        self.assertEqual(self.cerebro.calls, [])
        self.assertEqual(self.call("/api/cerebro/ask", {"q": "hola"}, headers={**cookie, "X-Arthur": "1"})[0], 200)

    def test_pregunta_mala_400(self):
        for body in ({"q": ""}, {"q": "x" * 501}, {"q": 5}, {}):
            self.assertEqual(self.call("/api/cerebro/ask", body, key=self.key)[0], 400, body)
        self.assertEqual(self.cerebro.calls, [])

    def test_cerebro_caido_503(self):
        self.web.cerebro = FakeCerebro(fail=True)
        code, data = self.call("/api/cerebro/ask", {"q": "hola"}, key=self.key)
        self.assertEqual(code, 503)
        self.assertEqual(data["error"], "El cerebro no está disponible ahora")
        self.web.cerebro = None
        self.assertEqual(self.call("/api/cerebro/ask", {"q": "hola"}, key=self.key)[0], 503)

    def test_diario_solo_el_portatil(self):
        self.cerebro.answer = {"markdown": "[[2026-10-17]]\n\n## Hecho\n", "engine": "local"}
        body = {"day": "2026-10-17", "registro": "x" * 70_000}  # el panel no corta: lo comprueba el cerebro
        self.assertEqual(self.call("/api/cerebro/diario", body, key=self.key)[0], 403)
        self.assertEqual(self.cerebro.calls, [])
        code, data = self.call("/api/cerebro/diario", body, key=self.portatil)
        self.assertEqual((code, data["engine"]), (200, "local"))
        self.assertEqual(self.cerebro.calls, [("/api/diario", body)])

    def test_paginas_tienen_caja(self):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        js = (WEB_DIR / "panel.js").read_text(encoding="utf-8")
        self.assertIn("Pregunta a tu cerebro", html)
        for ident in ('id="brain-form"', 'id="brain-q"', 'id="brain-answer"', 'id="brain-sources"'):
            self.assertIn(ident, html)
        self.assertIn('maxlength="500"', html)
        self.assertIn("/api/cerebro/ask", js)
        self.assertIn('rel = "noopener noreferrer"', js)
        self.assertNotIn(".innerHTML", js)  # el comentario de arriba lo nombra; usarlo, nunca
        self.assertIn('$("brain-answer").textContent', js)


if __name__ == "__main__":
    unittest.main()
