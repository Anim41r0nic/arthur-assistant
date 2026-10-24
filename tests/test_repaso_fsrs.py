import math
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from arthur.cerebro.repaso.fsrs import (
    W, Card, new_card, retrievability, review,
)

MADRID = ZoneInfo("Europe/Madrid")
AHORA = datetime(2026, 10, 17, 20, 5, tzinfo=MADRID)


def repasos(notas: list[int], inicio: datetime, **kw) -> Card:
    """Contesta cada nota justo cuando la tarjeta vence (como hacen los tests de py-fsrs)."""
    card = new_card(inicio)
    for nota in notas:
        card = review(card, nota, card.due, **kw)
    return card


class FsrsTests(unittest.TestCase):
    def test_parametros_por_defecto(self) -> None:
        # Copiados de DEFAULT_PARAMETERS de py-fsrs (FSRS-6, 21 valores; el último es el decaimiento)
        self.assertEqual(len(W), 21)
        self.assertEqual(W[0], 0.212)
        self.assertEqual(W[20], 0.1542)

    def test_estabilidad_inicial(self) -> None:
        for nota, esperada in zip((1, 2, 3, 4), (0.212, 1.2931, 2.3065, 8.2956)):
            self.assertEqual(review(new_card(AHORA), nota, AHORA).stability, esperada)

    def test_dificultad_inicial(self) -> None:
        d = review(new_card(AHORA), 3, AHORA).difficulty
        self.assertAlmostEqual(d, W[4] - math.exp(W[5] * 2) + 1, places=4)
        self.assertTrue(1 <= d <= 10)
        # «Fácil» daría una dificultad negativa sin el tope: se queda en 1
        self.assertEqual(review(new_card(AHORA), 4, AHORA).difficulty, 1.0)

    def test_intervalo_con_090(self) -> None:
        card = review(new_card(AHORA), 3, AHORA)
        self.assertEqual(card.state, "repaso")
        self.assertEqual(card.due, AHORA + timedelta(days=2))  # S = 2,3065 → 2 días
        self.assertEqual(card.reps, 1)
        self.assertEqual(card.last_review, AHORA)

    def test_olvido_baja_estabilidad(self) -> None:
        card = repasos([3, 3, 3], AHORA)
        olvidada = review(card, 1, card.due)
        self.assertLess(olvidada.stability, card.stability)
        self.assertGreater(olvidada.difficulty, card.difficulty)
        self.assertEqual(olvidada.lapses, 1)
        self.assertEqual(olvidada.state, "reaprendiendo")

    def test_fallo_vuelve_hoy(self) -> None:
        self.assertEqual(review(new_card(AHORA), 1, AHORA).due, AHORA + timedelta(minutes=10))
        card = repasos([3, 3], AHORA)
        self.assertEqual(review(card, 1, card.due).due, card.due + timedelta(minutes=10))

    def test_retrievability_en_due(self) -> None:
        self.assertEqual(retrievability(new_card(AHORA), AHORA), 0.0)
        card = repasos([3, 3, 3], AHORA)
        self.assertAlmostEqual(retrievability(card, card.due), 0.9, delta=0.01)
        self.assertEqual(retrievability(card, card.last_review), 1.0)

    def test_dias_en_utc_con_cambio_de_hora(self) -> None:
        # El 25-10-2026 se atrasa la hora: 2 días de reloj son 49 h. Restar dos fechas con la misma zona en
        # Python ignora el cambio de hora, así que el intervalo se cuenta en UTC: vence 48 h después.
        antes = datetime(2026, 10, 24, 12, 0, tzinfo=MADRID)
        card = review(new_card(antes), 3, antes)
        self.assertEqual(card.due.astimezone(timezone.utc) - antes.astimezone(timezone.utc), timedelta(days=2))
        self.assertEqual(card.due.tzinfo, MADRID)

    def test_hora_sin_zona_falla(self) -> None:
        with self.assertRaises(ValueError):
            review(new_card(AHORA), 3, datetime(2026, 10, 17, 20, 5))
        with self.assertRaises(ValueError):
            review(new_card(AHORA), 5, AHORA)

    def test_contra_referencia(self) -> None:
        """Valores publicados en los tests de py-fsrs (tests/test_basic.py), con sus pasos de aprendizaje
        por defecto (1 min y 10 min) y sin fuzz, que es como los calcula la referencia."""
        pasos = {"learning_steps": (timedelta(minutes=1), timedelta(minutes=10)),
                 "relearning_steps": (timedelta(minutes=10),)}
        inicio = datetime(2022, 11, 29, 12, 30, tzinfo=timezone.utc)

        # test_review_card: Bien ×6, Otra vez ×2, Bien ×5 contestando al vencer
        card = new_card(inicio)
        intervalos = []
        for nota in [3] * 6 + [1] * 2 + [3] * 5:
            card = review(card, nota, card.due, **pasos)
            intervalos.append((card.due - card.last_review).days)
        self.assertEqual(intervalos, [0, 2, 11, 46, 163, 498, 0, 0, 2, 4, 7, 12, 21])

        # test_memo_state: Otra vez y cinco Bien a los días 0, 0, 1, 4, 12 y 33
        card = new_card(inicio)
        momento = inicio
        for nota, dias in zip((1, 3, 3, 3, 3, 3), (0, 0, 1, 3, 8, 21)):
            momento += timedelta(days=dias)
            card = review(card, nota, momento, **pasos)
        self.assertAlmostEqual(card.stability, 53.62691, delta=1e-4)
        self.assertAlmostEqual(card.difficulty, 6.3574867, delta=1e-4)

        # test_repeated_correct_reviews: diez «Fácil» seguidos dejan la dificultad en el mínimo
        card = new_card(inicio)
        for i in range(10):
            card = review(card, 4, inicio + timedelta(microseconds=i), **pasos)
        self.assertEqual(card.difficulty, 1.0)


if __name__ == "__main__":
    unittest.main()
