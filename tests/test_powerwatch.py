"""Perfil de energía: todo FALSO (nunca se llama a powerprofilesctl ni a notify-send reales)."""

import unittest

from arthur.core.config import Config, ConfigError, config_from_dict
from arthur.core.powerwatch import PERFORMANCE, SAVER, SAVER_MESSAGE, PowerWatcher


class Fake:
    """Un portátil de mentira: perfil actual y enchufe que el test cambia a mano."""

    def __init__(self, profile="balanced", ac=False, auto=True, fail=False):
        self.profile, self.ac, self.fail, self.sent, self.set_calls = profile, ac, fail, [], []
        self.watcher = PowerWatcher(lambda: self.profile, self.sent.append, read_ac=lambda: self.ac,
                                    set_profile=self.set if auto else None)

    def set(self, profile):
        if self.fail:
            raise RuntimeError("polkit dice que no")
        self.set_calls.append(profile)
        self.profile = profile

    def check(self):
        self.watcher.check()


class SaverWarningTest(unittest.TestCase):
    def test_warns_once_per_entry_into_saver(self):
        f = Fake(profile=SAVER, auto=False)
        f.check()
        f.check()
        f.profile = "balanced"
        f.check()
        f.profile = SAVER
        f.check()
        self.assertEqual(f.sent, [SAVER_MESSAGE, SAVER_MESSAGE])  # al arrancar y al volver a entrar; no cada minuto

    def test_silent_when_not_saver_or_unknown(self):
        f = Fake(profile="balanced", auto=False)
        f.check()
        f.profile = None
        f.check()
        self.assertEqual(f.sent, [])


class FollowThePlugTest(unittest.TestCase):
    def test_plugging_in_sets_performance_and_unplugging_restores(self):
        f = Fake(profile="balanced", ac=False)
        f.check()
        self.assertEqual(f.set_calls, [])
        f.ac = True
        f.check()
        self.assertEqual((f.set_calls, f.profile), ([PERFORMANCE], PERFORMANCE))
        f.check()
        f.check()
        self.assertEqual(f.set_calls, [PERFORMANCE])  # una vez por enchufe, no cada minuto
        f.ac = False
        f.check()
        self.assertEqual((f.set_calls, f.profile), ([PERFORMANCE, "balanced"], "balanced"))

    def test_started_while_plugged_in(self):
        f = Fake(profile=SAVER, ac=True)
        f.check()
        self.assertEqual(f.profile, PERFORMANCE)
        self.assertNotIn(SAVER_MESSAGE, f.sent)  # ya no está en ahorro: no hay nada que avisar

    def test_restoring_saver_is_not_announced_as_news(self):
        f = Fake(profile=SAVER, ac=True)
        f.check()
        f.ac = False
        f.check()
        self.assertEqual(f.profile, SAVER)
        self.assertEqual(f.sent.count(SAVER_MESSAGE), 0)

    def test_does_not_fight_a_manual_choice(self):
        f = Fake(profile="balanced", ac=True)
        f.check()
        f.profile = SAVER  # el usuario elige otro estando enchufado
        f.check()
        self.assertEqual(f.set_calls, [PERFORMANCE])
        self.assertEqual(f.profile, SAVER)

    def test_unplugging_keeps_a_profile_it_did_not_change(self):
        f = Fake(profile=PERFORMANCE, ac=True)  # ya estaba en rendimiento por decisión tuya
        f.check()
        f.ac = False
        f.check()
        self.assertEqual((f.set_calls, f.profile), ([], PERFORMANCE))

    def test_failure_to_set_is_survived_and_not_retried_every_minute(self):
        f = Fake(profile="balanced", ac=True, fail=True)
        f.check()
        f.check()
        self.assertEqual(f.profile, "balanced")

    def test_disabled_or_unknown_charger_changes_nothing(self):
        f = Fake(profile="balanced", ac=True, auto=False)
        f.check()
        self.assertEqual(f.set_calls, [])
        g = Fake(profile="balanced", ac=None)
        g.check()
        self.assertEqual(g.set_calls, [])

    def test_config_option(self):
        self.assertTrue(Config().power.auto_performance)
        self.assertFalse(config_from_dict({"power": {"auto_performance": False}}).power.auto_performance)
        with self.assertRaises(ConfigError):
            config_from_dict({"power": {"auto_performance": "si"}})


if __name__ == "__main__":
    unittest.main()
