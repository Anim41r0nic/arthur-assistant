"""El aviso del panel solo puede abrir la dirección que el núcleo eligió expresamente."""

import io
import json
import shutil
import subprocess
import unittest
import base64
from pathlib import Path
from unittest import mock

from arthur.api.core_api import CoreServer, notice_destination
from arthur.integrations.push import PushLink, PushNotifier
from arthur.push.destination import check_notice_target, check_notice_url
from arthur.push.push_server import PushServer
from arthur.studio.notify import Notifier

PANEL = "http://100.64.1.2:8775"
CLIENTES = "http://100.64.1.2:8777"
TOKEN = "arthur_" + "x" * 40


class DestinationTest(unittest.TestCase):
    def test_only_root_of_https_or_tailscale(self):
        for value in (PANEL, "https://panel.example.com", "http://equipo.tailnet.ts.net:8775"):
            with self.subTest(value=value):
                self.assertEqual(check_notice_url(value), value)
        for value in ("http://evil.example.com", "http://192.168.1.2:8775", "http://100.128.0.1:8775",
                      "https://user:pass@panel.example.com", "https://panel.example.com/redirect",
                      "https://panel.example.com/?next=evil", "https://panel.example.com/#x",
                      "javascript:alert(1)", "https://panel.example.com\n", "https://panel.example.com:0",
                      "https://panel.example.com\\evil", "https://panel.example.com?",
                      "https://%65vil.example.com", "https://panel.example.com:", 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_notice_url(value)

    def test_request_contains_no_address_and_studio_cannot_redirect(self):
        self.assertEqual(notice_destination("panel", {"destination": "panel", "url": "https://evil.example"}, PANEL),
                         PANEL)
        self.assertIsNone(notice_destination("studio", {"text": "Vídeo listo"}, PANEL))
        with self.assertRaises(PermissionError):
            notice_destination("studio", {"destination": "panel"}, PANEL)
        with self.assertRaises(ValueError):
            notice_destination("panel", {"destination": "https://evil.example"}, PANEL)
        with self.assertRaises(LookupError):
            notice_destination("panel", {"destination": "panel"}, None)


class ClientesDestinationTest(unittest.TestCase):
    """El aviso de un contacto abre la ficha del cliente: solo lo pide el chat de la web y solo con un número."""

    def test_core_destination_clientes_only_for_webchat(self):
        body = {"text": "Cliente nuevo C-0012", "destination": "clientes", "client": 12}
        self.assertEqual(notice_destination("webchat", body, PANEL, CLIENTES), CLIENTES + "#c12")
        for device in ("panel", "studio", "movil"):
            with self.subTest(device=device), self.assertRaises(PermissionError):
                notice_destination(device, body, PANEL, CLIENTES)
        with self.assertRaises(PermissionError):   # y el chat sigue sin poder abrir el panel
            notice_destination("webchat", {"destination": "panel"}, PANEL, CLIENTES)

    def test_core_destination_clientes_url_with_client_id(self):
        self.assertEqual(notice_destination("webchat", {"destination": "clientes", "client": 1,
                                                        "url": "https://evil.example"}, None, CLIENTES),
                         CLIENTES + "#c1")
        self.assertEqual(notice_destination("webchat", {"destination": "clientes", "client": 123456}, None,
                                            "https://clientes.example.com"), "https://clientes.example.com#c123456")
        with self.assertRaises(LookupError):
            notice_destination("webchat", {"destination": "clientes", "client": 12}, PANEL, None)
        # El panel no cambia por tener destino de clientes.
        self.assertEqual(notice_destination("panel", {"destination": "panel"}, PANEL, CLIENTES), PANEL)

    def test_core_destination_rejects_non_int_client(self):
        for client in ("12", 12.0, True, False, None, 0, -3, 10 ** 9, "12#x", [12]):
            with self.subTest(client=client), self.assertRaises(ValueError):
                notice_destination("webchat", {"destination": "clientes", "client": client}, PANEL, CLIENTES)
        with self.assertRaises(ValueError):
            notice_destination("webchat", {"destination": "clientes"}, PANEL, CLIENTES)
        with self.assertRaises(ValueError):   # el número solo vale con el destino de clientes
            notice_destination("panel", {"destination": "panel", "client": 3}, PANEL, CLIENTES)

    def test_core_route_opens_client_card(self):
        delivered = []
        server = object.__new__(CoreServer)
        server.panel_public_url = PANEL
        server.clientes_public_url = CLIENTES
        server.on_notice = lambda text, **options: delivered.append((text, options))
        handler = object.__new__(server._make_handler())
        replies = []
        handler._reply = lambda status, body: replies.append((status, body))
        handler._json_body = lambda: {"text": "Cliente nuevo C-0012", "destination": "clientes", "client": 12}
        handler._notice("webchat")
        self.assertEqual(replies[-1], (200, {"ok": True}))
        self.assertEqual(delivered, [("Cliente nuevo C-0012", {"target_url": CLIENTES + "#c12"})])
        handler._notice("panel")
        self.assertEqual(replies[-1][0], 403)
        handler._json_body = lambda: {"text": "x", "destination": "clientes", "client": "12"}
        handler._notice("webchat")
        self.assertEqual(replies[-1][0], 400)
        server.clientes_public_url = None
        handler._json_body = lambda: {"text": "x", "destination": "clientes", "client": 12}
        handler._notice("webchat")
        self.assertEqual(replies[-1][0], 503)
        self.assertEqual(len(delivered), 1)

    def test_core_server_validates_clientes_public_url(self):
        from arthur.core.events import EventBus
        with self.assertRaises(ValueError):
            CoreServer(bus=EventBus(), devices=None, handle_text=lambda t: t, port=0,
                       clientes_public_url="https://clientes.example.com/redirect")

    def test_notice_target_allows_only_client_fragment(self):
        for value in (CLIENTES, CLIENTES + "#c12", "https://clientes.example.com#c1"):
            with self.subTest(value=value):
                self.assertEqual(check_notice_target(value), value)
        self.assertEqual(check_notice_target(CLIENTES + "/#c12"), CLIENTES + "#c12")   # una sola forma canónica
        for value in (CLIENTES + "#", CLIENTES + "#x12", CLIENTES + "#c", CLIENTES + "#c12x", CLIENTES + "#c-1",
                      CLIENTES + "/x#c12", CLIENTES + "#c12#c3", CLIENTES + "?a#c12", "http://evil.example#c12",
                      CLIENTES + "#c0", CLIENTES + "#c1234567890", CLIENTES + "#c12\n", 12):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_notice_target(value)

    def test_notifier_sends_clientes_destination_with_number(self):
        core = FakeCore()
        notifier = Notifier("http://core:8770", TOKEN, opener=core)
        self.assertTrue(notifier.send("Cliente nuevo C-0012", destination="clientes", client=12))
        self.assertEqual(core.sent, [{"text": "Cliente nuevo C-0012", "destination": "clientes", "client": 12}])
        for kwargs in ({"destination": "clientes"}, {"destination": "clientes", "client": "12"},
                       {"destination": "clientes", "client": True}, {"destination": "panel", "client": 12},
                       {"client": 12}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                notifier.send("x", **kwargs)
        self.assertEqual(len(core.sent), 1)

    def test_push_link_and_server_carry_client_card(self):
        link = PushLink("http://push:8772", TOKEN)
        with mock.patch.object(link, "_call", return_value={"status": 201}) as call:
            self.assertEqual(link.send({}, "ARTHUR", "Cliente nuevo C-0012", url=CLIENTES + "#c12"), 201)
        self.assertEqual(call.call_args.args[1]["url"], CLIENTES + "#c12")
        with self.assertRaises(ValueError):
            link.send({}, "ARTHUR", "x", url=CLIENTES + "#javascript:alert(1)")


class FakeCore:
    def __init__(self):
        self.sent = []

    def open(self, request, timeout=None):
        self.sent.append(json.loads(request.data))
        return io.BytesIO(b'{"ok": true}')


class FakePushLink:
    def __init__(self):
        self.sent = []

    def send(self, subscription, title, body, **options):
        self.sent.append((title, body, options))
        return 201


class FakeSubs:
    def all(self):
        return {"movil": {"endpoint": "https://web.push.apple.com/x", "keys": {"p256dh": "a", "auth": "b"}}}


class FakeDevices:
    def names(self):
        return [("movil", "hoy")]


class ChainTest(unittest.TestCase):
    def test_core_route_uses_its_own_address_without_a_socket(self):
        delivered = []
        server = object.__new__(CoreServer)
        server.panel_public_url = PANEL
        server.on_notice = lambda text, **options: delivered.append((text, options))
        handler_class = server._make_handler()
        handler = object.__new__(handler_class)
        replies = []
        handler._reply = lambda status, body: replies.append((status, body))
        handler._json_body = lambda: {"text": "Buenos días", "destination": "panel", "url": "https://evil.example"}
        handler._notice("panel")
        self.assertEqual(replies[-1], (200, {"ok": True}))
        self.assertEqual(delivered, [("Buenos días", {"target_url": PANEL})])
        handler._notice("studio")
        self.assertEqual(replies[-1][0], 403)
        self.assertEqual(len(delivered), 1)
        server.panel_public_url = None
        handler._notice("panel")
        self.assertEqual(replies[-1][0], 503)
        self.assertEqual(len(delivered), 1)

    def test_panel_requests_fixed_destination_and_studio_does_not(self):
        core = FakeCore()
        notifier = Notifier("http://core:8770", TOKEN, opener=core)
        self.assertTrue(notifier.send("Buenos días", destination="panel"))
        self.assertTrue(notifier.send("Vídeo listo"))
        self.assertEqual(core.sent, [{"text": "Buenos días", "destination": "panel"}, {"text": "Vídeo listo"}])
        with self.assertRaises(ValueError):
            notifier.send("x", destination="https://evil.example")

    def test_core_to_push_carries_configured_url(self):
        link = FakePushLink()
        push = PushNotifier(link, FakeSubs(), FakeDevices())
        self.assertEqual(push.notify("ARTHUR", "Buenos días", url=PANEL), 1)
        self.assertEqual(link.sent, [("ARTHUR", "Buenos días", {"url": PANEL})])
        link.sent.clear()
        self.assertEqual(push.notify("ARTHUR", "Recordatorio"), 1)
        self.assertEqual(link.sent, [("ARTHUR", "Recordatorio", {})])

    def test_push_link_sends_url_only_for_panel(self):
        link = PushLink("http://push:8772", TOKEN)
        with mock.patch.object(link, "_call", return_value={"status": 201}) as call:
            self.assertEqual(link.send({}, "ARTHUR", "Hola", url=PANEL), 201)
        self.assertEqual(call.call_args.args[1]["url"], PANEL)
        with self.assertRaises(ValueError):
            link.send({}, "ARTHUR", "Hola", url="https://evil.example/redirect")

    def test_push_server_checks_url_before_encrypting_without_a_socket(self):
        def b64(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        subscription = {"endpoint": "https://web.push.apple.com/x",
                        "keys": {"p256dh": b64(b"\x04" + b"a" * 64), "auth": b64(b"b" * 16)}}
        encrypted = []
        server = object.__new__(PushServer)
        server.deliver = lambda sub, payload, **kw: encrypted.append(payload) or 201
        handler_class = server._make_handler()
        handler = object.__new__(handler_class)
        handler.path = "/send"
        handler._authorized = lambda: True
        replies = []
        handler._reply = lambda status, body: replies.append((status, body))
        body = {"subscription": subscription, "title": "ARTHUR", "body": "Buenos días", "url": PANEL}
        handler._json = lambda: body
        handler.do_POST()
        self.assertEqual(replies[-1], (200, {"status": 201}))
        self.assertEqual(encrypted, [{"title": "ARTHUR", "body": "Buenos días", "url": PANEL}])
        body["url"] = CLIENTES + "#c12"
        handler.do_POST()
        self.assertEqual(replies[-1], (200, {"status": 201}))
        self.assertEqual(encrypted[-1]["url"], CLIENTES + "#c12")
        body["url"] = "https://evil.example/redirect"
        handler.do_POST()
        self.assertEqual(replies[-1][0], 400)
        self.assertEqual(len(encrypted), 2)

    @unittest.skipUnless(shutil.which("node"), "Node no está instalado; se omite la prueba aislada del navegador")
    def test_service_worker_opens_panel_or_core(self):
        script = Path(__file__).parents[1] / "arthur/api/web/sw.js"
        js = r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const handlers = {}, opened = [], shown = [];
const self = {
  location: { origin: "https://chat.example.com" },
  clients: { claim: async () => {}, matchAll: async () => [], openWindow: async url => opened.push(url) },
  registration: { showNotification: async (title, options) => shown.push(options) },
  skipWaiting: () => {}, addEventListener: (name, handler) => { handlers[name] = handler; },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { self, URL, Date });
async function click(data) {
  let pending;
  handlers.push({ data: { json: () => data }, waitUntil: p => { pending = p; } });
  await pending;
  const notice = { data: shown.at(-1).data, close: () => {} };
  handlers.notificationclick({ notification: notice, waitUntil: p => { pending = p; } });
  await pending;
}
(async () => {
  await click({ body: "Resumen", url: "http://100.64.1.2:8775" });
  await click({ body: "Recordatorio" });
  await click({ body: "Malo", url: "https://evil.example/redirect" });
  await click({ body: "Cliente nuevo C-0012", url: "http://100.64.1.2:8777#c12" });
  await click({ body: "Malo", url: "http://100.64.1.2:8777#javascript:alert(1)" });
  await click({ body: "Malo", url: "http://100.64.1.2:8777/x#c12" });
  assert.deepStrictEqual(opened, ["http://100.64.1.2:8775/", "/", "/", "http://100.64.1.2:8777/#c12", "/", "/"]);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", js, str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
