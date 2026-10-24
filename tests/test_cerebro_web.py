"""API del cerebro (cerebro/web.py) y reloj de reindexado (cerebro/scheduler.py).

Servidor de verdad en 127.0.0.1:0; la respuesta, el índice y las fuentes son falsos o temporales. Sin red.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arthur.cerebro.budget import Budget
from arthur.cerebro.chunks import Doc
from arthur.cerebro.index import Index
from arthur.cerebro.scheduler import Reindexer, Source
from arthur.cerebro.web import BAD_KEY_DELAY, CerebroWeb, build_estado
from arthur.security.devices import DeviceRegistry
from arthur.tools.base import ToolError

UTC_AHORA = datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc)


def doc(ref, text, source="obsidian:cerebro"):
    return Doc(source=source, ref=ref, title=ref, subject="", changed_at="2026-10-17T20:05:00+02:00", text=text,
               links=())


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.keys = {name: self.devices.add(name) for name in ("core", "panel", "apuntes", "portatil", "studio")}
        self.preguntas, self.diarios, self.sleeps = [], [], []
        self.web = CerebroWeb(self.devices, self.ask, lambda: {"trozos": 3}, self.diario,
                              host="127.0.0.1", port=0, sleep=self.sleeps.append)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.tmp.cleanup()

    def ask(self, q, sources):
        self.preguntas.append((q, sources))
        return {"answer": "Se usa L'Hôpital [n1]", "engine": "workers-ai", "found": True,
                "citations": [{"id": "n1", "title": "Derivadas", "source": "apuntes", "url": "http://a/#n1"}]}

    def diario(self, material):
        self.diarios.append(material)
        return {"markdown": "[[2026-10-17]]\n\n## Hecho\n", "engine": "workers-ai"}

    def call(self, path, body=None, *, device="core", key=None, x_arthur=True):
        req = urllib.request.Request(self.web.url + path, method="POST" if body is not None else "GET")
        token = key if key is not None else (self.keys[device] if device else None)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
            if x_arthur:
                req.add_header("X-Arthur", "1")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_sin_llave_401(self):
        self.assertEqual(self.call("/api/estado", device=None)[0], 401)
        self.assertEqual(self.call("/api/ask", {"q": "hola"}, device=None)[0], 401)
        self.assertEqual(self.call("/api/estado", key="arthur_no-vale")[0], 401)
        self.assertEqual(self.sleeps, [BAD_KEY_DELAY])  # la llave mala espera; la ausente no
        self.assertEqual(self.preguntas, [])

    def test_llave_de_otro_servicio_403(self):
        # Un aparato que no es de los del cerebro (p. ej. Studio) no entra aunque su llave esté en el registro.
        self.assertEqual(self.call("/api/estado", device="studio")[0], 403)
        self.assertEqual(self.call("/api/ask", {"q": "hola"}, device="studio")[0], 403)
        # La nota diaria: solo el portátil (directo) o el panel (que se la reenvía).
        self.assertEqual(self.call("/api/diario", {"day": "2026-10-17"}, device="core")[0], 403)
        self.assertEqual(self.call("/api/ask", {"q": "hola"}, device="portatil")[0], 403)
        self.assertEqual(self.preguntas, [])

    def test_post_sin_x_arthur_403(self):
        self.assertEqual(self.call("/api/ask", {"q": "hola"}, x_arthur=False)[0], 403)
        self.assertEqual(self.preguntas, [])

    def test_pregunta_vacia_o_larga_400(self):
        for body in ({"q": ""}, {"q": "   "}, {"q": "x" * 501}, {"q": 3}, {}, {"q": "hola", "fuentes": "apuntes"},
                     {"q": "hola", "fuentes": ["otra"]}):
            self.assertEqual(self.call("/api/ask", body)[0], 400, body)
        self.assertEqual(self.preguntas, [])

    def test_ask_devuelve_citas(self):
        code, data = self.call("/api/ask", {"q": "  ¿cómo se usa L'Hôpital? ", "fuentes": ["apuntes", "obsidian"]},
                               device="panel")
        self.assertEqual(code, 200)
        self.assertEqual(data["citations"][0]["id"], "n1")
        self.assertEqual(self.preguntas, [("¿cómo se usa L'Hôpital?", ("apuntes", "obsidian"))])
        self.assertEqual(self.call("/api/ask", {"q": "hola"}, device="apuntes")[0], 200)
        self.assertEqual(self.preguntas[-1], ("hola", None))

    def test_ask_falla_503(self):
        def roto(q, sources):
            raise RuntimeError("se ha roto")
        self.web.ask_fn = roto
        code, data = self.call("/api/ask", {"q": "hola"})
        self.assertEqual(code, 503)
        self.assertNotIn("roto", data["error"])

    def test_diario(self):
        body = {"day": "2026-10-17", "registro": "- 2026-10-17 · algo", "memoria": [], "commits": ["abc Algo"]}
        code, data = self.call("/api/diario", body, device="portatil")
        self.assertEqual(code, 200)
        self.assertTrue(data["markdown"].startswith("[[2026-10-17]]"))
        self.assertEqual(self.call("/api/diario", body, device="panel")[0], 200)
        self.assertEqual(self.call("/api/diario", {"day": "ayer"}, device="portatil")[0], 400)
        self.assertEqual(len(self.diarios), 2)

    def test_estado_y_rutas(self):
        self.assertEqual(self.call("/api/estado"), (200, {"trozos": 3}))
        self.assertEqual(self.call("/api/nada")[0], 404)
        self.assertEqual(self.call("/", device=None)[0], 404)  # sin página propia


class EstadoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.index = Index(root / "indice.sqlite3")
        self.budget = Budget(root / "presupuesto.json", daily=3000, now=lambda: UTC_AHORA)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def test_estado(self):
        self.budget.spend("ask", 12.5)

        def apuntes(since):
            raise ToolError("Apuntes no contesta (Connection refused): ¿está en marcha?")

        sources = [Source("obsidian:cerebro", lambda since: ([doc("a.md", "# A\nUna nota.")], None)),
                   Source("apuntes", apuntes)]
        rx = Reindexer(self.index, lambda: sources, now=lambda: UTC_AHORA)
        rx.run_once()
        estado = build_estado(self.index, self.budget, rx)
        self.assertEqual(estado["trozos"], 1)
        self.assertEqual(estado["pendientes"], 1)  # sin embedder: pendiente de significado
        self.assertEqual(estado["gastado_hoy"], 12.5)
        self.assertEqual(estado["presupuesto"], 3000)
        self.assertEqual(estado["ultima_pasada"], UTC_AHORA.isoformat())
        self.assertIsNone(estado["fuentes"]["obsidian:cerebro"]["error"])
        self.assertEqual(estado["fuentes"]["obsidian:cerebro"]["trozos"], 1)
        self.assertIn("Apuntes no contesta", estado["fuentes"]["apuntes"]["error"])
        self.assertIsNone(estado["fuentes"]["apuntes"]["ultima_ok"])
        json.dumps(estado)  # se puede mandar tal cual


class ReindexerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = Index(Path(self.tmp.name) / "indice.sqlite3")
        self.now = UTC_AHORA

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def test_reindexer_fuente_caida(self):
        def apuntes(since):
            raise ToolError("Apuntes no contesta")

        sources = [Source("apuntes", apuntes),
                   Source("obsidian:cerebro", lambda since: ([doc("a.md", "# A\nUna nota.")], None))]
        rx = Reindexer(self.index, lambda: sources, now=lambda: self.now)
        rx.run_once()
        self.assertEqual(self.index.stats()["fuentes"]["obsidian:cerebro"]["documentos"], 1)
        state = rx.state()
        self.assertIn("Apuntes no contesta", state["fuentes"]["apuntes"]["error"])
        self.assertIsNone(state["fuentes"]["obsidian:cerebro"]["error"])

    def test_since_solo_tras_exito(self):
        vistos, falla = [], [True]

        def apuntes(since):
            vistos.append(since)
            if falla[0]:
                raise ToolError("caído")
            return [doc("apunte:1", "Texto", source="apuntes")], {"apunte:1"}

        rx = Reindexer(self.index, lambda: [Source("apuntes", apuntes)], now=lambda: self.now)
        rx.run_once()
        falla[0] = False
        rx.run_once()
        primera_ok = self.now
        self.now += timedelta(minutes=15)
        rx.run_once()
        # Mientras falla se sigue pidiendo todo; tras la primera buena, solo lo cambiado desde que EMPEZÓ esa pasada.
        self.assertEqual(vistos, [None, None, primera_ok.isoformat()])
        self.assertEqual(rx.state()["fuentes"]["apuntes"]["ultima_ok"], self.now.isoformat())

    def test_embeddings_tras_sync(self):
        class Falso:
            def __init__(self):
                self.textos = []

            def embed(self, texts, *, kind="embed"):
                self.textos += texts
                return [[1.0] + [0.0] * 1023 for _ in texts]

        emb = Falso()
        rx = Reindexer(self.index, lambda: [Source("obsidian:cerebro", lambda s: ([doc("a.md", "Hola")], None))],
                       embedder=lambda: emb, now=lambda: self.now)
        rx.run_once()
        self.assertEqual(self.index.stats()["pendientes"], 0)
        self.assertEqual(len(emb.textos), 1)

    def test_hilo_arranca_y_para(self):
        hechas = threading.Event()
        rx = Reindexer(self.index, lambda: hechas.set() or [], every=3600, now=lambda: self.now)
        rx.start()
        self.assertTrue(hechas.wait(5))  # la primera pasada es al arrancar, sin esperar 15 min
        rx.stop()
        self.assertFalse(rx._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
