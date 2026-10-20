"""Presupuesto diario de Neurons del cerebro (arthur/cerebro/budget.py): prioridades, cambio de día UTC y fichero.

Sin red: el reloj es falso y el fichero va a un directorio temporal.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.cerebro.budget import COSTS, Budget, approx_tokens, estimate

MADRID = ZoneInfo("Europe/Madrid")


class Reloj:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self):
        return self.t


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cerebro" / "presupuesto.json"
        self.reloj = Reloj(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc))

    def tearDown(self):
        self.tmp.cleanup()

    def nuevo(self, daily=3000):
        return Budget(self.path, daily=daily, now=self.reloj)

    def test_reserva_por_prioridad(self):
        b = self.nuevo()
        b.spend("embed", 2000)
        # Generar preguntas solo llega al 80 % del tope (2 400): 2 000 + 600 se pasa.
        self.assertFalse(b.allows("generate", 600))
        self.assertTrue(b.allows("generate", 400))
        # Las preguntas de Marc pueden llegar al 100 % (3 000).
        self.assertTrue(b.allows("ask", 900))
        self.assertTrue(b.allows("ask", 1000))
        self.assertFalse(b.allows("ask", 1001))

    def test_embed_hasta_90(self):
        b = self.nuevo()
        b.spend("ask", 2500)
        self.assertTrue(b.allows("embed", 200))   # 2 700 justo
        self.assertFalse(b.allows("embed", 201))

    def test_cambia_de_dia(self):
        # 01:30 en Madrid del día 18 son las 23:30 UTC del 17: todavía es el mismo «día» de Cloudflare.
        self.reloj.t = datetime(2026, 10, 18, 1, 30, tzinfo=MADRID)
        b = self.nuevo()
        b.spend("ask", 1234.5)
        self.assertEqual(b.spent_today(), 1234.5)
        self.reloj.t = datetime(2026, 10, 17, 23, 59, 59, tzinfo=timezone.utc)
        self.assertEqual(b.spent_today(), 1234.5)
        # A las 00:00 UTC Cloudflare reinicia la capa gratis: se empieza de cero.
        self.reloj.t = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(b.spent_today(), 0)
        self.assertTrue(b.allows("generate", 2400))
        b.spend("generate", 10)
        datos = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(datos["day"], "2026-10-18")
        self.assertEqual(datos["spent"], {"generate": 10})

    def test_persiste(self):
        self.nuevo().spend("embed", 100)
        self.nuevo().spend("ask", 50.5)
        otra = self.nuevo()
        self.assertEqual(otra.spent_today(), 150.5)
        self.assertFalse(otra.allows("ask", 3000 - 150.5 + 1))

    def test_fichero_roto_empieza_de_cero(self):
        # Un fichero corrupto (corte de luz a medias, edición a mano) no tumba el cerebro: cuenta desde 0.
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{no es json", encoding="utf-8")
        b = self.nuevo()
        with self.assertLogs("arthur.cerebro.budget", "WARNING"):
            self.assertEqual(b.spent_today(), 0)
        b.spend("ask", 5)
        self.assertEqual(self.nuevo().spent_today(), 5)

    def test_valida_tipo_y_cantidad(self):
        b = self.nuevo()
        with self.assertRaises(ValueError):
            b.spend("otro", 1)
        with self.assertRaises(ValueError):
            b.allows("otro", 1)
        with self.assertRaises(ValueError):
            b.spend("ask", -1)
        with self.assertRaises(ValueError):
            b.spend("ask", float("nan"))

    def test_tope_ajustable(self):
        b = self.nuevo(daily=1000)
        self.assertTrue(b.allows("generate", 800))
        self.assertFalse(b.allows("generate", 801))


class EstimateTests(unittest.TestCase):
    def test_approx_tokens(self):
        self.assertEqual(approx_tokens(""), 0)
        self.assertEqual(approx_tokens("a"), 1)
        self.assertEqual(approx_tokens("x" * 7), 2)
        self.assertEqual(approx_tokens("x" * 8), 3)

    def test_estimate(self):
        entrada, salida = COSTS["chat"]
        self.assertAlmostEqual(estimate("chat", 1_000_000, 0), entrada)
        self.assertAlmostEqual(estimate("chat", 2000, 300), (2000 * entrada + 300 * salida) / 1_000_000)
        # Los embeddings no tienen salida que pagar.
        self.assertAlmostEqual(estimate("bge-m3", 1_000_000, 999), COSTS["bge-m3"][0])
        with self.assertRaises(KeyError):
            estimate("otro", 1, 1)

    def test_una_pregunta_es_barata(self):
        # Orden de magnitud de una pregunta (8 trozos de 800 caracteres + 150 palabras): unas decenas de Neurons.
        coste = estimate("chat", approx_tokens("x" * 7000), approx_tokens("x" * 1000))
        self.assertLess(coste, 50)


if __name__ == "__main__":
    unittest.main()
