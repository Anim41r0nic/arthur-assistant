"""ARTHUR en el servidor (fase S2): llaves por aparato, la API del core y el cliente de texto.

Todo en local: carpetas temporales, el servidor HTTP escucha en 127.0.0.1 en un puerto libre y el «cerebro» es
una función falsa. Nada de red de verdad ni del escritorio.
"""

import io
import json
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from arthur.api.core_api import CoreServer
from arthur.core.config import SecurityConfig
from arthur.core.events import Event, EventBus
from arthur.integrations.google import FileKeyring
from arthur.interfaces.core import (RemoteCore, RemoteError, check_url, load_client, pair, run_remote_client,
                                    save_client)
from arthur.security.broker import ConfirmationBroker
from arthur.security.confirm import ConfirmationRequest
from arthur.security.devices import DeviceError, DeviceRegistry
from arthur.tools import build_registry
from arthur.tools.base import Risk


class DeviceRegistryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "datos" / "devices.json"
        self.devices = DeviceRegistry(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_key_is_shown_once_and_only_its_fingerprint_is_kept(self):
        token = self.devices.add("cachyos")
        self.assertTrue(token.startswith("arthur_"))
        self.assertNotIn(token, self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.devices.check(token), "cachyos")
        self.assertIsNone(self.devices.check(token + "x"))
        self.assertIsNone(self.devices.check(""))
        self.assertIsNone(self.devices.check(None))

    def test_each_device_has_its_own_key_and_can_be_revoked_alone(self):
        a, b = self.devices.add("cachyos"), self.devices.add("iphone")
        self.assertTrue(self.devices.revoke("cachyos"))
        self.assertIsNone(self.devices.check(a))
        self.assertEqual(self.devices.check(b), "iphone")
        self.assertFalse(self.devices.revoke("cachyos"))
        self.assertEqual([n for n, _ in self.devices.names()], ["iphone"])

    def test_bad_names_and_duplicates(self):
        for name in ("", "CachyOS", "../x", "a b", "x" * 33):
            with self.subTest(name=name), self.assertRaises(DeviceError):
                self.devices.add(name)
        self.devices.add("pc")
        with self.assertRaises(DeviceError):
            self.devices.add("pc")


class CoreApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.devices = DeviceRegistry(Path(self.dir.name) / "devices.json")
        self.token = self.devices.add("cachyos")
        self.bus = EventBus()
        self.broker = ConfirmationBroker(self.bus, timeout=5)
        self.asked = []

        def handle_text(text):
            self.asked.append(text)
            return f"eco: {text}"

        self.notices = []
        self.server = CoreServer(bus=self.bus, devices=self.devices, handle_text=handle_text, broker=self.broker,
                                 host="127.0.0.1", port=0, on_notice=self.notices.append)
        self.broker.has_clients = lambda: self.server.stream_count() > 0
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.port}"
        self.core = RemoteCore(self.url, self.token)

    def tearDown(self):
        self.server.stop()
        self.dir.cleanup()

    def raw(self, method, path, body=None, token=None, content_type="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_health_needs_no_key_but_everything_else_does(self):
        self.assertEqual(self.raw("GET", "/api/v1/health"), (200, {"ok": True}))
        import arthur.api.core_api as api
        delay, api.BAD_KEY_DELAY = api.BAD_KEY_DELAY, 0
        try:
            with self.assertLogs("arthur.api.core_api", "WARNING"):
                for method, path in (("GET", "/api/v1/whoami"), ("GET", "/api/v1/events"), ("POST", "/api/v1/message")):
                    with self.subTest(path=path):
                        self.assertEqual(self.raw(method, path, {"text": "hola"} if method == "POST" else None,
                                                  token="arthur_falsa")[0], 401)
        finally:
            api.BAD_KEY_DELAY = delay
        self.assertEqual(self.asked, [])

    def test_message_goes_to_the_assistant_and_comes_back(self):
        self.assertEqual(self.core.whoami()["device"], "cachyos")
        self.assertEqual(self.core.ask("¿qué hora es?"), "eco: ¿qué hora es?")
        self.assertEqual(self.asked, ["¿qué hora es?"])

    def test_studio_notices_only_from_the_studio_key(self):
        # Creator Studio (otro contenedor) avisa por ARTHUR: portátil y móvil, como un recordatorio.
        studio = self.devices.add("studio")
        self.assertEqual(self.raw("POST", "/api/v1/notice", {"text": "Vídeo listo"}, token=self.token)[0], 403)
        for body in ({"text": ""}, {"text": "x" * 301}, {"texto": "hola"}, {"text": 5}):
            with self.subTest(body=str(body)[:20]):
                self.assertEqual(self.raw("POST", "/api/v1/notice", body, token=studio)[0], 400)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.raw("POST", "/api/v1/notice", {"text": " Vídeo\nlisto\x07 "}, token=studio),
                         (200, {"ok": True}))
        self.assertEqual(self.notices, ["Vídeo listo"])   # sin saltos ni caracteres de control
        # el panel de vida también puede; un móvil, no
        panel = self.devices.add("panel")
        self.assertEqual(self.raw("POST", "/api/v1/notice", {"text": "Buenos días"}, token=panel), (200, {"ok": True}))
        self.assertEqual(self.notices[-1], "Buenos días")
        movil = self.devices.add("movil")
        self.assertEqual(self.raw("POST", "/api/v1/notice", {"text": "hola"}, token=movil)[0], 403)
        self.assertEqual(self.asked, [])                   # nunca pasa por el cerebro

    def test_webchat_sends_plain_notices_but_cannot_open_the_panel(self):
        # El asistente de la web avisa a Marc de un contacto nuevo; antes el núcleo le daba 403 y el aviso no llegaba.
        webchat = self.devices.add("webchat")
        self.assertEqual(self.raw("POST", "/api/v1/notice", {"text": "Contacto nuevo"}, token=webchat),
                         (200, {"ok": True}))
        self.assertEqual(self.notices[-1], "Contacto nuevo")
        # pero el destino «panel» sigue siendo solo del panel
        self.assertEqual(self.raw("POST", "/api/v1/notice", {"text": "hola", "destination": "panel"},
                                  token=webchat)[0], 403)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.asked, [])

    def test_bad_bodies_are_rejected_before_reaching_the_assistant(self):
        cases = [({"text": ""}, 400), ({"text": "x" * 4001}, 400), ({"texto": "hola"}, 400), ([1, 2], 400),
                 (b"no es json", 400)]
        for body, code in cases:
            with self.subTest(body=str(body)[:20]):
                self.assertEqual(self.raw("POST", "/api/v1/message", body, token=self.token)[0], code)
        self.assertEqual(self.raw("POST", "/api/v1/message", {"text": "hola"}, token=self.token,
                                  content_type="text/plain")[0], 415)
        self.assertEqual(self.asked, [])

    def test_confirmation_is_asked_on_the_connected_device(self):
        events = []
        stream = threading.Thread(target=lambda: [events.append(e) for e in self.core.events()], daemon=True)
        stream.start()
        deadline = time.monotonic() + 5
        while self.server.stream_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        answer = []
        asking = threading.Thread(target=lambda: answer.append(self.broker.ask(
            ConfirmationRequest("delete_event", Risk.WRITE, "borrar «dentista»"))))
        asking.start()
        while not any(e["type"] == "confirmation_pending" for e in events) and time.monotonic() < deadline:
            time.sleep(0.02)
        pending = next(e for e in events if e["type"] == "confirmation_pending")
        self.assertEqual(pending["data"]["summary"], "borrar «dentista»")
        self.assertTrue(self.core.confirm(pending["data"]["id"], True))
        asking.join(5)
        self.assertEqual(answer, [True])
        self.assertFalse(self.core.confirm(pending["data"]["id"], False))  # ya contestada: no se puede cambiar
        self.assertEqual(events[0]["type"], "hello")
        self.assertEqual(events[0]["data"]["device"], "cachyos")

    def test_only_listed_events_reach_the_devices(self):
        events = []
        threading.Thread(target=lambda: [events.append(e) for e in self.core.events()], daemon=True).start()
        deadline = time.monotonic() + 5
        while self.server.stream_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.bus.publish(Event("gesture", {"name": "palma"}))
        self.bus.publish(Event("assistant_message", {"text": "hola", "announcement": True}))
        while len(events) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([e["type"] for e in events], ["hello", "assistant_message"])


