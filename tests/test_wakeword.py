"""Tests de la palabra de activación y la escucha "manos libres". Micrófono y Vosk FALSOS."""

import json
import sys
import threading
import time
import unittest

from arthur.core.config import ConfigError, config_from_dict
from arthur.voice.recorder import SAMPLE_RATE, Recorder
from arthur.voice.wakeword import WakeSwitch, WakeWordListener

ENDLESS_MIC = [sys.executable, "-c",
               "import sys,time\nwhile True:\n sys.stdout.buffer.write(b'\\0'*3200); sys.stdout.buffer.flush(); time.sleep(0.02)"]


class FakeRecognizer:
    """Devuelve una secuencia de "lo que ha oído", un trozo de audio cada vez."""

    def __init__(self, heard):
        self.heard, self.resets = list(heard), 0

    def AcceptWaveform(self, chunk):
        return True

    def Result(self):
        return json.dumps({"text": self.heard.pop(0) if self.heard else ""})

    def PartialResult(self):
        return json.dumps({"partial": ""})

    def Reset(self):
        self.resets += 1


def wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


class WakeWordTest(unittest.TestCase):
    def make(self, heard, idle=True):
        self.woken = []
        self.idle = idle
        listener = WakeWordListener(lambda: self.woken.append(1), is_idle=lambda: self.idle,
                                    recognizer_factory=lambda: FakeRecognizer(heard), command=ENDLESS_MIC)
        self.addCleanup(listener.stop)
        return listener

    def test_wakes_on_the_phrase_once(self):
        listener = self.make(["", "[unk]", "hey arthur", "hey arthur"])
        listener.start()
        self.assertTrue(wait(lambda: self.woken))
        time.sleep(0.3)
        self.assertEqual(self.woken, [1])  # la segunda vez está dentro del tiempo de espera

    def test_ignores_other_words(self):
        listener = self.make(["[unk]", "arthur", "hey"] * 5)
        listener.start()
        time.sleep(0.5)
        self.assertEqual(self.woken, [])

    def test_does_not_wake_while_busy_or_paused(self):
        listener = self.make(["hey arthur"] * 50, idle=False)
        listener.start()
        time.sleep(0.4)
        self.assertEqual(self.woken, [])  # ARTHUR ocupado (p. ej. diciendo su propio nombre)
        listener.pause()
        self.idle = True
        time.sleep(0.4)
        self.assertEqual(self.woken, [])
        listener.resume()
        self.assertTrue(wait(lambda: self.woken))

    def test_switch(self):
        switch = WakeSwitch()
        self.assertIn("no está configurada", switch.set(True))
        switch.listener = self.make([])
        self.assertIn("Dejo de escuchar", switch.set(False))
        self.assertTrue(switch.listener.paused.is_set())
        self.assertIn("Vuelvo a escuchar «hey arthur»", switch.set(True))
        self.assertFalse(switch.listener.paused.is_set())

    def test_config(self):
        self.assertFalse(config_from_dict({}).wake.enabled)
        for phrase in ("Hey Arthur", "héy arthur", "a b c d e", "hey; arthur", "arthur"):
            with self.subTest(phrase=phrase), self.assertRaises(ConfigError):
                config_from_dict({"wake": {"phrase": phrase}})
        self.assertEqual(config_from_dict({"wake": {"variants": ["arthur"]}}).wake.variants, ("arthur",))


def speech_mic(speech_seconds, silence_seconds):
    """Micrófono falso: silencio corto, voz (tono fuerte) y luego silencio, a ritmo real."""
    code = f"""
import struct, sys, time
loud = struct.pack('<800h', *([9000, -9000] * 400))
quiet = b'\\0' * 1600
for chunk in [quiet] * 4 + [loud] * {int(speech_seconds * 20)} + [quiet] * {int(silence_seconds * 20)}:
    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush(); time.sleep(0.05)
time.sleep(30)
"""
    return [sys.executable, "-c", code]


def noisy_mic(noise_amplitude, speech_seconds, after_seconds):
    """Micrófono con ruido de fondo constante (como un ventilador) + voz + vuelta al ruido."""
    code = f"""
import struct, sys, time
noise = struct.pack('<800h', *([{noise_amplitude}, -{noise_amplitude}] * 400))
loud = struct.pack('<800h', *([12000, -12000] * 400))
for chunk in [noise] * 8 + [loud] * {int(speech_seconds * 20)} + [noise] * {int(after_seconds * 20)}:
    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush(); time.sleep(0.05)
time.sleep(30)
"""
    return [sys.executable, "-c", code]


