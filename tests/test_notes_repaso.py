"""La pestaña Repaso de Apuntes: el reenvío /api/repaso/* al cerebro (notes/web.py + cerebro/relay.py), KaTeX
servido desde la propia página y la estructura de la pestaña. Sin red: cerebro de verdad en 127.0.0.1:0 con el
almacén en memoria, o un cerebro falso que apunta lo que le llega."""

import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.cerebro.repaso.service import Repaso
from arthur.cerebro.repaso.store import RepasoStore
from arthur.cerebro.web import CerebroWeb
from arthur.notes.store import NotesStore
from arthur.notes.web import WEB_DIR, NotesWeb
from arthur.notes.worker import Worker
from arthur.security.devices import DeviceRegistry
from arthur.tools.base import ToolError
from arthur.tools.services import ServiceLink

AHORA = datetime(2026, 10, 17, 19, 0, tzinfo=ZoneInfo("Europe/Madrid"))


class FakeCerebro:
    """Contesta lo que se le diga por ruta (sin la consulta) y apunta cada llamada."""

    def __init__(self, answers=None, fail=None):
        self.answers, self.fail, self.calls = answers or {}, fail, []

    def call(self, path, body=None, **kwargs):
        self.calls.append((path, body))
        if self.fail is not None:
            raise self.fail
        return self.answers.get(path.split("?")[0], {})


def http_error(status, reason):
    e = ToolError(f"Cerebro: {reason}")
    e.status, e.reason = status, reason
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = NotesStore(root / "notes")
        self.devices = DeviceRegistry(root / "devices.json")
        self.key = self.devices.add("movil")
        self.cerebro_key = self.devices.add("cerebro")
        self.web = NotesWeb(self.store, self.devices, Worker(self.store, {}, scans_enabled=False), "127.0.0.1", 0,
                            cerebro=self.make_cerebro(root))
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.cookie = {"Cookie": f"apuntes_llave={self.key}"}

    def make_cerebro(self, root):
        self.cerebro = FakeCerebro()
        return self.cerebro

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def call(self, path, body=None, *, headers=None, raw=False):
        req = urllib.request.Request(self.web.url + path, method="POST" if body is not None else "GET")
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = r.read()
                return (r.status, r.headers, data) if raw else (r.status, json.loads(data))
        except urllib.error.HTTPError as e:
            with e:
                data = e.read()
                return (e.code, e.headers, data) if raw else (e.code, json.loads(data or b"{}"))

    def get(self, path):
        return self.call(path, headers=self.cookie)

    def post(self, path, body):
        return self.call(path, body, headers={**self.cookie, "X-Arthur": "1"})


