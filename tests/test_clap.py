"""Doble palmada: audio SINTÉTICO (nunca el micrófono real)."""

import array
import math
import random
import sys
import time
import unittest

from arthur.core.config import ConfigError, config_from_dict
from arthur.voice.clap import ClapDetector, chunk_stats
from arthur.voice.wakeword import WakeWordListener

CHUNK = 1600  # 100 ms a 16 kHz


def silence(noise=30):
    rng = random.Random(1)
    return array.array("h", [rng.randint(-noise, noise) for _ in range(CHUNK)]).tobytes()


def clap(level=20000):
    """Un golpe seco: ~5 ms de pico que se apaga enseguida, como una palmada."""
    a = array.array("h", [0] * CHUNK)
    for i in range(80):
        a[i] = int(level * math.exp(-i / 15) * (1 if i % 2 == 0 else -1))
    return a.tobytes()


def speech(level=6000):
    """Voz o música: fuerte pero sostenida (cresta baja)."""
    return array.array("h", [int(level * math.sin(i / 7) + level / 2 * math.sin(i / 3)) for i in range(CHUNK)]).tobytes()


def fires(sequence, **kw):
    detector = ClapDetector(**kw)
    return [i for i, chunk in enumerate(sequence) if detector.feed(chunk)]


S, C = silence(), clap()


class ClapDetectorTest(unittest.TestCase):
    def test_two_claps_fire_once_after_a_short_calm(self):
        self.assertEqual(fires([S] * 3 + [C, S, S, C] + [S] * 8), [11])  # 0,5 s después de la segunda

    def test_a_single_clap_or_two_far_apart_do_nothing(self):
        self.assertEqual(fires([S] * 3 + [C] + [S] * 15), [])
        self.assertEqual(fires([C] + [S] * 14 + [C] + [S] * 10), [])  # 1,5 s entre ellas

    def test_claps_too_close_are_one_knock(self):
        self.assertEqual(fires([S, C, C] + [S] * 10), [])  # el mismo golpe repartido en dos trozos

    def test_applause_is_ignored_however_long(self):
        self.assertEqual(fires([S, C, S, C, S, C] + [S] * 12), [])
        self.assertEqual(fires([S, C, S, C, S, C, S, C, S, C] + [S] * 12), [])

    def test_speech_and_music_never_count(self):
        self.assertEqual(fires([speech()] * 30), [])
        self.assertEqual(fires([S, speech(), S, speech(), S] * 3), [])

    def test_a_quiet_clap_needs_the_configured_peak(self):
        quiet = clap(level=4000)  # pico ≈ 0,12
        self.assertEqual(fires([S, quiet, S, S, quiet] + [S] * 8), [])
        self.assertEqual(fires([S, quiet, S, S, quiet] + [S] * 8, min_peak=0.1), [9])

    def test_can_fire_again_later(self):
        self.assertEqual(len(fires(([S] * 3 + [C, S, S, C] + [S] * 10) * 2)), 2)

    def test_stats_are_numbers_only(self):
        peak, crest = chunk_stats(C)
        self.assertGreater(peak, 0.5)
        self.assertGreater(crest, 6)
        self.assertEqual(chunk_stats(b""), (0.0, 0.0))


class ListenerWithClapTest(unittest.TestCase):
    def test_double_clap_wakes_the_listener(self):
        # Un «micrófono» falso que emite silencio, dos palmadas y silencio, sin Vosk de verdad.
        script = (
            "import sys,time,array,math\n"
            "S=array.array('h',[0]*1600).tobytes()\n"
            "a=array.array('h',[0]*1600)\n"
            "for i in range(80): a[i]=int(20000*math.exp(-i/15)*(1 if i%2==0 else -1))\n"
            "C=a.tobytes()\n"
            "for c in [S,S,C,S,S,C]+[S]*10:\n"
            " sys.stdout.buffer.write(c); sys.stdout.buffer.flush(); time.sleep(0.02)\n"
            "time.sleep(5)\n")

        class NoWords:
            def AcceptWaveform(self, chunk):
                return False

        woken = []
        listener = WakeWordListener(lambda: woken.append(1), is_idle=lambda: True, recognizer_factory=NoWords,
                                    command=[sys.executable, "-c", script], clap=ClapDetector())
        self.addCleanup(listener.stop)
        listener.start()
        deadline = time.monotonic() + 3
        while not woken and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(woken, [1])

    def test_config_option(self):
        self.assertFalse(config_from_dict({}).wake.double_clap)  # apagada por defecto: cada usuario la calibra y la activa
        self.assertTrue(config_from_dict({"wake": {"double_clap": True}}).wake.double_clap)
        with self.assertRaises(ConfigError):
            config_from_dict({"wake": {"clap_peak": 0.01}})


if __name__ == "__main__":
    unittest.main()