class ClientTest(unittest.TestCase):
    def test_http_only_inside_home_or_tailscale(self):
        for url in ("http://100.64.1.2:8770", "http://10.0.0.50:8770/", "http://127.0.0.1:8770",
                    "http://arthur-server.tail1234.ts.net:8770", "https://arthur.example.com"):
            with self.subTest(url=url):
                check_url(url)
        for url in ("http://arthur.example.com", "http://8.8.8.8:8770", "ftp://100.64.1.2", "http://100.64.1.2/api",
                    "nada"):
            with self.subTest(url=url), self.assertRaises(RemoteError):
                check_url(url)

    def test_saved_client_file_is_private_and_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "conf" / "cliente.toml"
            save_client("http://100.64.1.2:8770", 'arthur_"raro"\\', path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_client(path), ("http://100.64.1.2:8770", 'arthur_"raro"\\'))
            with self.assertRaises(RemoteError):
                load_client(Path(d) / "no-existe.toml")

    def test_pairing_checks_the_key_before_saving(self):
        with tempfile.TemporaryDirectory() as d:
            devices = DeviceRegistry(Path(d) / "devices.json")
            token = devices.add("cachyos")
            server = CoreServer(bus=EventBus(), devices=devices, handle_text=lambda t: t, host="127.0.0.1", port=0)
            server.start()
            import arthur.api.core_api as api
            delay, api.BAD_KEY_DELAY = api.BAD_KEY_DELAY, 0
            try:
                url, path, said = f"http://127.0.0.1:{server.port}", Path(d) / "cliente.toml", []
                with self.assertLogs("arthur.api.core_api", "WARNING"):
                    self.assertEqual(pair(url, ask_secret=lambda _: "arthur_mala", say=said.append, path=path), 1)
                self.assertFalse(path.exists())
                self.assertEqual(pair(url, ask_secret=lambda _: token, say=said.append, path=path), 0)
                self.assertEqual(load_client(path), (url, token))
                self.assertNotIn(token, "\n".join(said))  # la llave nunca se muestra en pantalla
            finally:
                api.BAD_KEY_DELAY = delay
                server.stop()

    def test_chat_loop(self):
        class FakeCore:
            url = "http://100.64.1.2:8770"

            def whoami(self):
                return {"device": "cachyos", "name": "ARTHUR"}

            def ask(self, text):
                if text == "falla":
                    raise RemoteError("No puedo conectar")
                return f"eco: {text}"

        lines = iter(["hola", "falla", "/salir"])
        out = io.StringIO()
        code = run_remote_client(FakeCore(), input_fn=lambda _: next(lines), output=out, listen_events=False)
        self.assertEqual(code, 0)
        self.assertIn("arthur › eco: hola", out.getvalue())
        self.assertIn("⚠ No puedo conectar", out.getvalue())