class ReenvioConCerebroDeVerdadTest(Base):
    """Apuntes → ServiceLink → cerebro de verdad: el navegador nunca ve la llave ni la respuesta antes de tiempo."""

    def make_cerebro(self, root):
        self.repaso_store = RepasoStore.in_memory(now=lambda: AHORA)
        repaso = Repaso(self.repaso_store, root / "ajustes.toml", now=lambda: AHORA,
                        link_fn=lambda chunk_id: f"https://apuntes/#n{chunk_id}", subjects_fn=lambda: ["Cálculo"])
        repaso.save_settings({"asignaturas": ["Cálculo"]})
        self.qid = self.repaso_store.add_question({
            "chunk_id": 7, "asignatura": "Cálculo", "carpeta": ["Cálculo", "Derivadas"], "tipo": "formula",
            "enunciado": r"$\frac{d(x^2)}{dx} = ___ x$", "respuesta": "2", "explicacion": "Regla de la potencia."})
        cerebro_devices = DeviceRegistry(root / "cerebro-devices.json")
        self.cerebro_web = CerebroWeb(cerebro_devices, lambda q, s: {}, lambda: {}, None, repaso=repaso,
                                      host="127.0.0.1", port=0, sleep=lambda s: None)
        threading.Thread(target=self.cerebro_web.serve_forever, daemon=True).start()
        return ServiceLink("Cerebro", self.cerebro_web.url, cerebro_devices.add("apuntes"))

    def tearDown(self):
        super().tearDown()
        self.cerebro_web.shutdown()
        self.repaso_store.close()

    def test_reenvia_con_llave(self):
        code, hoy = self.get("/api/repaso/hoy")
        self.assertEqual(code, 200)
        self.assertEqual([q["id"] for q in hoy["preguntas"]], [self.qid])
        self.assertEqual((hoy["total"], hoy["hechas"], hoy["hecha"], hoy["racha"]), (1, 0, False, 0))
        q = hoy["preguntas"][0]
        self.assertEqual(q["carpeta"], ["Cálculo", "Derivadas"])
        self.assertNotIn("respuesta", q)
        self.assertNotIn("explicacion", q)

        code, data = self.post("/api/repaso/respuesta", {"question_id": self.qid, "respuesta": "3"})
        self.assertEqual(code, 200)
        self.assertEqual((data["correcto"], data["respuesta"], data["explicacion"]), (False, "2", "Regla de la potencia."))
        self.assertEqual(data["enlace"], "https://apuntes/#n7")
        # Fallada: vuelve al final de la sesión, que aún no está hecha (y Apuntes deja pasar «otra_vez»)
        self.assertFalse(data["hecha"])
        self.assertIs(data["otra_vez"], True)

        code, data = self.get("/api/repaso/libre")
        self.assertEqual((code, data), (200, {"carpetas": [{"carpeta": "Cálculo", "preguntas": 1},
                                                           {"carpeta": "Cálculo/Derivadas", "preguntas": 1}]}))
        code, data = self.get("/api/repaso/libre?carpeta=" + urllib.parse.quote("Cálculo/Derivadas"))
        self.assertEqual((code, data["total"], data["carpeta"]), (200, 1, "Cálculo/Derivadas"))

        code, data = self.get("/api/repaso/resumen")
        self.assertEqual((code, data["racha"], data["aciertos_7d"], data["pendientes"]), (200, 0, 0.0, 1))
        code, data = self.post("/api/repaso/ajustes", {"aviso": "21:30"})
        self.assertEqual((code, data["aviso"]), (200, "21:30"))
        self.assertEqual(self.get("/api/repaso/ajustes")[1]["disponibles"], ["Cálculo"])

        # Los errores del cerebro llegan con su código y su motivo (se pueden enseñar)
        code, data = self.post("/api/repaso/ajustes", {"aviso": "25:00"})
        self.assertEqual(code, 400)
        self.assertIn("HH:MM", data["error"])
        self.assertEqual(self.post("/api/repaso/respuesta", {"question_id": 999, "respuesta": "x"})[0], 404)
        code, data = self.post("/api/repaso/mal", {"question_id": self.qid, "motivo": "El hueco no se entiende"})
        self.assertEqual((code, data), (200, {"question_id": self.qid, "retirada": True}))
        self.assertEqual(self.post("/api/repaso/respuesta", {"question_id": self.qid, "respuesta": "2"})[0], 409)


