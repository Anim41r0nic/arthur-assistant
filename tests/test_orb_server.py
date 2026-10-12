import http.client
import json
import socket
import time
import unittest
from unittest import mock

from arthur.api import server as orb_server
from arthur.api.server import OrbServer
from arthur.core.config import ConfigError, config_from_dict
from arthur.core.events import Event, EventBus


class OrbServerTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.server = OrbServer(self.bus, port=0, assistant_name="ARTHUR", get_state=lambda: "idle")
        self.server.start()
        self.addCleanup(self.server.stop)
        self.host = f"127.0.0.1:{self.server.port}"

    def request(self, path, *, host=None, cookie=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Host": host or self.host}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_index_with_token_sets_cookie_and_security_headers(self):
        response, body = self.request(f"/?t={self.server.token}")
        self.assertEqual(response.status, 200)
        self.assertIn(b"<canvas", body)
        self.assertIn("HttpOnly; SameSite=Strict", response.getheader("Set-Cookie"))
        csp = response.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("'sha256-", csp)  # el import map, autorizado por su huella
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")

    def test_cookie_gives_access_to_assets(self):
        response, body = self.request("/js/main.js", cookie=f"arthur_token={self.server.token}")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/javascript"))

    def test_no_or_wrong_token_is_rejected(self):
        for path, cookie in (("/", None), ("/?t=falso", None), ("/js/main.js", "arthur_token=falso"), ("/events", None)):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=cookie)[0].status, 403)

    def test_a_token_with_non_ascii_characters_is_a_normal_403(self):
        """hmac.compare_digest lanza TypeError con texto no ASCII: sin controlar, el servidor soltaba una traza
        y cortaba la conexión sin contestar (cualquiera que alcance el puerto podía provocarlo)."""
        for path, cookie in (("/?t=%C3%A9", None), ("/?t=%E2%82%AC%E2%82%AC", None), ("/js/main.js", "arthur_token=é")):
            with self.subTest(path=path, cookie=cookie):
                self.assertEqual(self.request(path, cookie=cookie)[0].status, 403)

    def test_dns_rebinding_is_blocked(self):
        response, _ = self.request(f"/?t={self.server.token}", host=f"malvado.com:{self.server.port}")
        self.assertEqual(response.status, 403)

    def test_cannot_escape_the_static_folder(self):
        cookie = f"arthur_token={self.server.token}"
        for path in ("/../arthur/core/config.py", "/%2e%2e/%2e%2e/README.md", "/vendor/three/VENDOR.md", "/js/"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=cookie)[0].status, 404)

    def test_there_is_no_way_to_send_orders(self):
        cookie = f"arthur_token={self.server.token}"
        # Sin broker no existe ni siquiera /api/confirm; y no hay ninguna otra ruta de escritura.
        for path in ("/api/confirm", "/api/gestures", "/events", "/api/run", "/"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=cookie, method="POST")[0].status, 404)
        for method in ("PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.request(f"/events?t={self.server.token}", method=method)[0].status, 501)

    def test_event_stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", f"/events?t={self.server.token}", headers={"Host": self.host})
        response = conn.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")

        def next_event():
            line = response.readline()
            while not line.startswith(b"data: "):
                line = response.readline()
            response.readline()  # línea en blanco que separa eventos
            return json.loads(line[6:])

        hello = next_event()
        self.assertEqual((hello["type"], hello["data"]), ("hello", {"name": "ARTHUR", "state": "idle", "pending": [],
                                                                 "camera": {"available": False, "on": False}}))

        self.bus.publish(Event("interno_no_reenviado", {"x": 1}))
        self.bus.publish(Event("state_changed", {"from": "idle", "to": "thinking"}))
        self.bus.publish(Event("assistant_message", {"text": "x" * 5000}))
        changed = next_event()
        self.assertEqual((changed["type"], changed["data"]["to"]), ("state_changed", "thinking"))
        message = next_event()
        self.assertEqual(len(message["data"]["text"]), 2000)  # textos recortados
        conn.close()


class OrbReplayTest(OrbServerTest):
    def test_late_client_gets_recent_messages_but_not_old_states(self):
        self.bus.publish(Event("user_message", {"text": "hola"}))
        self.bus.publish(Event("state_changed", {"from": "idle", "to": "thinking"}))
        self.bus.publish(Event("assistant_message", {"text": "¡hola!"}))
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", f"/events?t={self.server.token}", headers={"Host": self.host})
        response = conn.getresponse()
        types = []
        while len(types) < 3:
            line = response.readline()
            if line.startswith(b"data: "):
                types.append(json.loads(line[6:])["type"])
        self.assertEqual(types, ["hello", "user_message", "assistant_message"])
        conn.close()


class OrbIdleConnectionTest(unittest.TestCase):
    """Una conexión que no envía nada no puede quedarse abierta para siempre (un hilo del servidor por cada una)."""

    def test_silent_connection_is_closed_and_the_server_keeps_working(self):
        with mock.patch.object(orb_server, "REQUEST_TIMEOUT", 0.3):
            server = OrbServer(EventBus(), port=0, assistant_name="ARTHUR", get_state=lambda: "idle")
        server.start()
        self.addCleanup(server.stop)
        silent = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        self.addCleanup(silent.close)
        started = time.monotonic()
        self.assertEqual(silent.recv(10), b"")  # el servidor ha cerrado la conexión sin recibir nada
        self.assertLess(time.monotonic() - started, 3)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", f"/?t={server.token}", headers={"Host": f"127.0.0.1:{server.port}"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_default_timeout_is_reasonable(self):
        self.assertTrue(5 <= orb_server.REQUEST_TIMEOUT <= 60)


class OrbConfigTest(unittest.TestCase):
    def test_port_limits(self):
        self.assertEqual(config_from_dict({"orb": {"port": 9000}}).orb.port, 9000)
        for port in (80, 70000):
            with self.subTest(port=port), self.assertRaises(ConfigError):
                config_from_dict({"orb": {"port": port}})


if __name__ == "__main__":
    unittest.main()