class HandsFreeTest(unittest.TestCase):
    def test_background_noise_is_not_speech(self):
        # Ruido de fondo de nivel ~0,09: por encima del umbral fijo (0,05) que hacía grabar 20 s.
        recorder = Recorder(command=noisy_mic(1200, 0.6, 5))
        recorder.start()
        start = time.monotonic()
        self.assertTrue(recorder.wait_for_end(silence=0.5))
        elapsed = time.monotonic() - start
        recorder.stop()
        self.assertGreater(recorder.speech_level, 0.05)  # se ha adaptado al ruido
        self.assertLess(elapsed, 2.5)                    # y ha cortado al acabar de hablar

    def test_detects_end_of_speech(self):
        recorder = Recorder(command=speech_mic(0.6, 3))
        recorder.start()
        start = time.monotonic()
        self.assertTrue(recorder.wait_for_end(silence=0.5))
        elapsed = time.monotonic() - start
        audio = recorder.stop()
        self.assertGreater(elapsed, 0.9)   # esperó a que acabara de hablar…
        self.assertLess(elapsed, 2.5)      # …y no se quedó escuchando de más
        self.assertGreater(len(audio), SAMPLE_RATE)

    def test_gives_up_if_you_say_nothing(self):
        recorder = Recorder(command=speech_mic(0, 5))
        recorder.start()
        self.assertFalse(recorder.wait_for_end(no_speech=0.5))
        recorder.stop()


if __name__ == "__main__":
    unittest.main()


class PhraseScoreTest(unittest.TestCase):
    def words(self, *items):
        return {"text": " ".join(w for w, *_ in items),
                "result": [{"word": w, "conf": c, "start": a, "end": b} for w, c, a, b in items]}

    def test_real_voice_passes_and_noise_does_not(self):
        from arthur.voice.wakeword import phrase_score

        phrases = ["hey arthur"]
        voice = self.words(("hey", 0.95, 1.0, 1.2), ("arthur", 0.9, 1.2, 1.6))
        self.assertEqual(phrase_score(voice, phrases)[:2], ("hey arthur", 0.9))
        self.assertAlmostEqual(phrase_score(voice, phrases)[2], 0.6)
        knock = self.words(("hey", 0.4, 2.0, 2.05), ("arthur", 0.5, 2.05, 2.1))
        self.assertEqual(phrase_score(knock, phrases)[1], 0.4)
        self.assertIsNone(phrase_score(self.words(("[unk]", 1, 0, 1)), phrases))

    def test_listener_discards_low_confidence_and_short_sounds(self):
        heard = [json.dumps(self.words(("hey", 0.3, 0, 0.3), ("arthur", 0.9, 0.3, 0.7))),  # poca seguridad
                 json.dumps(self.words(("hey", 0.9, 0, 0.1), ("arthur", 0.9, 0.1, 0.2))),  # demasiado corto
                 json.dumps(self.words(("hey", 0.9, 0, 0.3), ("arthur", 0.8, 0.3, 0.8)))]  # voz de verdad

        class Recognizer(FakeRecognizer):
            def Result(self):
                return self.heard.pop(0) if self.heard else "{}"

        woken = []
        listener = WakeWordListener(lambda: woken.append(1), is_idle=lambda: True,
                                    recognizer_factory=lambda: Recognizer(heard), command=ENDLESS_MIC)
        self.addCleanup(listener.stop)
        with self.assertLogs("arthur.voice.wakeword", "INFO") as logs:
            listener.start()
            self.assertTrue(wait(lambda: woken))
        self.assertEqual(woken, [1])
        self.assertEqual(sum("Descartado" in line for line in logs.output), 2)

    def test_config(self):
        with self.assertRaises(ConfigError):
            config_from_dict({"wake": {"min_confidence": 1.5}})