class ReenvioTest(Base):
    def test_misma_comprobacion_que_sus_otras_rutas(self):
        self.assertEqual(self.call("/api/repaso/hoy")[0], 401)                       # sin llave
        body = {"question_id": 1, "respuesta": "x"}
        self.assertEqual(self.call("/api/repaso/respuesta", body, headers=self.cookie)[0], 401)  # sin X-Arthur
        self.assertEqual(self.call("/api/repaso/respuesta", body, headers={
            **self.cookie, "X-Arthur": "1", "Origin": "https://mala.example"})[0], 401)
        # La llave del cerebro solo sirve para exportar: no repasa (ni se llama a sí mismo)
        bearer = {"Authorization": f"Bearer {self.cerebro_key}"}
        self.assertEqual(self.call("/api/repaso/hoy", headers=bearer)[0], 403)
        self.assertEqual(self.call("/api/repaso/respuesta", body, headers={**bearer, "X-Arthur": "1"})[0], 403)
        self.assertEqual(self.cerebro.calls, [])

    def test_cuerpo_del_repaso_hasta_8_kib(self):
        # El cerebro solo admite 8 KiB: Apuntes no lee (ni guarda en memoria) 4 MB para reenviarlos y que los rechace
        grande = {"question_id": 1, "respuesta": "x" * (8 * 1024)}
        self.assertEqual(self.post("/api/repaso/respuesta", grande)[0], 413)
        self.assertEqual(self.cerebro.calls, [])
        self.assertEqual(self.post("/api/repaso/respuesta", {"question_id": 1, "respuesta": "x" * 7000})[0], 200)
        self.assertEqual(len(self.cerebro.calls), 1)

    def test_solo_las_rutas_del_repaso_y_sus_campos(self):
        self.assertEqual(self.get("/api/repaso/otra")[0], 404)
        self.assertEqual(self.post("/api/repaso/hoy", {})[0], 404)
        self.assertEqual(self.get("/api/repaso/mal")[0], 404)
        self.assertEqual(self.cerebro.calls, [])
        # Al cerebro solo le llegan los campos de cada ruta
        self.post("/api/repaso/respuesta", {"question_id": 3, "respuesta": "x", "nota": 4, "otra": 1})
        self.post("/api/repaso/mal", {"question_id": 3, "motivo": "mal", "x": 2})
        self.post("/api/repaso/ajustes", {"asignaturas": ["MTP"], "aviso": "20:00", "z": 1})
        self.assertEqual(self.cerebro.calls, [
            ("/api/repaso/respuesta", {"question_id": 3, "respuesta": "x", "nota": 4}),
            ("/api/repaso/mal", {"question_id": 3, "motivo": "mal"}),
            ("/api/repaso/ajustes", {"asignaturas": ["MTP"], "aviso": "20:00"})])
        # La carpeta va codificada y comprobada antes de gastar una llamada
        self.cerebro.calls.clear()
        self.get("/api/repaso/libre?carpeta=" + urllib.parse.quote("Cálculo/Tema 2&x=1"))
        self.assertEqual(self.cerebro.calls, [("/api/repaso/libre?carpeta=C%C3%A1lculo/Tema%202%26x%3D1", None)])
        self.assertEqual(self.get("/api/repaso/libre?carpeta=" + "x" * 2000)[0], 400)
        self.assertEqual(self.post("/api/repaso/respuesta", {"question_id": "3"})[0], 400)
        self.assertEqual(len(self.cerebro.calls), 1)

    def test_respuesta_no_viaja_antes(self):
        # Aunque el cerebro mandara de más, al navegador solo llega lo que pinta la página
        q = {"id": 1, "tipo": "test", "enunciado": "¿2+2?", "opciones": ["3", "4", "5", "6"], "asignatura": "MTP",
             "carpeta": ["MTP"], "nueva": True, "respuesta": "4", "explicacion": "Suma.", "chunk_id": 9}
        self.cerebro.answers = {
            "/api/repaso/hoy": {"dia": "2026-10-17", "preguntas": [q, "basura", {"id": "x"}], "total": 1,
                                "hechas": 0, "hecha": False, "racha": 2, "secreto": 1},
            "/api/repaso/libre": {"carpeta": "MTP", "preguntas": [q], "total": 1},
        }
        code, data = self.get("/api/repaso/hoy")
        self.assertEqual(code, 200)
        self.assertEqual(data, {"dia": "2026-10-17", "total": 1, "hechas": 0, "hecha": False, "racha": 2,
                                "preguntas": [{"id": 1, "tipo": "test", "enunciado": "¿2+2?",
                                               "opciones": ["3", "4", "5", "6"], "asignatura": "MTP",
                                               "carpeta": ["MTP"], "nueva": True}]})
        code, data = self.get("/api/repaso/libre?carpeta=MTP")
        self.assertEqual(code, 200)
        self.assertNotIn("respuesta", data["preguntas"][0])
        self.assertNotIn("chunk_id", data["preguntas"][0])

    def test_cerebro_caido_o_sin_configurar(self):
        self.web.cerebro = FakeCerebro(fail=ToolError("Cerebro no contesta (Connection refused): ¿está en marcha?"))
        self.assertEqual(self.get("/api/repaso/hoy"), (503, {"error": "El cerebro no está disponible ahora"}))
        self.web.cerebro = FakeCerebro(fail=http_error(503, "el Repaso no está disponible"))
        self.assertEqual(self.get("/api/repaso/resumen")[0], 503)
        self.web.cerebro = None
        self.assertEqual(self.post("/api/repaso/mal", {"question_id": 1})[0], 503)


