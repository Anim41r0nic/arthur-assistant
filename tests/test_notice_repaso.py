"""Repaso (Tarea 14, núcleo): el aviso diario del cerebro abre Apuntes en la pestaña Repaso («#repaso»).

El cerebro solo pide el destino fijo «apuntes»; la dirección la pone el núcleo (ARTHUR_APUNTES_PUBLIC_URL + «#repaso»).
Push y el service worker aceptan ese fragmento y nada parecido. Núcleo falso y Node aislado: nada sale a la red.
"""

import io
import json
import shutil
import subprocess
import unittest
import urllib.error
from pathlib import Path

from arthur.api.core_api import NOTICE_DEVICES, CoreServer, notice_destination
from arthur.push.destination import check_notice_target
from arthur.studio import notify

APUNTES = "http://100.64.0.10:8774"
TOKEN = "arthur_" + "c" * 40


class FakeCore:
    """El núcleo, falso: apunta lo que le llega."""

    def __init__(self):
        self.sent = []

    def open(self, request, timeout=None):
        self.sent.append(json.loads(request.data))
        return io.BytesIO(b'{"ok": true}')


def destination(device, body, apuntes=APUNTES):
    return notice_destination(device, body, None, None, None, apuntes)


class CoreTest(unittest.TestCase):
    def test_cerebro_puede_mandar_avisos(self):
        self.assertIn("cerebro", NOTICE_DEVICES)

    def test_destino_apuntes_solo_cerebro(self):
        body = {"text": "10 preguntas te esperan", "destination": "apuntes"}
        self.assertEqual(destination("cerebro", body), APUNTES + "#repaso")
        for device in ("studio", "panel", "webchat", "movil"):
            with self.subTest(device=device), self.assertRaises(PermissionError):
                destination(device, body)
        with self.assertRaises(LookupError):
            destination("cerebro", body, apuntes=None)
        # El cerebro no puede pedir otros destinos ni colar números.
        self.assertIsNone(destination("cerebro", {"text": "x"}))
        for bad in ({"destination": "panel"}, {"destination": "clientes", "client": 1},
                    {"destination": "studio", "review": 1, "profile": "frutinovelas"}):
            with self.subTest(bad=bad), self.assertRaises(PermissionError):
                notice_destination("cerebro", bad, "http://100.64.1.2:8775", APUNTES, APUNTES, APUNTES)
        for extra in ({"client": 3}, {"review": 3}, {"profile": "frutinovelas"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                destination("cerebro", {"destination": "apuntes", **extra})
        # Una dirección pedida por el aparato nunca se usa.
        self.assertEqual(destination("cerebro", {"destination": "apuntes", "url": "https://evil.example"}),
                         APUNTES + "#repaso")

    def test_ruta_del_nucleo_entrega_el_enlace(self):
        delivered, replies = [], []
        server = object.__new__(CoreServer)
        server.panel_public_url = server.clientes_public_url = server.studio_public_url = None
        server.apuntes_public_url = APUNTES
        server.on_notice = lambda text, **options: delivered.append((text, options))
        handler = object.__new__(server._make_handler())
        handler._reply = lambda status, body: replies.append((status, body))
        handler._json_body = lambda: {"text": "10 preguntas te esperan", "destination": "apuntes"}
        handler._notice("cerebro")
        self.assertEqual(replies[-1], (200, {"ok": True}))
        self.assertEqual(delivered, [("10 preguntas te esperan", {"target_url": APUNTES + "#repaso"})])
        handler._notice("studio")
        self.assertEqual(replies[-1][0], 403)
        handler._notice("movil")
        self.assertEqual(replies[-1][0], 403)
        server.apuntes_public_url = None
        handler._notice("cerebro")
        self.assertEqual(replies[-1][0], 503)
        self.assertEqual(len(delivered), 1)

    def test_core_server_valida_la_direccion_de_apuntes(self):
        from arthur.core.events import EventBus
        server = CoreServer(bus=EventBus(), devices=None, handle_text=lambda t: t, port=0,
                            apuntes_public_url=APUNTES + "/")
        self.assertEqual(server.apuntes_public_url, APUNTES)
        with self.assertRaises(ValueError):
            CoreServer(bus=EventBus(), devices=None, handle_text=lambda t: t, port=0,
                       apuntes_public_url="https://apuntes.example.com/redirect")


class FragmentTest(unittest.TestCase):
    def test_fragmento_repaso_valido(self):
        self.assertEqual(check_notice_target(APUNTES + "#repaso"), APUNTES + "#repaso")
        for value in (APUNTES + "#repaso2", APUNTES + "#x", APUNTES + "#Repaso", APUNTES + "#repaso#c1",
                      APUNTES + "#repasox", APUNTES + "#c12repaso", APUNTES + "/x#repaso"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_notice_target(value)

    @unittest.skipUnless(shutil.which("node"), "Node no está instalado; se omite la prueba aislada del navegador")
    def test_fragmento_repaso_valido_en_sw(self):
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
  await click({ body: "10 preguntas te esperan", url: "http://100.64.0.10:8774#repaso" });
  await click({ body: "Malo", url: "http://100.64.0.10:8774#repaso2" });
  await click({ body: "Malo", url: "http://100.64.0.10:8774#x" });
  assert.deepStrictEqual(opened, ["http://100.64.0.10:8774/#repaso", "/", "/"]);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", js, str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class NotifierTest(unittest.TestCase):
    def test_notifier_admite_destino_apuntes(self):
        core = FakeCore()
        notifier = notify.Notifier("http://core:8770", TOKEN, opener=core)
        self.assertTrue(notifier.send("10 preguntas te esperan", destination="apuntes"))
        self.assertEqual(core.sent, [{"text": "10 preguntas te esperan", "destination": "apuntes"}])
        for kwargs in ({"destination": "apuntes", "client": 1}, {"destination": "apuntes", "review": 1},
                       {"destination": "apuntes", "review": 1, "profile": "frutinovelas"},
                       {"destination": "repaso"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                notifier.send("x", **kwargs)
        self.assertEqual(len(core.sent), 1)


if __name__ == "__main__":
    unittest.main()
