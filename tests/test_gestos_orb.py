"""Tests del Orb con los gestos (paso 6 de docs/gestos.md): punto rojo, botón de la cámara y qué eventos salen.

Sin cámara ni navegador: el servidor del Orb de verdad en 127.0.0.1 con un puerto libre, y un vigilante con el
proceso de la cámara FALSO.
"""

import http.client
import json
import re
import unittest
from pathlib import Path

from arthur.api.server import FORWARDED_EVENTS, REPLAYED_EVENTS, OrbServer
from arthur.core.config import GesturesConfig
from arthur.core.events import Event, EventBus
from arthur.interfaces.gestos.watcher import GestureSwitch, GestureWatcher

ORB = Path(__file__).resolve().parents[1] / "ui" / "orb"


class FakeWorker:
    def __init__(self, on_message, on_exit):
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class OrbGesturesTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.switch = GestureSwitch()
        self.server = OrbServer(self.bus, port=0, assistant_name="ARTHUR", get_state=lambda: "idle",
                                gestures=self.switch)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.origin = f"http://127.0.0.1:{self.server.port}"
        self.cookie = f"arthur_token={self.server.token}"

    def connect_watcher(self, enabled=True):
        self.switch.watcher = GestureWatcher(self.bus, GesturesConfig(enabled=enabled), tick_seconds=0,
                                             worker_factory=FakeWorker, is_locked=lambda: False)
        self.addCleanup(self.switch.watcher.turn_off, "servicio")
        return self.switch.watcher

    def post(self, body, *, headers=None):
        default = {"Host": f"127.0.0.1:{self.server.port}", "Cookie": self.cookie, "Origin": self.origin,
                   "X-Arthur": "1", "Content-Type": "application/json"}
        default.update(headers or {})
        default = {k: v for k, v in default.items() if v is not None}
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn.request("POST", "/api/gestures", body=data, headers=default)
        response = conn.getresponse()
        text = response.read().decode()
        conn.close()
        return response.status, text

    def stream(self):
        """Abre /events como haría el Orb; devuelve una función que lee el siguiente evento."""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{self.server.port}", "Cookie": self.cookie})
        response = conn.getresponse()
        self.addCleanup(conn.close)

        def next_event():
            line = response.readline()
            while not line.startswith(b"data: "):
                line = response.readline()
            return json.loads(line[6:])

        return next_event

    def test_gesture_events_never_reach_the_orb_but_camera_state_does(self):
        self.assertIn("camera_state", FORWARDED_EVENTS)
        self.assertNotIn("gesture", FORWARDED_EVENTS)
        self.assertNotIn("camera_state", REPLAYED_EVENTS)  # el estado actual va en «hello», no un evento viejo
        next_event = self.stream()
        self.assertEqual(next_event()["type"], "hello")
        self.bus.publish(Event("gesture", {"name": "palma"}))
        self.bus.publish(Event("camera_state", {"on": True, "reason": "orb"}))
        camera = next_event()  # el «gesture» no ha llegado: lo siguiente ya es la cámara
        self.assertEqual((camera["type"], camera["data"]), ("camera_state", {"on": True, "reason": "orb"}))

    def test_hello_says_whether_there_is_a_button_and_if_the_camera_is_on(self):
        self.assertEqual(self.server.camera_status(), {"available": False, "on": False})  # aún sin vigilante
        watcher = self.connect_watcher()
        self.assertEqual(self.server.camera_status(), {"available": True, "on": False})
        watcher.turn_on("orb")
        hello = self.stream()()
        self.assertEqual(hello["data"]["camera"], {"available": True, "on": True})

    def test_the_button_turns_the_camera_on_and_off(self):
        watcher = self.connect_watcher()
        status, text = self.post({"enabled": True})
        self.assertEqual(status, 200)
        self.assertIn("encendida", text)
        self.assertTrue(watcher.active)
        self.assertEqual(self.post({"enabled": False})[0], 200)
        self.assertFalse(watcher.active)

    def test_the_button_respects_enabled_false(self):
        watcher = self.connect_watcher(enabled=False)
        status, text = self.post({"enabled": True})
        self.assertEqual(status, 200)
        self.assertIn("enabled = true", text)
        self.assertFalse(watcher.active)

    def test_every_defense_of_the_button(self):
        self.assertEqual(self.post({"enabled": True})[0], 404)  # sin vigilante (no es el servicio)
        watcher = self.connect_watcher()
        cases = [
            ("sin cookie", {"enabled": True}, {"Cookie": None}, 403),
            ("otra web (Origin)", {"enabled": True}, {"Origin": "https://malvado.com"}, 403),
            ("sin X-Arthur", {"enabled": True}, {"X-Arthur": None}, 403),
            ("formulario HTML", {"enabled": True}, {"Content-Type": "application/x-www-form-urlencoded"}, 415),
            ("DNS rebinding", {"enabled": True}, {"Host": f"malvado.com:{self.server.port}"}, 403),
            ("no booleano", {"enabled": "true"}, {}, 400),
            ("número", {"enabled": 1}, {}, 400),
            ("clave de más", {"enabled": True, "device": "/dev/video1"}, {}, 400),
            ("lista", [True], {}, 400),
            ("JSON roto", b"{no", {}, 400),
            ("demasiado grande", b'{"enabled": true, "x": "' + b"x" * 2000 + b'"}', {}, 413),
        ]
        for name, body, headers, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.post(body, headers=headers)[0], expected)
        self.assertFalse(watcher.active)  # nada de lo anterior la ha encendido


class OrbWithoutGesturesTest(unittest.TestCase):
    def test_no_button_and_no_route(self):
        server = OrbServer(EventBus(), port=0, assistant_name="ARTHUR", get_state=lambda: "idle")
        self.addCleanup(server._httpd.server_close)
        self.assertEqual(server.camera_status(), {"available": False, "on": False})
        self.assertIsNone(server.gestures)


class OrbFilesTest(unittest.TestCase):
    """El HTML y el JavaScript del Orb: punto rojo fijo, botón, y nada de innerHTML."""

    def test_red_dot_and_label_exist_and_start_hidden(self):
        html = (ORB / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r'<span id="camera"[^>]*\bhidden\b')
        self.assertIn('id="camera-dot"', html)
        self.assertIn("Cámara encendida", html)
        self.assertRegex(html, r'<button id="camera-toggle"[^>]*\bhidden\b')

    def test_the_orb_listens_to_camera_state_and_hello(self):
        main = (ORB / "js" / "main.js").read_text(encoding="utf-8")
        self.assertIn("camera_state:", main)
        self.assertIn("e.data.camera", main)
        self.assertNotIn("gesture:", main)
        camera = (ORB / "js" / "camera.js").read_text(encoding="utf-8")
        self.assertIn('"/api/gestures"', camera)
        self.assertIn('"X-Arthur": "1"', camera)
        html_sinks = re.compile(r"\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|\beval\(")
        for js in (ORB / "js").glob("*.js"):
            self.assertNotRegex(js.read_text(encoding="utf-8"), html_sinks, js.name)

    def test_the_dot_is_red(self):
        css = (ORB / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"#camera-dot\s*\{[^}]*#ff[0-9a-f]{4}")


if __name__ == "__main__":
    unittest.main()
