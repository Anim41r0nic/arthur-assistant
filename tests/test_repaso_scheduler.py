"""Reloj del Repaso (cerebro/scheduler.py: RepasoClock): preguntas nuevas a las 02:00 y aviso diario.

Reloj, notificador y generador falsos. Sin red ni hilos.
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.cerebro.repaso.service import Repaso
from arthur.cerebro.repaso.store import RepasoStore
from arthur.cerebro.scheduler import RepasoClock

MADRID = ZoneInfo("Europe/Madrid")


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=MADRID)


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def send(self, text, *, destination=None):
        self.sent.append((text, destination))
        return self.ok


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = at(17, 12)
        self.store = RepasoStore.in_memory(now=lambda: self.now)
        self.repaso = Repaso(self.store, Path(self.tmp.name) / "ajustes.toml", now=lambda: self.now)
        self.repaso.save_settings({"asignaturas": ["Cálculo"], "aviso": "20:00"})
        self.generados = []
        self.notifier = FakeNotifier()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def clock(self, notifier="default"):
        return RepasoClock(self.repaso, lambda now, limit: self.generados.append(now) or {"creadas": 0},
                           self.notifier if notifier == "default" else notifier, now=lambda: self.now)

    def add(self, n=3):
        for i in range(n):
            self.store.add_question({"chunk_id": i, "asignatura": "Cálculo", "tipo": "vf", "enunciado": f"¿{i}?",
                                     "respuesta": True, "explicacion": "Sí."})

    def tick(self, clock, when):
        self.now = when
        clock.tick()

    def test_aviso_una_vez(self):
        self.add(3)
        c = self.clock()
        self.tick(c, at(17, 19, 59))
        self.assertEqual(self.notifier.sent, [])
        self.tick(c, at(17, 20, 0))
        self.tick(c, at(17, 20, 1))
        self.tick(c, at(17, 22, 0))
        self.assertEqual(self.notifier.sent, [("3 preguntas te esperan", "apuntes")])
        self.assertTrue(self.store.day(at(17, 20).date())["notified"])
        # Un reloj nuevo (reinicio) tampoco repite: lo dice el almacén
        c2 = self.clock()
        self.tick(c2, at(17, 21))
        self.assertEqual(len(self.notifier.sent), 1)
        # Al día siguiente vuelve a tocar, con la racha si la hay
        self.store.mark_day(at(17, 20).date(), done=True)
        self.tick(c2, at(18, 20, 0))
        self.assertEqual(self.notifier.sent[-1], ("3 preguntas te esperan · racha 1 día", "apuntes"))

    def test_aviso_no_si_hecha(self):
        self.add(2)
        self.store.mark_day(at(17, 20).date(), done=True)
        c = self.clock()
        self.tick(c, at(17, 20, 5))
        self.assertEqual(self.notifier.sent, [])
        # Ni si no hay preguntas
        self.tick(c, at(18, 20, 5))  # el 18 no está hecho y siguen ahí las 2 nuevas: sí toca
        self.assertEqual(len(self.notifier.sent), 1)
        self.store.retire(1, "mal")
        self.store.retire(2, "mal")
        self.tick(c, at(19, 20, 5))
        self.assertEqual(len(self.notifier.sent), 1)

    def test_aviso_tras_arranque_tarde(self):
        self.add(1)
        c = self.clock()
        self.tick(c, at(17, 22, 30))  # arranca a las 22:30: aún toca
        self.assertEqual(self.notifier.sent, [("1 pregunta te espera", "apuntes")])
        c = self.clock()
        self.tick(c, at(18, 23, 5))   # a las 23:05 ya no: mejor nada que un aviso a medianoche
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertFalse(self.store.day(at(18, 12).date())["notified"])

    def test_aviso_no_llega_reintenta(self):
        # El núcleo sin la dirección de Apuntes contesta 503 → send devuelve False: el día no queda avisado
        self.add(1)
        self.notifier.ok = False
        c = self.clock()
        self.tick(c, at(17, 20, 0))
        self.assertFalse(self.store.day(at(17, 20).date())["notified"])
        self.assertIn("no ha llegado", c.state()["aviso"]["error"])
        self.tick(c, at(17, 20, 1))  # no lo reintenta cada minuto
        self.assertEqual(len(self.notifier.sent), 1)
        self.notifier.ok = True
        self.tick(c, at(17, 20, 20))
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertTrue(self.store.day(at(17, 20).date())["notified"])
        self.assertIsNone(c.state()["aviso"]["error"])

    def test_sin_avisos_configurados(self):
        self.add(1)
        c = self.clock(notifier=None)
        self.tick(c, at(17, 20, 0))
        self.assertFalse(self.store.day(at(17, 20).date())["notified"])
        self.assertIn("sin avisos", c.state()["aviso"]["error"])

    def test_generar_a_las_2(self):
        c = self.clock()
        self.tick(c, at(17, 1, 59))
        self.assertEqual(self.generados, [])
        self.tick(c, at(17, 2, 0))
        self.tick(c, at(17, 2, 1))
        self.tick(c, at(17, 12, 0))
        self.assertEqual(self.generados, [at(17, 2, 0)])
        self.tick(c, at(18, 2, 3))
        self.assertEqual(self.generados, [at(17, 2, 0), at(18, 2, 3)])
        self.assertEqual(c.state()["generacion"], {"creadas": 0})
        # Arrancar a mediodía no genera (se espera a la noche, que es cuando sobra presupuesto)
        c = self.clock()
        self.tick(c, at(19, 12))
        self.assertEqual(len(self.generados), 2)

    def test_generar_falla_no_para_el_reloj(self):
        self.add(1)

        def roto(now, limit):
            raise RuntimeError("roto")
        c = RepasoClock(self.repaso, roto, self.notifier, now=lambda: self.now)
        self.tick(c, at(17, 2, 0))
        self.assertIn("RuntimeError", c.state()["generacion"]["error"])
        self.tick(c, at(17, 20, 0))
        self.assertEqual(len(self.notifier.sent), 1)


class NightTest(unittest.TestCase):
    """Lo hecho cada noche queda en SQLite: un reinicio entre las 02:00 y las 06:00 no rompe el tope de 40 y un
    fallo de Workers AI a las 02:00 no pierde la noche entera."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = at(18, 2)
        self.path = Path(self.tmp.name) / "repaso.sqlite3"
        self.open_store()
        self.calls = []        # (hora, límite) de cada vez que se ha generado
        self.results = []      # lo que devuelve cada llamada, en orden; sin más, «sin trozos pendientes»

    def open_store(self):
        self.store = RepasoStore(self.path, now=lambda: self.now)
        self.addCleanup(self.store.close)
        self.repaso = Repaso(self.store, Path(self.tmp.name) / "ajustes.toml", now=lambda: self.now)

    def forget_night(self):
        # Como si el proceso hubiera muerto antes de apuntar la noche
        self.store._q("DELETE FROM nights")

    def generate(self, now, limit):
        self.calls.append((now, limit))
        result = self.results.pop(0) if self.results else {"creadas": 0, "parado": None}
        if isinstance(result, Exception):
            raise result
        for i in range(result.get("creadas", 0)):
            self.store.add_question({"chunk_id": 100 * len(self.calls) + i, "asignatura": "Cálculo", "tipo": "vf",
                                     "enunciado": f"¿{i}?", "respuesta": True, "explicacion": "Sí."})
        return result

    def clock(self):
        return RepasoClock(self.repaso, self.generate, None, now=lambda: self.now)

    def tick(self, clock, when):
        self.now = when
        clock.tick()

    def test_reinicio_no_vuelve_a_generar(self):
        self.results = [{"creadas": 40, "parado": "tope de la noche"}]
        self.tick(self.clock(), at(18, 2, 0))
        # El cerebro se reinicia a las 03:00: el reloj nuevo lee la noche del disco y no genera otra vez
        self.store.close()
        self.open_store()
        c = self.clock()
        self.tick(c, at(18, 3, 0))
        self.tick(c, at(18, 4, 0))
        self.assertEqual(self.calls, [(at(18, 2, 0), 40)])
        night = self.store.night(at(18, 2).date())
        self.assertEqual(night["creadas"], 40)
        self.assertFalse(night["reintentar"])
        # Y el estado enseña la noche guardada aunque este reloj no haya generado nada
        self.assertEqual(c.state()["generacion"]["creadas"], 40)

    def test_fallo_de_workers_ai_se_reintenta_cada_30_min(self):
        self.results = [{"creadas": 10, "parado": "Workers AI no disponible", "reintentar": True},
                        {"creadas": 5, "parado": "Workers AI no disponible", "reintentar": True},
                        {"creadas": 0, "parado": None}]
        c = self.clock()
        for hh, mm in ((2, 0), (2, 1), (2, 29), (2, 30), (2, 45), (3, 0), (3, 30), (4, 0)):
            self.tick(c, at(18, hh, mm))
        # Cada reintento pide solo lo que falta para 40; con «sin trozos pendientes» la noche queda cerrada
        self.assertEqual(self.calls, [(at(18, 2, 0), 40), (at(18, 2, 30), 30), (at(18, 3, 0), 25)])
        self.assertEqual(self.store.night(at(18, 2).date())["creadas"], 15)

    def test_reinicio_a_mitad_resta_lo_ya_generado(self):
        # A las 02:00 se guardan 12 preguntas y el cerebro se cae antes de apuntar la noche
        self.results = [{"creadas": 12, "parado": "Workers AI no disponible", "reintentar": True}]
        self.tick(self.clock(), at(18, 2, 0))
        self.forget_night()
        self.store.close()
        self.open_store()
        self.tick(self.clock(), at(18, 2, 40))
        self.assertEqual(self.calls[-1], (at(18, 2, 40), 28))

    def test_caida_durante_la_generacion_espera_30_min(self):
        # El intento se apunta ANTES de llamar: si el cerebro muere a media generación, al arrancar a las 02:10
        # no se lanza otra enseguida (y otra, y otra si el fallo se repite), se espera a las 02:30
        def muere(now, limit):
            self.calls.append((now, limit))
            raise SystemExit  # no lo coge el reloj: como un proceso que se muere
        with self.assertRaises(SystemExit):
            self.tick(RepasoClock(self.repaso, muere, None, now=lambda: self.now), at(18, 2, 0))
        c = self.clock()
        self.tick(c, at(18, 2, 10))
        self.assertEqual(len(self.calls), 1)
        self.tick(c, at(18, 2, 30))
        self.assertEqual(len(self.calls), 2)

    def test_error_inesperado_se_reintenta(self):
        self.results = [RuntimeError("roto")]
        c = self.clock()
        self.tick(c, at(18, 2, 0))
        self.assertIn("RuntimeError", c.state()["generacion"]["error"])
        self.tick(c, at(18, 2, 30))
        self.assertEqual(len(self.calls), 2)

    def test_tope_sin_presupuesto_o_rechazo_no_se_reintentan(self):
        for parado in ("tope de la noche", "sin presupuesto", "Workers AI rechaza la petición (EngineRefused)",
                       "sin Workers AI esta noche"):
            with self.subTest(parado=parado):
                self.forget_night()
                self.calls.clear()
                self.results = [{"creadas": 0, "parado": parado, "reintentar": False}]
                c = self.clock()
                self.tick(c, at(18, 2, 0))
                self.tick(c, at(18, 2, 30))
                self.tick(c, at(18, 5, 59))
                self.assertEqual(len(self.calls), 1)

    def test_reintento_solo_dentro_de_la_ventana(self):
        self.results = [{"creadas": 0, "parado": "Workers AI no disponible", "reintentar": True}] * 2
        c = self.clock()
        self.tick(c, at(18, 5, 45))
        self.tick(c, at(18, 6, 15))   # ya es de día: el presupuesto es para las preguntas de Marc
        self.assertEqual(len(self.calls), 1)
        self.tick(c, at(19, 2, 0))    # la noche siguiente empieza de cero
        self.assertEqual(self.calls[-1], (at(19, 2, 0), 40))

    def test_con_40_ya_hechas_no_llama(self):
        self.results = [{"creadas": 40, "parado": "Workers AI no disponible", "reintentar": True}]
        c = self.clock()
        self.tick(c, at(18, 2, 0))
        self.tick(c, at(18, 2, 30))
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
