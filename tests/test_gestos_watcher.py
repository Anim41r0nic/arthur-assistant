"""Tests del vigilante de gestos y de `set_gestures` (paso 4 de docs/gestos.md).

Sin cámara ni procesos: el «proceso de la cámara» es un objeto FALSO al que se le inyectan líneas, y el reloj
también es falso (los segundos pasan cuando el test quiere).
"""

import tempfile
import unittest
from pathlib import Path

from arthur.core.config import GesturesConfig
from arthur.core.events import EventBus
from arthur.interfaces.gestos.protocol import GestureLine, StatusLine
from arthur.interfaces.gestos.watcher import COOLDOWN, GestureSwitch, GestureWatcher, screen_locked
from arthur.interfaces.gestos.worker import WorkerUnavailable
from arthur.tools.base import Risk, ToolCall
from tests.helpers import ScriptedConfirmer, make_router


class FakeWorker:
    """Hace de proceso de la cámara: no lanza nada; el test le hace «decir» líneas."""

    def __init__(self, on_message, on_exit, *, fail=None):
        self.on_message, self.on_exit, self.fail = on_message, on_exit, fail
        self.started = self.stopped = 0

    def start(self):
        if self.fail:
            raise self.fail
        self.started += 1

    def stop(self):
        self.stopped += 1


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class WatcherTestCase(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe("camera_state", lambda e: self.events.append(("camera_state", e.data)))
        self.bus.subscribe("gesture", lambda e: self.events.append(("gesture", e.data)))
        self.clock = Clock()
        self.workers = []
        self.locked = False
        self.watcher = self.make()

    def make(self, fail=None, **config):
        def factory(on_message, on_exit):
            worker = FakeWorker(on_message, on_exit, fail=fail)
            self.workers.append(worker)
            return worker

        cfg = GesturesConfig(**{"enabled": True, **config})
        return GestureWatcher(self.bus, cfg, worker_factory=factory, clock=self.clock,
                              is_locked=lambda: self.locked, tick_seconds=0)

    @property
    def worker(self):
        return self.workers[-1]

    def feed(self, gesture, seconds, *, score=0.9, step=0.125):
        """El proceso de la cámara manda `gesture` durante `seconds` (8 fotogramas por segundo)."""
        for _ in range(round(seconds / step) + 1):
            self.worker.on_message(GestureLine(gesture, score))
            self.clock.now += step
        self.clock.now -= step

    def gestures(self):
        return [data["name"] for kind, data in self.events if kind == "gesture"]

    def states(self):
        return [(data["on"], data["reason"]) for kind, data in self.events if kind == "camera_state"]


class OnOffTest(WatcherTestCase):
    def test_starts_off_and_only_turns_on_when_asked(self):
        self.assertFalse(self.watcher.active)
        self.assertEqual(self.workers, [])  # crear el vigilante no lanza nada
        self.watcher.tick()
        self.assertEqual(self.workers, [])
        self.assertIn("encendida", self.watcher.turn_on("orb"))
        self.assertTrue(self.watcher.active)
        self.assertEqual(self.worker.started, 1)
        self.assertEqual(self.states(), [(True, "orb")])

    def test_disabled_in_config_never_starts(self):
        watcher = self.make(enabled=False)
        self.assertIn("enabled = true", watcher.turn_on("orb"))
        self.assertFalse(watcher.active)
        self.assertEqual(self.workers, [])
        self.assertEqual(self.states(), [])

    def test_turning_on_twice_and_off_twice(self):
        self.watcher.turn_on("voz")
        self.assertIn("ya está encendida", self.watcher.turn_on("orb"))
        self.assertEqual(len(self.workers), 1)
        self.assertIn("apagada", self.watcher.turn_off("orb"))
        self.assertIn("ya estaba apagada", self.watcher.turn_off("voz"))
        self.assertEqual(self.worker.stopped, 1)
        self.assertEqual(self.states(), [(True, "voz"), (False, "orb")])

    def test_a_worker_that_cannot_start_leaves_it_off(self):
        watcher = self.make(fail=WorkerUnavailable("bubblewrap (bwrap) no está instalado"))
        with self.assertLogs("arthur.interfaces.gestos.watcher", "WARNING"):
            self.assertIn("bwrap", watcher.turn_on("orb"))
        self.assertFalse(watcher.active)
        self.assertEqual(self.states(), [])

    def test_unknown_reasons_are_a_bug(self):
        with self.assertRaises(ValueError):
            self.watcher.turn_on("correo")
        with self.assertRaises(ValueError):
            self.watcher.turn_off("porque sí")

    def test_turns_off_after_idle_minutes_without_gestures(self):
        self.watcher.turn_on("orb")
        self.clock.now += 4 * 60
        self.feed("palma", 1.0)  # un gesto reinicia la cuenta
        self.clock.now += 4 * 60 + 59
        self.watcher.tick()
        self.assertTrue(self.watcher.active)
        self.feed(None, 1.0)  # ver la mano sin gesto (o nada) no cuenta como actividad
        self.clock.now += 1
        self.watcher.tick()
        self.assertFalse(self.watcher.active)
        self.assertEqual(self.states()[-1], (False, "inactividad"))
        self.assertEqual(self.worker.stopped, 1)

    def test_turns_off_when_the_screen_is_locked(self):
        self.watcher.turn_on("orb")
        self.locked = True
        self.watcher.tick()
        self.assertFalse(self.watcher.active)
        self.assertEqual(self.states()[-1], (False, "bloqueo"))

    def test_turns_off_when_the_camera_process_complains_or_dies(self):
        for message, reason in ((StatusLine("no-camera"), "sin-camara"), (StatusLine("no-detector"), "sin-detector"),
                                (StatusLine("error"), "fallo")):
            with self.subTest(reason=reason):
                self.watcher.turn_on("orb")
                self.worker.on_message(message)
                self.assertFalse(self.watcher.active)
                self.assertEqual(self.states()[-1], (False, reason))
        self.watcher.turn_on("orb")
        self.worker.on_message(StatusLine("ready"))
        self.assertTrue(self.watcher.active)
        self.worker.on_exit(1)
        self.assertEqual(self.states()[-1], (False, "fallo"))

    def test_an_old_process_cannot_touch_the_new_one(self):
        self.watcher.turn_on("orb")
        old = self.worker
        self.watcher.turn_off("orb")
        self.watcher.turn_on("orb")
        old.on_message(StatusLine("no-camera"))
        old.on_exit(1)
        for _ in range(20):
            old.on_message(GestureLine("palma", 1.0))
            self.clock.now += 0.125
        self.assertTrue(self.watcher.active)
        self.assertEqual(self.gestures(), [])

    def test_switch(self):
        switch = GestureSwitch()
        self.assertIn("servicio", switch.set(True))
        switch.watcher = self.watcher
        switch.set(True, by="orb")
        switch.set(False, by="orb")
        self.assertEqual(self.states(), [(True, "orb"), (False, "orb")])

    def test_switch_toggle_for_the_shortcut(self):
        switch = GestureSwitch()
        self.assertIn("servicio", switch.toggle())
        switch.watcher = self.watcher
        self.assertIn("encendida", switch.toggle())
        self.assertIn("apagada", switch.toggle())
        self.assertEqual(self.states(), [(True, "atajo"), (False, "atajo")])


class HoldAndCooldownTest(WatcherTestCase):
    def setUp(self):
        super().setUp()
        self.watcher.turn_on("orb")

    def test_a_gesture_counts_after_holding_it_one_second(self):
        self.feed("palma", 0.875)
        self.assertEqual(self.gestures(), [])
        self.feed("palma", 0.125)
        self.assertEqual(self.gestures(), ["palma"])

    def test_holding_longer_does_not_repeat_it(self):
        self.feed("palma", 6.0)
        self.assertEqual(self.gestures(), ["palma"])

    def test_three_second_pause_between_gestures(self):
        self.feed("palma", 1.0)
        self.feed(None, 0.25)
        self.feed("pulgar_abajo", 1.5)  # empieza dentro de la pausa: no cuenta
        self.assertEqual(self.gestures(), ["palma"])
        self.clock.now += COOLDOWN
        self.feed("pulgar_abajo", 1.0)
        self.assertEqual(self.gestures(), ["palma", "pulgar_abajo"])

    def test_one_weak_frame_does_not_let_go(self):
        """Con la mano real salen fotogramas flojos sueltos en medio de una palma clara (prueba real, 0.35.3)."""
        self.feed("palma", 0.5)
        self.feed("palma", 0.25, score=0.6)  # flojos: por debajo de min_score
        self.feed(None, 0.125)
        self.feed("palma", 0.25)
        self.assertEqual(self.gestures(), ["palma"])

    def test_a_thumb_down_seen_in_bursts_still_counts(self):
        """Secuencia real de la burbuja (prueba real, 0.35.4): 👎 claro a ráfagas, con huecos de 0,5-0,8 s sin verlo."""
        start = self.clock.now
        for t in (0.0, 0.2, 0.7, 0.9, 1.5, 1.7):  # instantes con 👎 ≥ 0,94; entre medias, «ninguno»
            self.clock.now = start + t
            self.worker.on_message(GestureLine("pulgar_abajo", 0.96))
            self.clock.now += 0.1
            self.worker.on_message(GestureLine(None, 0.0))
        self.assertEqual(self.gestures(), ["pulgar_abajo"])

    def test_two_loose_frames_are_not_a_gesture(self):
        self.feed("palma", 0.0)
        self.feed(None, 0.875)
        self.feed("palma", 0.0)  # 1 s después del primero, pero solo dos lecturas claras
        self.assertEqual(self.gestures(), [])

    def test_turning_off_logs_what_the_camera_saw(self):
        self.feed(None, 0.25)
        self.feed("pulgar_abajo", 0.25, score=0.6)
        self.feed("palma", 0.25)
        with self.assertLogs("arthur.interfaces.gestos.watcher", "INFO") as logs:
            self.watcher.turn_off("orb")
        self.assertIn("9 lecturas; palma 3 ≥0.8 (máx 0.90), pulgar_abajo 0 ≥0.8 (máx 0.60)", logs.output[-1])

    def test_motion_gestures_count_at_once_but_respect_the_pause(self):
        self.feed("saludo", 0.0)
        self.assertEqual(self.gestures(), ["saludo"])
        self.clock.now += 1.0
        self.feed("mover_lateral", 0.0)  # dentro de la pausa de 3 s: no cuenta
        self.assertEqual(self.gestures(), ["saludo"])
        self.clock.now += COOLDOWN
        self.feed("mover_lateral", 0.0)
        self.assertEqual(self.gestures(), ["saludo", "mover_lateral"])

    def test_yes_gestures_need_a_longer_hold(self):
        self.feed("pulgar_arriba", 1.25)
        self.assertEqual(self.gestures(), [])
        self.feed("pulgar_arriba", 0.25)
        self.assertEqual(self.gestures(), ["pulgar_arriba"])

    def test_letting_go_or_changing_gesture_restarts_the_count(self):
        self.feed("palma", 0.75)
        self.feed(None, 1.25)  # más de MAX_GAP sin verla clara: soltada
        self.feed("palma", 0.75)
        self.feed("pulgar_abajo", 0.75)
        self.assertEqual(self.gestures(), [])

    def test_a_gap_in_the_readings_restarts_the_count(self):
        self.feed("palma", 0.5)
        self.clock.now += 1.25  # la cámara dejó de mandar lecturas un rato (o se ignoraron)
        self.feed("palma", 0.5)
        self.assertEqual(self.gestures(), [])

    def test_low_confidence_does_not_count(self):
        self.feed("palma", 2.0, score=0.79)
        self.assertEqual(self.gestures(), [])
        watcher = self.make(min_score=0.5)
        watcher.turn_on("orb")
        self.feed("palma", 1.0, score=0.6)
        self.assertEqual(self.gestures(), ["palma"])

    def test_configurable_hold(self):
        self.watcher.turn_off("orb")
        watcher = self.make(hold_seconds=2.0)
        watcher.turn_on("orb")
        self.feed("palma", 1.5)
        self.assertEqual(self.gestures(), [])
        self.feed("palma", 0.5)
        self.assertEqual(self.gestures(), ["palma"])

    def test_after_turning_off_nothing_counts(self):
        worker = self.worker
        self.watcher.turn_off("orb")
        for _ in range(20):
            worker.on_message(GestureLine("palma", 1.0))
            self.clock.now += 0.125
        self.assertEqual(self.gestures(), [])


class RealThreadsTest(unittest.TestCase):
    """Con hilos de verdad y un proceso FALSO (un script de Python que solo escribe una línea)."""

    def wait_for(self, condition, timeout=10):
        import time

        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def test_the_ticker_turns_it_off_by_itself(self):
        bus = EventBus()
        watcher = GestureWatcher(bus, GesturesConfig(enabled=True),
                                 worker_factory=lambda m, e: FakeWorker(m, e), is_locked=lambda: True,
                                 tick_seconds=0.01)
        watcher.turn_on("orb")
        self.assertTrue(self.wait_for(lambda: not watcher.active))

    def test_whole_path_with_a_fake_camera_process(self):
        import sys

        from arthur.interfaces.gestos.worker import GestureWorker

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "sin_detector.py"
        script.write_text('print(\'{"status": "no-detector"}\', flush=True)\n')
        bus = EventBus()
        states = []
        bus.subscribe("camera_state", lambda e: states.append((e.data["on"], e.data["reason"])))
        watcher = GestureWatcher(
            bus, GesturesConfig(enabled=True), tick_seconds=0, is_locked=lambda: False,
            worker_factory=lambda m, e: GestureWorker(m, on_exit=e, command=[sys.executable, "-I", str(script)]))
        self.addCleanup(watcher.turn_off, "servicio")
        watcher.turn_on("orb")
        self.assertTrue(self.wait_for(lambda: len(states) == 2))
        self.assertEqual(states, [(True, "orb"), (False, "sin-detector")])
        self.assertFalse(watcher.active)


class ScreenLockedTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name)

    def process(self, pid, name):
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "comm").write_text(name + "\n")

    def test_hyprlock_running_means_locked(self):
        self.process(10, "firefox")
        self.assertFalse(screen_locked(self.proc, run=lambda argv: "no\n"))
        self.process(11, "hyprlock")
        self.assertTrue(screen_locked(self.proc, run=lambda argv: "no\n"))

    def test_logind_locked_hint(self):
        calls = []

        def run(argv):
            calls.append(argv)
            return "yes\n"

        self.assertTrue(screen_locked(self.proc, run=run))
        self.assertEqual(calls, [["loginctl", "show-session", "auto", "-p", "LockedHint", "--value"]])
        self.assertFalse(screen_locked(self.proc, run=lambda argv: ""))


