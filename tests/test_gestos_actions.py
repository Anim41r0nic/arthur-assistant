"""Tests de lo que hacen los gestos y de su conexión con el servicio (paso 5 de docs/gestos.md).

Sin cámara, sin música de verdad y sin modelo: el reproductor es una herramienta `media` FALSA, la voz es un objeto
falso y el proceso de la cámara también.
"""

import ast
import dataclasses
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from arthur.app import build_app
from arthur.core.config import BrainConfig, Config, GesturesConfig
from arthur.core.events import Event, EventBus
from arthur.interfaces.gestos.actions import ACTIONS, GestureActions
from arthur.interfaces.gestos.watcher import GESTURE_HELP, GestureSwitch
from arthur.interfaces.gestos.names import Gesture
from arthur.interfaces.gestos.protocol import ALLOWED_GESTURES
from arthur.interfaces.service import run_service
from arthur.security.audit import AuditLog
from arthur.security.broker import ConfirmationBroker
from arthur.security.confirm import ConfirmationRequest, DenyAll
from arthur.tools.base import Param, Risk, Tool

from .helpers import make_router


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


class GestureActionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audit_path = Path(tmp.name) / "audit.jsonl"
        self.bus = EventBus()
        self.seen = []
        self.bus.subscribe("*", lambda e: self.seen.append(e.type))
        self.media_calls = []
        media = Tool("media", "", Risk.LOW, run=lambda a: self.media_calls.append(a["action"]) or "Pausado",
                     params=(Param("action", str, ""),))
        audit = AuditLog(self.audit_path)
        self.router = make_router([media], audit=audit, bus=self.bus)
        self.stops = []
        self.broker = ConfirmationBroker(self.bus, timeout=5, has_clients=lambda: True)
        self.repeated, self.orbs = [], []
        self.actions = GestureActions(self.bus, router=self.router, audit=audit,
                                      stop_speaking=lambda: self.stops.append(1), broker=lambda: self.broker,
                                      repeat=lambda text: self.repeated.append(text) or True,
                                      show_orb=lambda: self.orbs.append(1),
                                      hide_orb=lambda: self.orbs.append(0),
                                      clock=lambda: datetime(2026, 10, 11, 16, 30))

    def audit(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]

    def gesture(self, name):
        self.bus.publish(Event("gesture", {"name": name}))

    def test_the_table(self):
        self.assertEqual(ACTIONS, {
            Gesture.PALM: "calla", Gesture.THUMB_DOWN: "no", Gesture.FIST: "no", Gesture.THUMB_UP: "si",
            Gesture.OK: "si", Gesture.INDEX_UP: "repite", Gesture.TWO_FINGERS: "pausa_play",
            Gesture.WAVE: "hola"})
        # lo que viaja por la tubería es EXACTAMENTE lo que tiene acción: ni gestos mudos ni acciones sin gesto
        self.assertEqual(set(ALLOWED_GESTURES), {g.value for g in ACTIONS})

    def test_the_orb_is_told_what_each_gesture_did(self):
        shown = []
        self.bus.subscribe("gesture_action", lambda e: shown.append(e.data))
        self.repeated.clear()
        self.actions.repeat = lambda text: False  # ARTHUR ocupado: el 👋 se ve, pero no saluda
        self.gesture("saludo")
        self.gesture("palma")
        self.assertEqual(shown[0], {"gesture": "saludo", "action": "hola", "status": "ok",
                                    "detail": "ARTHUR está ocupado: no saluda"})
        self.assertEqual((shown[1]["gesture"], shown[1]["action"]), ("palma", "calla"))

    def ask(self, risk, tool="create_event"):
        answers = []
        thread = threading.Thread(target=lambda: answers.append(
            self.broker.ask(ConfirmationRequest(tool, risk, "Algo"))))
        thread.start()
        self.assertTrue(wait_for(lambda: len(self.broker.pending()) >= 1))
        return thread, answers

    def test_thumbs_up_and_ok_say_yes_to_low_or_write_risk(self):
        for name, risk in (("pulgar_arriba", Risk.WRITE), ("circulo", Risk.LOW)):
            with self.subTest(name=name):
                thread, answers = self.ask(risk)
                self.gesture(name)
                thread.join(5)
                self.assertEqual(answers, [True])
                self.assertEqual(self.audit()[-1]["detail"], "Confirmación aceptada")

    def test_yes_never_accepts_private_or_dangerous(self):
        for risk in (Risk.PRIVATE, Risk.DANGEROUS):
            with self.subTest(risk=risk):
                thread, answers = self.ask(risk)
                self.gesture("pulgar_arriba")
                self.assertIn("usa el clic del Orb", self.audit()[-1]["detail"])
                self.assertEqual(len(self.broker.pending()), 1)  # sigue abierta: decide el clic
                self.gesture("puno")  # ✊ también es «No»
                thread.join(5)
                self.assertEqual(answers, [False])

    def test_yes_does_not_choose_between_several_questions(self):
        first, a = self.ask(Risk.WRITE)
        second, b = self.ask(Risk.WRITE, tool="save_contact")
        self.assertTrue(wait_for(lambda: len(self.broker.pending()) == 2))
        self.gesture("pulgar_arriba")
        self.assertIn("varias confirmaciones", self.audit()[-1]["detail"])
        self.gesture("pulgar_abajo")
        first.join(5), second.join(5)
        self.assertEqual((a, b), ([False], [False]))

    def test_yes_without_an_open_question_does_nothing(self):
        self.gesture("circulo")
        self.assertEqual(self.audit()[-1]["detail"], "Sin confirmación abierta: no se ha hecho nada")

    def test_index_up_repeats_the_last_reply_without_thinking(self):
        self.gesture("indice_arriba")
        self.assertEqual(self.audit()[-1]["detail"], "Nada que repetir")
        self.bus.publish(Event("assistant_message", {"text": "Son las cinco"}))
        self.bus.publish(Event("assistant_message", {"text": "Mañana tienes clase", "announcement": True}))
        self.gesture("indice_arriba")
        self.assertEqual(self.repeated, ["Mañana tienes clase"])

    def test_index_up_while_busy_repeats_as_soon_as_arthur_is_free(self):
        """El usuario hizo ☝️ mientras ARTHUR aún le leía el calendario (0.37.2): ahora espera y lo repite."""
        busy = [True]
        done = threading.Event()

        def repeat(text):
            if busy[0]:
                return False
            self.repeated.append(text)
            done.set()
            return True

        self.actions.repeat = repeat
        self.actions.sleep = lambda s: busy.__setitem__(0, False)  # en la primera espera, ARTHUR acaba de hablar
        self.bus.publish(Event("assistant_message", {"text": "Mañana tienes clase"}))
        self.gesture("indice_arriba")
        self.assertIn("lo repetirá al quedar libre", self.audit()[-1]["detail"])
        self.assertTrue(done.wait(5))
        self.assertEqual(self.repeated, ["Mañana tienes clase"])

    def test_several_index_up_while_busy_repeat_only_once(self):
        release = threading.Event()
        self.actions.repeat = lambda text: False
        self.actions.sleep = lambda s: release.wait(5)
        self.bus.publish(Event("assistant_message", {"text": "Hola"}))
        self.gesture("indice_arriba")
        self.gesture("indice_arriba")
        self.assertEqual(self.audit()[-1]["detail"], "Ya hay una repetición esperando")
        release.set()

    def test_two_fingers_toggles_the_music(self):
        self.gesture("dos_dedos")
        self.assertEqual(self.media_calls, ["toggle"])
        self.assertEqual([e["arguments"]["action"] for e in self.audit() if e["tool"] == "gesture"], ["pausa_play"])

    def test_wave_says_hello(self):
        self.gesture("saludo")
        self.assertEqual(self.repeated, ["¡Buenas tardes! Aquí estoy."])
        self.assertEqual([e["detail"] for e in self.audit() if e["tool"] == "gesture"], ["Saludado"])

    def test_the_hide_orb_action_still_works_for_a_future_gesture(self):
        self.assertEqual(self.actions._hide_orb(), "Orb ocultado")
        self.assertEqual(self.orbs, [0])

    def test_the_orb_action_still_works_for_a_future_gesture(self):
        self.assertEqual(self.actions._orb(), "Orb a la vista")
        self.assertEqual(self.orbs, [1])

    def test_palm_cuts_the_voice_and_pauses_the_music_through_the_router(self):
        self.gesture("palma")
        self.assertEqual(self.stops, [1])
        self.assertEqual(self.media_calls, ["pause"])
        entries = self.audit()
        self.assertEqual([e["tool"] for e in entries], ["media", "gesture"])  # el router apunta la música
        self.assertEqual(entries[0]["arguments"], {"action": "pause"})
        self.assertEqual(entries[1]["arguments"], {"gesture": "palma", "action": "calla"})
        self.assertEqual(entries[1]["status"], "ok")
        self.assertEqual(oct(self.audit_path.stat().st_mode & 0o777), "0o600")

    def test_thumbs_down_says_no_to_the_open_confirmation(self):
        answers = []
        request = ConfirmationRequest("run_command", Risk.DANGEROUS, "Ejecutar: ls")
        thread = threading.Thread(target=lambda: answers.append(self.broker.ask(request)))
        thread.start()
        self.assertTrue(wait_for(lambda: self.broker.pending()))
        self.gesture("pulgar_abajo")
        thread.join(5)
        self.assertEqual(answers, [False])
        resolved = [e for e in self.audit() if e["tool"] == "gesture"]
        self.assertIn("rechazada (1)", resolved[0]["detail"])
        self.assertEqual(self.media_calls, [])

    def test_thumbs_down_without_an_open_question_does_nothing(self):
        self.gesture("pulgar_abajo")
        self.assertEqual(self.audit()[0]["detail"], "Sin confirmación abierta: no se ha hecho nada")
        actions = GestureActions(EventBus(), router=self.router, audit=AuditLog(None), stop_speaking=lambda: None)
        self.assertIn("Sin confirmación", actions._reject())  # sin Orb (sin broker), tampoco

    def test_unknown_or_silent_gestures_do_nothing(self):
        for name in ("pinza", "pulgar_menique", "mover_lateral", "puño", "", None, 3, "PALMA"):
            with self.subTest(name=name), self.assertLogs("arthur.interfaces.gestos.actions", "WARNING"):
                self.gesture(name)
        self.assertEqual((self.stops, self.media_calls, self.repeated, self.orbs, self.audit()), ([], [], [], [], []))

    def test_gestures_never_reach_the_language_model(self):
        self.gesture("palma")
        self.gesture("pulgar_abajo")
        self.assertNotIn("user_message", self.seen)
        self.assertNotIn("assistant_message", self.seen)
        source = (Path(__file__).resolve().parents[1] / "arthur" / "interfaces" / "gestos" / "actions.py")
        imported = {node.module for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
                    if isinstance(node, ast.ImportFrom)}
        self.assertFalse({m for m in imported if m and ("brain" in m or "assistant" in m)})

    def test_a_failing_action_is_audited_as_error(self):
        def boom():
            raise RuntimeError("la voz no responde")

        self.actions.stop_speaking = boom
        with self.assertLogs("arthur.interfaces.gestos.actions", "ERROR"):
            self.gesture("palma")
        self.assertEqual(self.audit()[-1]["status"], "error")


