"""API del Repaso (/api/repaso/* en cerebro/web.py, lógica en cerebro/repaso/service.py) y sus ajustes.

Servidor de verdad en 127.0.0.1:0, almacén en memoria y reloj fijo. Sin red.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.cerebro.repaso.service import Repaso
from arthur.cerebro.repaso.store import RepasoStore
from arthur.cerebro.settings import CerebroError, load_settings, save_repaso
from arthur.cerebro.web import CerebroWeb
from arthur.security.devices import DeviceRegistry

MADRID = ZoneInfo("Europe/Madrid")
AHORA = datetime(2026, 10, 17, 19, 0, tzinfo=MADRID)


def pregunta(tipo="test", asignatura="Cálculo", **extra):
    q = {"chunk_id": 1, "asignatura": asignatura, "tipo": tipo, "enunciado": f"¿{tipo}?", "explicacion": "Porque sí."}
    q.update({"test": {"opciones": ["2", "3", "4", "5"], "respuesta": "4"},
              "vf": {"respuesta": True},
              "formula": {"enunciado": "$(x^n)' = ___$", "respuesta": "n x^{n-1}"},
              "corta": {"respuesta": "El límite del cociente de derivadas"}}[tipo])
    q.update(extra)
    return q


class RepasoWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ajustes = root / "ajustes.toml"
        self.now = AHORA
        self.store = RepasoStore.in_memory(now=lambda: self.now)
        self.repaso = Repaso(self.store, self.ajustes, now=lambda: self.now,
                             link_fn=lambda chunk_id: f"https://apuntes/#n{chunk_id}",
                             subjects_fn=lambda: ["Cálculo", "MTP", "Historia"])
        self.repaso.save_settings({"asignaturas": ["Cálculo", "MTP"]})
        self.ids = {t: self.store.add_question(pregunta(t)) for t in ("test", "vf", "formula", "corta")}
        self.devices = DeviceRegistry(root / "devices.json")
        self.keys = {name: self.devices.add(name) for name in ("core", "panel", "apuntes", "studio")}
        self.web = CerebroWeb(self.devices, lambda q, s: {}, lambda: {}, None, repaso=self.repaso,
                              host="127.0.0.1", port=0, sleep=lambda s: None)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def call(self, path, body=None, *, device="apuntes"):
        req = urllib.request.Request(self.web.url + path, method="POST" if body is not None else "GET")
        if device:
            req.add_header("Authorization", f"Bearer {self.keys[device]}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Arthur", "1")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def responder(self, tipo, **body):
        return self.call("/api/repaso/respuesta", {"question_id": self.ids[tipo], **body})

    def test_hoy_sin_respuestas(self):
        code, data = self.call("/api/repaso/hoy")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["preguntas"]), 4)
        for q in data["preguntas"]:
            self.assertNotIn("respuesta", q)
            self.assertNotIn("explicacion", q)
            self.assertNotIn("chunk_id", q)
        por_tipo = {q["tipo"]: q for q in data["preguntas"]}
        self.assertEqual(por_tipo["test"]["opciones"], ["2", "3", "4", "5"])
        self.assertIsNone(por_tipo["vf"]["opciones"])
        self.assertEqual((data["total"], data["hechas"], data["hecha"], data["racha"]), (4, 0, False, 0))
        # Solo las asignaturas activas
        self.store.add_question(pregunta("vf", asignatura="Historia"))
        self.assertEqual(len(self.call("/api/repaso/hoy")[1]["preguntas"]), 4)
        # El modo libre tampoco enseña la respuesta
        code, data = self.call("/api/repaso/libre?carpeta=" + urllib.parse.quote("Cálculo"))
        self.assertEqual(code, 200)
        self.assertEqual(len(data["preguntas"]), 4)
        self.assertTrue(all("respuesta" not in q for q in data["preguntas"]))
        code, data = self.call("/api/repaso/libre")
        self.assertEqual(code, 200)
        self.assertIn({"carpeta": "Cálculo", "preguntas": 4}, data["carpetas"])
        self.assertEqual(self.call("/api/repaso/libre?carpeta=" + urllib.parse.quote("Cálculo//x"))[0], 400)

    def test_respuesta_corrige_en_servidor(self):
        self.call("/api/repaso/hoy")  # fija la sesión de hoy: lo contestado cuenta como «diaria»
        # La nota que mande el navegador no cuenta: falla → 1 aunque diga 4
        code, data = self.responder("test", respuesta="3", nota=4)
        self.assertEqual(code, 200)
        self.assertFalse(data["correcto"])
        self.assertTrue(data["otra_vez"])  # fallada: vuelve en esta misma sesión
        self.assertEqual(data["nota"], 1)
        self.assertEqual(data["respuesta"], "4")
        self.assertEqual(data["explicacion"], "Porque sí.")
        self.assertEqual(data["enlace"], "https://apuntes/#n1")
        code, data = self.responder("vf", respuesta=True)
        self.assertEqual((code, data["correcto"], data["nota"]), (200, True, 3))
        code, data = self.responder("formula", respuesta=" $n x^{n - 1}$ ")
        self.assertEqual((code, data["correcto"], data["nota"]), (200, True, 3))
        respuestas = self.store.answers_since(AHORA - timedelta(hours=1))
        self.assertEqual([(a["rating"], a["correct"], a["mode"]) for a in respuestas],
                         [(1, False, "diaria"), (3, True, "diaria"), (3, True, "diaria")])
        # Sin respuesta, o de tipo que no toca → 400; pregunta que no existe → 404
        self.assertEqual(self.responder("test")[0], 400)
        self.assertEqual(self.responder("vf", respuesta="quizá")[0], 400)
        self.assertEqual(self.call("/api/repaso/respuesta", {"question_id": 999, "respuesta": "x"})[0], 404)
        self.assertEqual(self.call("/api/repaso/respuesta", {"question_id": "1", "respuesta": "x"})[0], 400)

    def test_nota_corta_la_pone_marc(self):
        # Sin nota: enseña la respuesta y no apunta nada
        code, data = self.responder("corta", respuesta="ni idea")
        self.assertEqual(code, 200)
        self.assertTrue(data["falta_nota"])
        self.assertEqual(data["respuesta"], "El límite del cociente de derivadas")
        self.assertEqual(self.store.answers_since(AHORA - timedelta(hours=1)), [])
        # Con la nota de Marc (Difícil): se apunta tal cual, y cuenta como acierto
        code, data = self.responder("corta", nota=2)
        self.assertEqual((code, data["nota"], data["correcto"]), (200, 2, True))
        self.assertEqual(self.store.card(self.ids["corta"]).reps, 1)
        self.assertEqual(self.responder("corta", nota=5)[0], 400)
        self.assertEqual(self.responder("corta", nota=True)[0], 400)

    def test_sesion_hecha_y_racha(self):
        self.call("/api/repaso/hoy")
        for tipo, body in (("test", {"respuesta": "4"}), ("vf", {"respuesta": False}),
                           ("formula", {"respuesta": "x"}), ("corta", {"nota": 3})):
            data = self.responder(tipo, **body)[1]
        # vf y fórmula se han fallado: vuelven al final de la sesión, que aún no está hecha
        self.assertEqual((data["hecha"], data["otra_vez"]), (False, False))
        code, data = self.call("/api/repaso/hoy")
        self.assertEqual([q["id"] for q in data["preguntas"]], [self.ids["vf"], self.ids["formula"]])
        self.assertEqual((data["hechas"], data["total"], data["hecha"]), (2, 4, False))
        self.assertEqual(self.repaso.pending(), 2)
        self.now = AHORA + timedelta(minutes=5)
        self.assertFalse(self.responder("vf", respuesta=True)[1]["hecha"])
        data = self.responder("formula", respuesta="n x^{n-1}")[1]
        self.assertTrue(data["hecha"])
        code, data = self.call("/api/repaso/hoy")
        self.assertEqual((data["preguntas"], data["hecha"], data["racha"], data["hechas"]), ([], True, 1, 4))
        code, data = self.call("/api/repaso/resumen", device="panel")
        self.assertEqual(code, 200)
        self.assertEqual(data["racha"], 1)
        self.assertEqual(data["pendientes"], 0)
        self.assertAlmostEqual(data["aciertos_7d"], 0.5)
        self.assertEqual(data["por_asignatura"], {"Cálculo": 0.5})
        # Para la revisión del panel: lo de hoy y los últimos 7 días (primer intento de la sesión diaria)
        self.assertEqual(data["hoy"], {"preguntas": 4, "aciertos": 2})
        self.assertEqual(data["dias"]["2026-10-17"], {"hecha": True, "preguntas": 4, "aciertos": 2})
        self.assertEqual(data["dias"]["2026-10-16"], {"hecha": False, "preguntas": 0, "aciertos": 0})
        self.assertEqual(len(data["dias"]), 7)

    def test_mal_retira(self):
        code, data = self.call("/api/repaso/mal", {"question_id": self.ids["test"], "motivo": "la 3 también vale"})
        self.assertEqual(code, 200)
        q = self.store.question(self.ids["test"])
        self.assertIsNotNone(q["retired_at"])
        self.assertEqual(q["motivo"], "la 3 también vale")
        self.assertNotIn(self.ids["test"], [q["id"] for q in self.call("/api/repaso/hoy")[1]["preguntas"]])
        self.assertEqual(self.responder("test", respuesta="4")[0], 409)
        self.assertEqual(self.call("/api/repaso/mal", {"question_id": self.ids["vf"], "motivo": "x" * 301})[0], 400)
        self.assertEqual(self.call("/api/repaso/mal", {"question_id": 999, "motivo": ""})[0], 404)

    def test_mal_con_la_ultima_completa_la_sesion(self):
        self.call("/api/repaso/hoy")
        for tipo, body in (("test", {"respuesta": "4"}), ("vf", {"respuesta": True}),
                           ("formula", {"respuesta": "n x^{n-1}"})):
            self.responder(tipo, **body)
        self.assertEqual(self.call("/api/repaso/mal", {"question_id": self.ids["corta"], "motivo": "ambigua"})[0], 200)
        code, data = self.call("/api/repaso/hoy")
        self.assertEqual((data["preguntas"], data["hecha"], data["racha"]), ([], True, 1))

    def test_doble_envio(self):
        self.call("/api/repaso/hoy")
        primera = self.responder("vf", respuesta=True)[1]
        code, segunda = self.responder("vf", respuesta=True)
        self.assertEqual(code, 200)
        self.assertEqual(segunda["vuelve"], primera["vuelve"])
        self.assertEqual(self.store.card(self.ids["vf"]).reps, 1)
        self.assertEqual(len(self.store.answers_since(AHORA - timedelta(hours=1))), 1)

    def test_sesion_tras_medianoche(self):
        self.now = datetime(2026, 10, 17, 23, 55, tzinfo=MADRID)
        self.call("/api/repaso/hoy")
        self.responder("test", respuesta="4")
        self.responder("vf", respuesta=True)
        self.responder("formula", respuesta="n x^{n-1}")
        self.now = datetime(2026, 10, 18, 0, 5, tzinfo=MADRID)
        data = self.responder("corta", nota=3)[1]
        self.assertTrue(data["hecha"])  # la sesión que se estaba haciendo, la de ayer
        self.assertEqual(data["racha"], 1)
        self.assertTrue(self.store.day(datetime(2026, 10, 17).date())["done"])

    def test_aciertos_semana_solo_sesion_diaria(self):
        self.call("/api/repaso/hoy")
        self.responder("vf", respuesta=True)
        for _ in range(3):
            self.responder("test", respuesta="3")   # el primero es de la sesión; los otros, el mismo fallo repetido
        self.store.answer(self.ids["formula"], 1, False, "libre", AHORA)
        data = self.call("/api/repaso/resumen", device="panel")[1]
        self.assertAlmostEqual(data["aciertos_7d"], 0.5)

    def test_ajustes_valida_hora(self):
        code, data = self.call("/api/repaso/ajustes")
        self.assertEqual(code, 200)
        self.assertEqual(data["asignaturas"], ["Cálculo", "MTP"])
        self.assertEqual(data["aviso"], "20:00")
        self.assertEqual(data["disponibles"], ["Cálculo", "MTP", "Historia"])
        for body in ({"aviso": "25:00"}, {"aviso": "8:00"}, {"aviso": "20:00:00"}, {"aviso": 20},
                     {"asignaturas": "Cálculo"}, {"asignaturas": [""]}, {"asignaturas": ["x" * 101]},
                     {"retencion": 0.5}, {"retencion": True}):
            self.assertEqual(self.call("/api/repaso/ajustes", body)[0], 400, body)
        code, data = self.call("/api/repaso/ajustes", {"aviso": "21:30", "asignaturas": ["Álgebra", "Álgebra"],
                                                       "retencion": 0.85})
        self.assertEqual(code, 200)
        self.assertEqual((data["aviso"], data["asignaturas"], data["retencion"]), ("21:30", ["Álgebra"], 0.85))
        # Queda en ajustes.toml y el resto del fichero sigue igual
        s = load_settings(self.ajustes)
        self.assertEqual((s.repaso.notice, s.repaso.subjects, s.repaso.retention),
                         (time(21, 30), ("Álgebra",), 0.85))
        self.assertEqual(s.budget, 3000)

    def test_solo_apuntes_y_panel(self):
        self.assertEqual(self.call("/api/repaso/hoy", device="core")[0], 403)
        self.assertEqual(self.call("/api/repaso/hoy", device="studio")[0], 403)
        self.assertEqual(self.call("/api/repaso/hoy", device="panel")[0], 403)
        self.assertEqual(self.call("/api/repaso/hoy", device=None)[0], 401)
        self.assertEqual(self.call("/api/repaso/resumen", device="panel")[0], 200)
        self.assertEqual(self.call("/api/repaso/mal", {"question_id": 1, "motivo": ""}, device="core")[0], 403)


class EnlaceTest(unittest.TestCase):
    def test_ver_en_el_apunte(self):
        from arthur.cerebro.answer import Links
        from arthur.cerebro.chunks import Doc
        from arthur.cerebro.cli import apunte_link
        from arthur.cerebro.index import Index

        with tempfile.TemporaryDirectory() as tmp:
            index = Index(Path(tmp) / "indice.sqlite3")
            try:
                index.sync([Doc(source="apuntes", ref="apunte:42", title="Derivadas", subject="Cálculo",
                                changed_at="2026-10-17T20:05:00+02:00", text="La derivada de x^n.", links=())],
                           source="apuntes")
                cid = index.chunk_ids("apuntes", {"Cálculo"})[0]
                self.assertEqual(apunte_link(index, Links("https://apuntes.example/"), cid),
                                 "https://apuntes.example/#n42")
                self.assertEqual(apunte_link(index, Links(""), cid), "")
                self.assertEqual(apunte_link(index, Links("https://apuntes.example"), 999), "")
                self.assertEqual(index.subjects("apuntes"), ["Cálculo"])
            finally:
                index.close()


class AjustesRepasoTest(unittest.TestCase):
    def test_por_defecto_y_guardar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ajustes.toml"
            s = load_settings(path)
            self.assertEqual(s.repaso.subjects, ("MTP", "ETC", "Álgebra", "Cálculo"))
            self.assertEqual((s.repaso.notice, s.repaso.retention), (time(20, 0), 0.9))
            save_repaso(path, subjects=('Cálculo "1"',), notice=time(7, 5), retention=0.9)
            save_repaso(path, subjects=("Física\\2",), notice=time(7, 5), retention=0.9)
            s = load_settings(path)
            self.assertEqual((s.repaso.subjects, s.repaso.notice), (("Física\\2",), time(7, 5)))
            self.assertEqual(path.read_text(encoding="utf-8").count("[repaso]"), 1)

    def test_fichero_sin_repaso_usa_lo_de_siempre(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ajustes.toml"
            path.write_text("presupuesto = 2000\n", encoding="utf-8")
            self.assertEqual(load_settings(path).repaso.notice, time(20, 0))
            path.write_text('presupuesto = 2000\n[repaso]\naviso = "24:00"\n', encoding="utf-8")
            with self.assertRaises(CerebroError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