class SetGesturesToolTest(unittest.TestCase):
    def setUp(self):
        from arthur.tools.gestures import build_gesture_tools

        self.switch = GestureSwitch()
        self.calls = []

        class FakeWatcher:
            def turn_on(inner, by):
                self.calls.append(("on", by))
                return "Cámara encendida"

            def turn_off(inner, by):
                self.calls.append(("off", by))
                return "Cámara apagada"

        self.switch.watcher = FakeWatcher()
        self.tool = build_gesture_tools(self.switch)[0]

    def test_turning_on_is_private_and_off_is_low(self):
        self.assertEqual(self.tool.risk, Risk.PRIVATE)
        self.assertEqual(self.tool.effective_risk({"enabled": True}), Risk.PRIVATE)
        self.assertEqual(self.tool.effective_risk({"enabled": False}), Risk.LOW)

    def test_turning_on_always_asks_even_without_confirm_writes(self):
        confirmer = ScriptedConfirmer(False)
        router = make_router([self.tool], confirmer=confirmer, confirm_writes=False)
        result = router.execute(ToolCall("set_gestures", {"enabled": True}))
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.calls, [])
        self.assertIn("ENCENDER LA CÁMARA", confirmer.requests[0].summary)
        confirmer.answers.append(True)
        self.assertEqual(router.execute(ToolCall("set_gestures", {"enabled": True})).status, "ok")
        self.assertEqual(self.calls, [("on", "voz")])

    def test_turning_off_does_not_ask(self):
        confirmer = ScriptedConfirmer()
        router = make_router([self.tool], confirmer=confirmer)
        self.assertEqual(router.execute(ToolCall("set_gestures", {"enabled": False})).status, "ok")
        self.assertEqual(confirmer.requests, [])
        self.assertEqual(self.calls, [("off", "voz")])

    def test_only_a_boolean(self):
        router = make_router([self.tool])
        for args in ({"enabled": "true"}, {"enabled": 1}, {}, {"enabled": True, "camera": "/dev/video1"}):
            with self.subTest(args=args):
                self.assertEqual(router.execute(ToolCall("set_gestures", args)).status, "error")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
