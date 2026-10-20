import threading
import unittest
from datetime import datetime, timedelta, timezone

from unittest import mock

from arthur.webchat import limits as limits_mod
from arthur.webchat.limits import Limits, RateLimiter, clave_cliente


class Reloj:
    """Reloj falso con zona horaria: así no dependemos de la hora real."""

    def __init__(self, t=None):
        self.t = t or datetime(2026, 10, 16, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def avanza(self, **kw):
        self.t += timedelta(**kw)


class LimitsEnvTest(unittest.TestCase):
    def test_defaults(self):
        l = Limits.from_env({})
        self.assertEqual((l.per_hour, l.sessions_per_10min, l.contacts_per_day,
                          l.workers_ai_per_day, l.hard_per_day), (20, 3, 3, 400, 600))

    def test_overrides_and_invalid(self):
        l = Limits.from_env({"ARTHUR_WEBCHAT_POR_HORA": "5", "ARTHUR_WEBCHAT_PASES": "abc",
                             "ARTHUR_WEBCHAT_CONTACTOS": "0", "ARTHUR_WEBCHAT_CF_DIA": "-2",
                             "ARTHUR_WEBCHAT_TOPE_DIA": "7"})
        self.assertEqual((l.per_hour, l.sessions_per_10min, l.contacts_per_day,
                          l.workers_ai_per_day, l.hard_per_day), (5, 3, 3, 400, 7))


class RateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.reloj = Reloj()
        self.rl = RateLimiter(Limits(), self.reloj)

    def test_messages_hour_window(self):
        for _ in range(20):
            self.assertTrue(self.rl.allow_message("1.1.1.1"))
        self.assertFalse(self.rl.allow_message("1.1.1.1"))
        self.assertTrue(self.rl.allow_message("2.2.2.2"))
        self.reloj.avanza(hours=1, seconds=1)
        self.assertTrue(self.rl.allow_message("1.1.1.1"))

    def test_sessions(self):
        for _ in range(3):
            self.assertTrue(self.rl.allow_session("ip"))
        self.assertFalse(self.rl.allow_session("ip"))
        self.reloj.avanza(minutes=10, seconds=1)
        self.assertTrue(self.rl.allow_session("ip"))

    def test_contacts_per_day(self):
        for _ in range(3):
            self.assertTrue(self.rl.allow_contact("ip"))
        self.assertFalse(self.rl.allow_contact("ip"))
        self.reloj.avanza(hours=24, seconds=1)
        self.assertTrue(self.rl.allow_contact("ip"))

    def test_reserve_answer_sequence_and_next_day(self):
        rl = RateLimiter(Limits(workers_ai_per_day=3, hard_per_day=5), self.reloj)
        self.assertEqual([rl.reserve_answer() for _ in range(7)],
                         ["workers-ai"] * 3 + ["local"] * 2 + ["fijo"] * 2)
        self.reloj.avanza(days=1)
        self.assertEqual(rl.reserve_answer(), "workers-ai")

    def test_hard_cap_day_changes_at_madrid_midnight(self):
        # 22:30 UTC de octubre = 00:30 en Madrid (CEST, UTC+2): ya es otro día para el tope duro.
        reloj = Reloj(datetime(2026, 10, 16, 21, 30, tzinfo=timezone.utc))
        rl = RateLimiter(Limits(workers_ai_per_day=5, hard_per_day=1), reloj)
        self.assertEqual(rl.reserve_answer(), "workers-ai")
        self.assertEqual(rl.reserve_answer(), "fijo")
        reloj.avanza(hours=1)  # 22:30 UTC = 00:30 del día 17 en Madrid
        self.assertEqual(rl.reserve_answer(), "workers-ai")

    def test_workers_ai_quota_renews_at_utc_midnight(self):
        # Cloudflare renueva la capa gratis a las 00:00 UTC, no a medianoche de Madrid: a las 00:30 de Madrid
        # (22:30 UTC) el cupo de Workers AI sigue gastado; a las 00:00 UTC vuelve.
        reloj = Reloj(datetime(2026, 10, 16, 21, 30, tzinfo=timezone.utc))
        rl = RateLimiter(Limits(workers_ai_per_day=1, hard_per_day=100), reloj)
        self.assertEqual(rl.reserve_answer(), "workers-ai")
        self.assertEqual(rl.reserve_answer(), "local")
        reloj.avanza(hours=1)   # 22:30 UTC: otro día en Madrid, el mismo en UTC
        self.assertEqual(rl.reserve_answer(), "local")
        reloj.avanza(hours=2)   # 00:30 UTC del 17
        self.assertEqual(rl.reserve_answer(), "workers-ai")

    def test_release_workers_ai_gives_slot_back(self):
        rl = RateLimiter(Limits(workers_ai_per_day=1, hard_per_day=5), self.reloj)
        self.assertEqual(rl.reserve_answer(), "workers-ai")
        rl.release_workers_ai()
        self.assertEqual(rl.reserve_answer(), "workers-ai")  # el total sigue contando
        self.assertEqual(rl.reserve_answer(), "local")

    def test_expired_ips_are_swept_when_table_is_big(self):
        with mock.patch.object(limits_mod, "MAX_IPS_POR_TABLA", 50):
            for i in range(60):
                self.rl.allow_message(f"ip{i}")
            self.reloj.avanza(hours=1, seconds=1)
            self.rl.allow_message("nueva")  # dispara el barrido
        self.assertEqual(list(self.rl._mensajes), ["nueva"])

    def test_sweep_keeps_active_ips(self):
        with mock.patch.object(limits_mod, "MAX_IPS_POR_TABLA", 5):
            self.rl.allow_message("vieja")
            self.reloj.avanza(minutes=50)
            for i in range(6):
                self.rl.allow_message(f"ip{i}")
            self.reloj.avanza(minutes=11)  # "vieja" caducó, las ip{i} no
            self.rl.allow_message("otra")
        self.assertNotIn("vieja", self.rl._mensajes)
        self.assertIn("ip0", self.rl._mensajes)

    def test_ipv6_same_64_shares_limit(self):
        rl = RateLimiter(Limits(per_hour=2), self.reloj)
        self.assertTrue(rl.allow_message("2001:db8:1:2::1"))
        self.assertTrue(rl.allow_message("2001:db8:1:2:ffff::9"))
        self.assertFalse(rl.allow_message("2001:db8:1:2:abcd::5"))
        self.assertTrue(rl.allow_message("2001:db8:1:3::1"))  # otro /64
        self.assertEqual(clave_cliente("1.2.3.4"), "1.2.3.4")
        self.assertEqual(clave_cliente("no-es-ip"), "no-es-ip")

    def test_ipv4_mapped_keeps_each_client_separate(self):
        self.assertEqual(clave_cliente("::ffff:1.2.3.4"), "1.2.3.4")
        self.assertNotEqual(clave_cliente("::ffff:1.2.3.4"), clave_cliente("::ffff:5.6.7.8"))
        rl = RateLimiter(Limits(per_hour=1), self.reloj)
        self.assertTrue(rl.allow_message("::ffff:1.2.3.4"))
        self.assertTrue(rl.allow_message("::ffff:5.6.7.8"))
        self.assertFalse(rl.allow_message("1.2.3.4"))  # mismo cliente que el mapeado

    def test_sweep_is_not_run_on_every_call(self):
        with mock.patch.object(limits_mod, "MAX_IPS_POR_TABLA", 10), \
                mock.patch.object(RateLimiter, "_barrer", wraps=RateLimiter._barrer) as barrer:
            for i in range(40):  # todas vivas: el barrido no libera nada
                self.rl.allow_message(f"10.0.0.{i}")
            # 1.º al pasar el umbral (11 claves); luego solo al duplicar (22, 44>40).
            self.assertEqual(barrer.call_count, 2)
            self.reloj.avanza(seconds=61)
            self.rl.allow_message("10.1.1.1")
            self.assertEqual(barrer.call_count, 3)  # pasó el intervalo

    def test_concurrent_reserve(self):
        rl = RateLimiter(Limits(workers_ai_per_day=4, hard_per_day=10), self.reloj)
        res = []
        def tarea():
            res.append(rl.reserve_answer())
        hilos = [threading.Thread(target=tarea) for _ in range(50)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(len([r for r in res if r != "fijo"]), 10)
        self.assertEqual(res.count("workers-ai"), 4)


if __name__ == "__main__":
    unittest.main()
