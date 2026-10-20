"""La línea «Repaso: racha N · X % esta semana» del panel (panel/today.py + cerebro/relay.repaso_line).

El panel lee GET /api/repaso/resumen del cerebro con su llave; si el cerebro no contesta, no sale nada (ni un aviso
en «Falta algo por cargar»: el Repaso no es parte del día que haga falta arreglar)."""

import http.client
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from arthur.cerebro import relay
from arthur.panel.store import PanelStore
from arthur.panel.today import build_today
from arthur.panel.web import WEB_DIR
from arthur.tools.base import ToolError

AHORA = datetime(2026, 10, 17, 9, 0, tzinfo=ZoneInfo("Europe/Madrid"))
RESUMEN = {"racha": 6, "pendientes": 10, "hecha": False, "aciertos_7d": 0.8181, "por_asignatura": {"MTP": 0.9},
           "extra": "fuera"}


class FakeCerebro:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer if answer is not None else RESUMEN, fail, []

    def call(self, path, body=None, *, timeout=None):
        self.calls.append((path, body, timeout))
        if isinstance(self.fail, BaseException):
            raise self.fail
        if self.fail:
            raise ToolError("Cerebro no contesta (Connection refused): ¿está en marcha?")
        return self.answer


class PanelRepasoTest(unittest.TestCase):
    def setUp(self):
        self.store = PanelStore.in_memory(now=lambda: AHORA)

    def tearDown(self):
        self.store.close()

    def today(self, repaso_fn):
        return build_today(store=self.store, vault=None, calendar=None, gmail=None, studio_link=None, now=AHORA,
                           repaso_fn=repaso_fn)

    def test_panel_linea_repaso(self):
        cerebro = FakeCerebro()
        line = relay.repaso_line(cerebro, "https://apuntes.ejemplo")
        self.assertEqual(line, {"racha": 6, "aciertos_7d": 82, "pendientes": 10, "hecha": False,
                                "url": "https://apuntes.ejemplo/#repaso"})
        # Con poco tiempo de espera: el panel no se queda colgado si el cerebro va lento
        self.assertEqual(cerebro.calls, [("/api/repaso/resumen", None, relay.LINE_TIMEOUT)])
        self.assertLessEqual(relay.LINE_TIMEOUT, 5)
        day = self.today(lambda: line)
        self.assertEqual(day["repaso"], line)
        self.assertEqual(day["problems"], [])
        # Sin respuestas esta semana: sin porcentaje; sin dirección de Apuntes (o rara): sin enlace
        line = relay.repaso_line(FakeCerebro({**RESUMEN, "aciertos_7d": None}), "javascript:alert(1)")
        self.assertEqual((line["aciertos_7d"], line["url"]), (None, ""))
        self.assertEqual(relay.repaso_line(FakeCerebro(), "")["url"], "")
        # La página la pinta con textContent y enlaza a Apuntes
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        js = (WEB_DIR / "panel.js").read_text(encoding="utf-8")
        self.assertIn('id="repaso-line"', html)
        self.assertIn("day.repaso", js)
        self.assertIn("esta semana", js)
        self.assertNotIn(".innerHTML", js)

    def test_lo_de_hoy_y_los_dias_para_la_revision(self):
        answer = {**RESUMEN, "hoy": {"preguntas": 10, "aciertos": 8, "raro": 1},
                  "dias": {"2026-10-16": {"hecha": True, "preguntas": 10, "aciertos": 9},
                           "no-es-fecha": {"hecha": True}, "2026-13-45": {"hecha": True}, "2026-10-15": {"hecha": "sí", "preguntas": -1},
                           **{f"2026-09-{d:02d}": {"hecha": True} for d in range(1, 20)}}}
        line = relay.repaso_line(FakeCerebro(answer), "")
        self.assertEqual(line["hoy"], {"preguntas": 10, "aciertos": 8})
        self.assertEqual(line["dias"]["2026-10-16"], {"hecha": True, "preguntas": 10, "aciertos": 9})
        self.assertEqual(line["dias"]["2026-10-15"], {"hecha": False, "preguntas": None, "aciertos": None})
        self.assertNotIn("no-es-fecha", line["dias"])
        self.assertNotIn("2026-13-45", line["dias"])
        self.assertLessEqual(len(line["dias"]), relay.MAX_REPASO_DAYS)
        # Un cerebro antiguo (0.65.0) no los manda: la línea sale igual, sin ellos
        self.assertNotIn("hoy", relay.repaso_line(FakeCerebro(), ""))

    def test_panel_sin_cerebro(self):
        self.assertIsNone(relay.repaso_line(None, "https://apuntes.ejemplo"))
        self.assertIsNone(relay.repaso_line(FakeCerebro(fail=True), "https://apuntes.ejemplo"))
        self.assertIsNone(relay.repaso_line(FakeCerebro({"racha": "x"}), "https://apuntes.ejemplo"))
        day = self.today(lambda: None)
        self.assertIsNone(day["repaso"])
        self.assertEqual(day["problems"], [])
        self.assertIsNone(self.today(None)["repaso"])  # sin cerebro configurado

    def test_red_cortada_no_tumba_el_panel(self):
        # Aunque llegue un error de red sin envolver (respuesta a medias, conexión reseteada), el panel sale sin línea
        for error in (http.client.IncompleteRead(b"{", 10), ConnectionResetError(104, "reset"),
                      http.client.RemoteDisconnected("cerrada"), TimeoutError()):
            self.assertIsNone(relay.repaso_line(FakeCerebro(fail=error), "https://apuntes.ejemplo"))

    def test_cache_de_la_linea(self):
        reloj = [1000.0]
        cerebro = FakeCerebro()
        line = relay.RepasoLine(cerebro, "https://apuntes.ejemplo", clock=lambda: reloj[0])
        primera = line()
        self.assertEqual(primera["racha"], 6)
        reloj[0] += relay.LINE_CACHE - 1
        self.assertEqual(line(), primera)
        self.assertEqual(len(cerebro.calls), 1)  # dentro del minuto no se vuelve a preguntar
        reloj[0] += 2
        cerebro.answer = {**RESUMEN, "racha": 7}
        self.assertEqual(line()["racha"], 7)
        self.assertEqual(len(cerebro.calls), 2)
        # Si el cerebro no contesta, tampoco se le vuelve a esperar en cada carga
        cerebro.fail = True
        reloj[0] += relay.LINE_CACHE + 1
        self.assertIsNone(line())
        self.assertIsNone(line())
        self.assertEqual(len(cerebro.calls), 3)
        self.assertLessEqual(relay.LINE_CACHE, 120)
        self.assertIsNone(relay.RepasoLine(None, "")())

    def test_carpetas_que_no_son_lista(self):
        for raro in ("abc", {"carpeta": "x"}, 5, None):
            self.assertEqual(relay._clean_repaso("libre", {"carpetas": raro}, None), {"carpetas": []})
        ok = relay._clean_repaso("libre", {"carpetas": [{"carpeta": "Cálculo", "preguntas": 3}, "x"]}, None)
        self.assertEqual(ok, {"carpetas": [{"carpeta": "Cálculo", "preguntas": 3}]})


if __name__ == "__main__":
    unittest.main()