class WakeFixesTest(unittest.TestCase):
    def test_threshold_is_capped_even_if_you_talk_from_the_start(self):
        # Lo que pasó de verdad: voz desde el primer instante → umbral 0,7-1,6 y "no te he oído".
        recorder = Recorder(command=speech_mic(1.0, 3))
        recorder.start()
        recorder._noise_levels = [0.9] * 6  # como si los primeros 300 ms fueran ya voz
        recorder.speech_level = Recorder._threshold(0.9)
        self.assertLessEqual(recorder.speech_level, 0.2)
        recorder.stop()

    def test_noisy_room_above_old_cap_still_ends(self):
        # Lo que pasó de verdad: ruido de fondo por encima de 0,2 → «voz» continua y 16 s grabados.
        recorder = Recorder(command=noisy_mic(3000, 0.6, 5))  # ruido ~0,23
        recorder.start(noise_floor=0.23)
        start = time.monotonic()
        self.assertTrue(recorder.wait_for_end(silence=0.5))
        elapsed = time.monotonic() - start
        recorder.stop()
        self.assertGreater(recorder.speech_level, 0.23)
        self.assertLess(elapsed, 2.5)

    def test_single_noise_spike_is_not_speech(self):
        code = """
import struct, sys, time
quiet = struct.pack('<800h', *([100, -100] * 400))
loud = struct.pack('<800h', *([12000, -12000] * 400))
for chunk in ([quiet] * 5 + [loud]) * 10:
    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush(); time.sleep(0.05)
time.sleep(30)
"""
        recorder = Recorder(command=[sys.executable, "-c", code])
        recorder.start(noise_floor=0.01)
        self.assertFalse(recorder.wait_for_end(no_speech=2))  # golpes sueltos de 50 ms: no es voz
        recorder.stop()

    def test_voice_is_ignored_until_the_chime_has_played(self):
        # Lo que pasó de verdad: con el micro bien ajustado, el ding contaba como voz y cortaba enseguida.
        from arthur.voice.controller import VoiceController

        recorder = Recorder(command=speech_mic(5, 1))  # "voz" (el ding) desde el principio
        controller = VoiceController.__new__(VoiceController)
        controller.recorder = recorder
        controller.start = lambda **kw: (recorder.start(**kw), True)[1]
        controller.say, finished = (lambda text: None), []
        controller.finish = lambda: finished.append(recorder.stop())
        controller.cancel = lambda: finished.append(recorder.stop())
        played = []
        controller.hands_free(noise_floor=0.01, chime=lambda: (time.sleep(0.5), played.append(time.monotonic())))
        self.assertTrue(played)
        self.assertGreater(recorder._first_voice or 0, played[0])  # la voz cuenta solo después del ding

    def test_known_noise_floor_and_ignored_chime(self):
        recorder = Recorder(command=speech_mic(0.6, 3))
        recorder.start(initial=b"\x01\x00" * 100, noise_floor=0.02, ignore_seconds=10)
        self.assertEqual(recorder.speech_level, 0.05)
        time.sleep(1.2)
        self.assertIsNone(recorder._first_voice)  # dentro del tiempo ignorado (el ding), nada cuenta como voz
        audio = recorder.stop()
        self.assertTrue(audio.startswith(b"\x01\x00" * 100))  # el audio previo va delante

    def test_listener_measures_noise_and_keeps_preroll(self):
        listener = WakeWordListener(lambda: None, is_idle=lambda: True,
                                    recognizer_factory=lambda: FakeRecognizer([]), command=ENDLESS_MIC)
        listener.start()
        self.addCleanup(listener.stop)
        self.assertTrue(wait(lambda: listener.noise_floor is not None))
        self.assertEqual(listener.noise_floor, 0.0)  # el micro falso solo da silencio
        self.assertEqual(len(listener.preroll()), 3200 * 10)

    def test_wake_phrase_is_removed_from_the_request(self):
        from arthur.voice.controller import strip_wake_phrase
        self.assertEqual(strip_wake_phrase("Hey Arthur, ¿qué hora es?"), "¿qué hora es?")
        self.assertEqual(strip_wake_phrase("Arturo, abre Firefox"), "abre Firefox")
        self.assertEqual(strip_wake_phrase("¿Qué tengo mañana?"), "¿Qué tengo mañana?")
        self.assertEqual(strip_wake_phrase("Hey Arthur."), "")
        # Lo que pasó de verdad: Whisper oyó «Hey» como «El».
        self.assertEqual(strip_wake_phrase("El arthur, ¿cuánta memoria tengo?"), "¿cuánta memoria tengo?")
        self.assertEqual(strip_wake_phrase("Oh Arthur abre Firefox"), "abre Firefox")
        self.assertEqual(strip_wake_phrase("El tiempo de mañana"), "El tiempo de mañana")