class CoreProfileTest(unittest.TestCase):
    def test_server_has_no_desktop_tools(self):
        from arthur.memory.store import MemoryStore

        memory = MemoryStore.in_memory()
        self.addCleanup(memory.close)
        registry = build_registry(SecurityConfig(), memory=memory, calendar=object(), gmail=object(), profile="core")
        names = {t.name for t in registry.all()}
        self.assertIn("remember", names)
        self.assertIn("get_system_info", names)
        for desktop in ("run_command", "read_file", "open_app", "close_window", "set_volume", "media_control"):
            self.assertNotIn(desktop, names)
        with self.assertRaises(ValueError):
            build_registry(SecurityConfig(), profile="nube")


class CommandLineTest(unittest.TestCase):
    def test_device_commands_run_from_the_real_entry_point(self):
        """Lanza `python -m arthur` de verdad (con carpetas temporales): así se vería si una orden nueva quedara
        definida después de arrancar el programa, como pasó en la primera prueba en el servidor."""
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "XDG_CONFIG_HOME": f"{d}/c", "XDG_DATA_HOME": f"{d}/s", "XDG_STATE_HOME": f"{d}/t"}
            run = lambda *a: subprocess.run([sys.executable, "-m", "arthur", *a], env=env, capture_output=True,
                                            text=True, timeout=60, cwd=Path(__file__).resolve().parents[1])
            added = run("--dispositivo-nuevo", "cachyos")
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertIn("arthur_", added.stdout)
            listed = run("--dispositivos")
            self.assertIn("cachyos", listed.stdout)
            self.assertNotIn("arthur_", listed.stdout)  # la lista nunca enseña llaves
            self.assertEqual(run("--revocar", "cachyos").returncode, 0)
            self.assertEqual(run("--revocar", "cachyos").returncode, 1)


class FileKeyringTest(unittest.TestCase):
    def test_private_file_store_load_clear(self):
        with tempfile.TemporaryDirectory() as d:
            ring = FileKeyring(Path(d) / "datos" / "google-token")
            self.assertIsNone(ring.load())
            ring.store("1//refresco")
            self.assertEqual(stat.S_IMODE(ring.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(ring.path.parent.stat().st_mode) & 0o077, 0)
            self.assertEqual(ring.load(), "1//refresco")
            ring.clear()
            self.assertIsNone(ring.load())


if __name__ == "__main__":
    unittest.main()