class KatexTest(Base):
    def test_katex_servido_desde_aqui(self):
        code, headers, body = self.call("/katex/katex.min.js", raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertEqual(body, (WEB_DIR / "katex" / "katex.min.js").read_bytes())
        code, headers, _ = self.call("/katex/katex.min.css", raw=True)
        self.assertEqual((code, headers["Content-Type"].split(";")[0]), (200, "text/css"))
        code, headers, body = self.call("/katex/fonts/KaTeX_Main-Regular.woff2", raw=True)
        self.assertEqual((code, headers["Content-Type"]), (200, "font/woff2"))
        self.assertEqual(body[:4], b"wOF2")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        # Nada fuera de la lista: ni rutas libres, ni otros ficheros de la carpeta
        for path in ("/katex/fonts/KaTeX_Nada-Regular.woff2", "/katex/fonts/../katex.min.js",
                     "/katex/fonts/%2e%2e/%2e%2e/web.py", "/katex/LICENSE", "/katex/ORIGEN.txt",
                     "/katex/fonts/KaTeX_Main-Regular.ttf", "/katex/", "/katex/fonts/x/KaTeX_Main-Regular.woff2"):
            self.assertEqual(self.call(path)[0], 401, path)  # 401: no es un fichero de la página, pide llave
        # Las fuentes de la marca para la pestaña (tokens.css las pide en fonts/)
        code, headers, _ = self.call("/fonts/jetbrains-mono-700.woff2", raw=True)
        self.assertEqual((code, headers["Content-Type"]), (200, "font/woff2"))
        self.assertEqual(self.call("/tokens.css", raw=True)[0], 200)
        self.assertEqual(self.call("/repaso.js", raw=True)[0], 200)
        self.assertEqual(self.call("/repaso.css", raw=True)[0], 200)


class PaginaTest(unittest.TestCase):
    def setUp(self):
        self.html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        self.js = (WEB_DIR / "repaso.js").read_text(encoding="utf-8")
        self.apuntes_js = (WEB_DIR / "apuntes.js").read_text(encoding="utf-8")

    def test_pagina_estructura(self):
        html, js = self.html, self.js
        # La pestaña y sus pantallas
        self.assertIn('href="#repaso"', html)
        self.assertIn('id="repaso"', html)
        self.assertIn(">Empezar<", html)
        self.assertIn('class="a-progress"', html)
        self.assertIn("Esta pregunta está mal", html)
        self.assertIn('id="r-settings"', html)
        self.assertIn('type="time"', html)
        self.assertIn('role="status"', html)
        for ident in ("r-home", "r-question", "r-result", "r-report", "r-end", "r-free"):
            self.assertIn(f'id="{ident}"', html)
        # #repaso la abre al cargar (también el aviso del móvil)
        self.assertIn("#repaso", self.apuntes_js)
        # KaTeX desde Apuntes, sin CDN
        self.assertIn('src="/katex/katex.min.js"', html)
        self.assertIn('href="/katex/katex.min.css"', html)
        for tag in re.findall(r"<(?:script|link)\b[^>]*>", html):
            self.assertNotRegex(tag, r"(src|href)=\"(https?:)?//", tag)
        # Fórmulas con katex.render (renderToString + innerHTML lo bloquearía la CSP y abriría la puerta a HTML)
        self.assertIn("katex.render(", js)
        self.assertIn("throwOnError: false", js)
        self.assertNotIn("renderToString", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("textContent", js)
        # Cada uso de localStorage dentro de un try { … } (en modo privado o sin espacio lanza)
        uses = [m.start() for m in re.finditer(r"localStorage", js)]
        self.assertTrue(uses)
        for pos in uses:
            before = js[:pos]
            self.assertGreater(before.rfind("try {"), before.rfind("}"), js[pos - 80:pos + 40])
        # Solo se enlaza a páginas web («Ver en el apunte»), nunca un javascript:
        self.assertIn('rel = "noopener noreferrer"', js)
        self.assertIn("https?:", js)

    def test_fallada_vuelve_en_la_misma_sesion(self):
        # Al acabar la tanda diaria la pestaña vuelve a pedir /hoy: ahí llegan las falladas que tienen que volver
        js = self.js
        cuerpo = js[js.index("async function moreDaily"):js.index("async function showQuestion")]
        self.assertIn('rApi("/api/repaso/hoy")', cuerpo)
        self.assertIn("rs.queue.push(...hoy.preguntas)", cuerpo)
        self.assertIn("await moreDaily()", js[js.index("async function showQuestion"):])
        self.assertIn("Esta pregunta volverá al final de esta sesión.", js)
        self.assertIn("Esta pregunta volverá en otro repaso.", js)   # al 3.er fallo del día
        self.assertIn("rs.fallos[q.id] < 3", js)

    def test_repaso_js_cargado_y_csp_sin_inline(self):
        self.assertIn('<script src="/repaso.js" defer></script>', self.html)
        self.assertNotIn(" style=", self.html)
        self.assertNotRegex(self.html, r"<script>(?!</script>)")


if __name__ == "__main__":
    unittest.main()