class FakeWorker:
    instances = []

    def __init__(self, on_message, *, on_exit=None, fps=8, command=None):
        self.on_message, self.started, self.stopped = on_message, 0, 0
        FakeWorker.instances.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class ServiceGesturesTest(unittest.TestCase):
    """El servicio con los gestos permitidos, con el proceso de la cámara FALSO."""

    def setUp(self):
        FakeWorker.instances = []
        patcher = mock.patch("arthur.interfaces.gestos.watcher.GestureWorker", FakeWorker)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.mkdtemp(prefix="arthur-", dir="/tmp")
        self.addCleanup(lambda: Path(tmp).rmdir())
        self.audit_path = Path(tmp) / "audit.jsonl"
        self.addCleanup(lambda: self.audit_path.unlink(missing_ok=True))
        self.path = Path(tmp) / "control.sock"
        self.switch = GestureSwitch()
        config = Config(brain=BrainConfig(provider="rules"), gestures=GesturesConfig(enabled=True))
        app = build_app(config, confirmer=DenyAll(), audit=AuditLog(self.audit_path), gestures=self.switch)
        self.app = dataclasses.replace(app, media=None, power_watch=False)
        self.states, self.notices = [], []
        self.app.bus.subscribe("camera_state", lambda e: self.states.append((e.data["on"], e.data["reason"])))
        self.stop = threading.Event()
        self.thread = threading.Thread(target=run_service, args=(self.app,), daemon=True, kwargs={
            "orb_url": lambda: None, "control_path": self.path, "stop": self.stop, "gestures": self.switch,
            "camera_notify": self.notices.append})
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.switch.watcher is not None))

    def test_camera_starts_off_and_is_turned_off_when_the_service_stops(self):
        self.assertEqual(FakeWorker.instances, [])  # arrancar el servicio NO enciende la cámara
        self.assertFalse(self.switch.watcher.active)
        self.assertIn("encendida", self.switch.set(True, by="orb"))
        self.assertEqual(FakeWorker.instances[0].started, 1)
        self.stop.set()
        self.thread.join(5)
        self.assertEqual(FakeWorker.instances[0].stopped, 1)
        self.assertEqual(self.states, [(True, "orb"), (False, "servicio")])
        self.assertEqual(self.notices, [  # notificación (falsa) al encender y al apagar
            f"Cámara ENCENDIDA para los gestos (desde el Orb). {GESTURE_HELP}.",
            "Cámara apagada (ARTHUR se ha parado)."])
        cameras = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
        self.assertEqual([(e["tool"], e["arguments"], e["decision"]) for e in cameras],
                         [("camera", {"on": True}, "orb"), ("camera", {"on": False}, "servicio")])

    def test_the_shortcut_toggles_the_camera_through_the_control_socket(self):
        from arthur.api.control import send_command

        self.assertTrue(wait_for(self.path.exists))
        self.assertIn("encendida", send_command("toggle-camera", self.path))
        self.assertIn("apagada", send_command("toggle-camera", self.path))
        self.stop.set()
        self.thread.join(5)
        self.assertEqual(self.states, [(True, "atajo"), (False, "atajo")])
        self.assertEqual(self.notices[0],
                         f"Cámara ENCENDIDA para los gestos (con el atajo de teclado). {GESTURE_HELP}.")

    def test_the_tool_is_registered(self):
        self.assertIsNotNone(self.app.registry.get("set_gestures"))
        self.stop.set()
        self.thread.join(5)


if __name__ == "__main__":
    unittest.main()
