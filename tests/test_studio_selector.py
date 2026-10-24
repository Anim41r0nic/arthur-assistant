"""Studio, «Elige canal» (0.63.1, 2026-10-17): al entrar se elige canal y «Vídeos» enseña solo los de ese perfil.

El filtro lo hace el servidor (`GET /api/reviews?perfil=…`) y no la página: así el móvil no descarga los vídeos de
los otros canales y el límite de 30 se cuenta dentro del canal elegido. Todo con datos falsos en una carpeta
temporal y un servidor real en 127.0.0.1 (puerto libre); nada sale a internet. Los avisos al móvil abren el vídeo en
su canal (`#v12-frutinovelas`): núcleo, push y service worker, con un núcleo falso.
"""

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from types import SimpleNamespace

from arthur.api.core_api import CoreServer, notice_destination
from arthur.push.destination import STUDIO_PROFILES, check_notice_target
from arthur.security.devices import DeviceRegistry
from arthur.studio import autopilot, channels, notify, project
from arthur.studio.review import ReviewBook
from arthur.studio.store import StudioStore, studio_dir
from arthur.studio.web import StudioWeb

GUION = """
titulo = "{titulo}"
descripcion = "Descripción."
idioma = "{idioma}"
{perfil}
[[fuentes]]
id = "f"
obra = "Obra"
autor = "Autor"
[[escenas]]
texto = "Uno."
imagen = "File:A.jpg"
fuentes = ["f"]
"""

REFRESH = "1//permiso-secreto-de-prueba"
WEB_DIR = Path(project.__file__).parent / "web"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.book = ReviewBook(self.store)
        studio_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def submit(self, name, idioma="es", perfil=""):
        folder = project.project_dir(name)
        (folder / "salida").mkdir(parents=True, exist_ok=True)
        line = f'perfil = "{perfil}"' if perfil else ""
        (folder / "guion.toml").write_text(GUION.format(titulo=name, idioma=idioma, perfil=line), encoding="utf-8")
        (folder / "salida" / "video.mp4").write_bytes(os.urandom(2000))
        (folder / "salida" / "miniatura.jpg").write_bytes(b"jpg")
        (folder / "salida" / "control.json").write_text('{"ok": true}')
        return self.book.submit(name)

    def make_old(self, review_id, channel=None):
        """Deja una revisión como las de antes del esquema 6: sin perfil (y, si se pide, con otro @)."""
        with self.store._lock, self.store._db:
            if channel is None:
                self.store._db.execute("UPDATE reviews SET profile = '' WHERE id = ?", (review_id,))
            else:
                self.store._db.execute("UPDATE reviews SET profile = '', channel = ? WHERE id = ?",
                                       (channel, review_id))

    def three_channels(self):
        es = self.submit("estoico")
        en = self.submit("stoic", "en")
        fruta = self.submit("fruta", "es", "frutinovelas")
        return es, en, fruta


class FilterTest(Base):
    def test_list_by_profile(self):
        es, en, fruta = self.three_channels()
        self.assertEqual([r.id for r in self.book.list(profile="frutinovelas")], [fruta.id])
        self.assertEqual([r.id for r in self.book.list(profile="estoicismo_en")], [en.id])
        self.assertEqual([r.id for r in self.book.list(profile="estoicismo_es")], [es.id])
        # Sin perfil, como siempre: todos.
        self.assertEqual({r.id for r in self.book.list()}, {es.id, en.id, fruta.id})

    def test_old_reviews_without_profile_are_deduced_by_channel(self):
        es, en, fruta = self.three_channels()
        old_en = self.submit("stoic-viejo", "en")
        self.make_old(old_en.id)                       # sin perfil, pero con el @ inglés
        old_es = self.submit("estoico-viejo")
        self.make_old(old_es.id, "@CanalQueYaNoExiste")  # ni perfil ni @ conocido: por idioma (es → estoicismo_es)
        self.assertEqual({r.id for r in self.book.list(profile="estoicismo_en")}, {en.id, old_en.id})
        self.assertEqual({r.id for r in self.book.list(profile="estoicismo_es")}, {es.id, old_es.id})
        self.assertEqual([r.id for r in self.book.list(profile="frutinovelas")], [fruta.id])

    def test_limit_counts_inside_the_profile(self):
        # 3 de estoicismo más nuevos que el de Frutinovelas: con límite 2 por canal, Frutinovelas no se pierde.
        fruta = self.submit("fruta", "es", "frutinovelas")
        for n in range(3):
            self.submit(f"estoico-{n}")
        self.assertEqual([r.id for r in self.book.list(limit=2, profile="frutinovelas")], [fruta.id])
        self.assertEqual(len(self.book.list(limit=2, profile="estoicismo_es")), 2)

    def test_pending_counts_has_the_three_profiles(self):
        es, en, fruta = self.three_channels()
        self.submit("estoico-2")
        self.book.discard(en.id, "movil")
        old = self.submit("estoico-viejo")
        self.make_old(old.id)
        self.assertEqual(self.book.pending_counts(), {"estoicismo_es": 3, "estoicismo_en": 0, "frutinovelas": 1})

    def test_pending_counts_empty(self):
        self.assertEqual(self.book.pending_counts(), {p: 0 for p in channels.ORDER})


class WebTest(Base):
    def setUp(self):
        super().setUp()
        # Un permiso guardado de Frutinovelas: su estado sale en las tarjetas, el permiso nunca.
        (studio_dir() / "youtube-oauth-frutinovelas").write_text(REFRESH)
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.key = self.devices.add("movil")
        self.web = StudioWeb(self.book, self.devices, "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        super().tearDown()

    def call(self, path, key=True):
        req = urllib.request.Request(self.web.url + path)
        if key:
            req.add_header("Authorization", f"Bearer {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_reviews_filtered_by_profile(self):
        es, en, fruta = self.three_channels()
        status, data = self.call("/api/reviews?perfil=frutinovelas")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in data["reviews"]], [fruta.id])
        self.assertEqual(data["profile"], "frutinovelas")
        status, data = self.call("/api/reviews?perfil=estoicismo_en")
        self.assertEqual([r["id"] for r in data["reviews"]], [en.id])
        # Sin perfil: todos, como antes (lo usa ARTHUR desde el chat, services.py).
        status, data = self.call("/api/reviews")
        self.assertEqual((status, {r["id"] for r in data["reviews"]}), (200, {es.id, en.id, fruta.id}))
        self.assertNotIn("profile", data)

    def test_alias_is_accepted_and_normalised(self):
        es, _, _ = self.three_channels()
        status, data = self.call("/api/reviews?perfil=es")
        self.assertEqual((status, data["profile"]), (200, "estoicismo_es"))
        self.assertEqual([r["id"] for r in data["reviews"]], [es.id])

    def test_unknown_profile_is_400(self):
        self.three_channels()
        for query in ("perfil=otro", "perfil=", "perfil=../x", "perfil=frutinovelas&perfil=es",
                      "perfil=%3Cscript%3E"):
            with self.subTest(query=query):
                status, data = self.call(f"/api/reviews?{query}")
                self.assertEqual(status, 400)
                self.assertNotIn("<script>", data.get("error", ""))  # no se devuelve lo que llegó
        self.assertEqual(self.call("/api/reviews?perfil=frutinovelas", key=False)[0], 401)

    def test_old_review_shows_its_deduced_profile(self):
        es, _, _ = self.three_channels()
        self.make_old(es.id)
        _, data = self.call("/api/reviews?perfil=estoicismo_es")
        self.assertEqual([(r["id"], r["profile"]) for r in data["reviews"]], [(es.id, "estoicismo_es")])
        # El detalle dice el perfil: la página lo usa para cambiar de canal al abrir un vídeo por su enlace (#id).
        _, detail = self.call(f"/api/reviews/{es.id}")
        self.assertEqual(detail["profile"], "estoicismo_es")

    def test_channels_route_counts_and_states(self):
        es, en, fruta = self.three_channels()
        self.book.discard(en.id, "movil")
        status, data = self.call("/api/channels")
        self.assertEqual(status, 200)
        rows = data["channels"]
        self.assertEqual([r["profile"] for r in rows], list(channels.ORDER))
        self.assertEqual([r["pending"] for r in rows], [1, 0, 1])
        self.assertEqual([r["handle"] for r in rows], ["@TuCanalEstoicismo", "@TuCanalStoicEN", "@TuCanalFrutinovelas"])
        self.assertEqual([r["name"] for r in rows], [channels.PROFILES[p].name for p in channels.ORDER])
        # El estado es el mismo que en «Cuentas».
        self.assertEqual([r["status"] for r in rows], ["disconnected", "disconnected", "unverified"])
        for row in rows:
            self.assertEqual(set(row), {"profile", "name", "handle", "status", "pending"})
        self.assertNotIn(REFRESH, json.dumps(data))
        self.assertEqual(self.call("/api/channels", key=False)[0], 401)


class PageTest(unittest.TestCase):
    """La página no se ejecuta en los tests (no hay navegador): se comprueba su estructura."""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (WEB_DIR / "studio.js").read_text(encoding="utf-8")
        cls.css = (WEB_DIR / "studio.css").read_text(encoding="utf-8")

    def test_chooser_section_and_change_button(self):
        self.assertIn('id="channels"', self.html)
        self.assertIn("Elige canal", self.html)
        self.assertIn('id="channel-list"', self.html)
        self.assertRegex(self.html, r'<button id="change-channel" type="button"[^>]*>Cambiar canal</button>')
        self.assertIn('id="channel-name"', self.html)

    def test_cards_are_buttons_in_the_channels_order(self):
        self.assertIn('card.className = "channel-card"', self.js)
        self.assertIn('document.createElement("button")', self.js)
        self.assertIn("/api/channels", self.js)
        self.assertIn("/api/reviews?perfil=", self.js)
        self.assertIn("encodeURIComponent(chosenProfile)", self.js)
        order = re.search(r"const accountProfiles = \[([^\]]*)\]", self.js).group(1)
        self.assertEqual(re.findall(r'"([a-z_]+)"', order), list(channels.ORDER))

    def test_remembered_channel_survives_storage_errors(self):
        # localStorage puede lanzar (modo privado, datos bloqueados): cada uso va dentro de try/catch.
        uses = [m.start() for m in re.finditer(r"localStorage\.", self.js)]
        self.assertGreaterEqual(len(uses), 2)
        for function in re.findall(r"function (rememberProfile|recalledProfile|forgetProfile)\(", self.js):
            body = self.js.split(f"function {function}(", 1)[1].split("\n}\n", 1)[0]
            self.assertIn("try {", body, function)
            self.assertIn("catch", body, function)
        for pos in uses:
            before = self.js[:pos]
            self.assertGreater(before.rfind("try {"), before.rfind("}\n"), "localStorage fuera de try/catch")

    def test_hash_link_opens_the_video_even_from_another_channel(self):
        self.assertIn("location.hash", self.js)
        self.assertIn("hashchange", self.js)
        self.assertIn("r.profile", self.js)  # al abrir un vídeo se pasa a su canal
        # El enlace del aviso al móvil (#v12-frutinovelas) elige el canal antes de abrir el vídeo.
        self.assertIn("v([1-9][0-9]{0,8})-([a-z_]{1,40})", self.js)
        self.assertIn("accountProfiles.includes(match[2])", self.js)

    def test_text_only_and_accessible(self):
        self.assertNotRegex(self.js, r"\.innerHTML\s*=")
        self.assertNotIn("insertAdjacentHTML", self.js)
        self.assertIn(".channel-card:focus-visible", self.css)
        # Una columna en el móvil (390 px): la lista de canales es una rejilla de una columna.
        self.assertRegex(self.css, r"\.channel-list \{[^}]*grid-template-columns: 1fr")


STUDIO = "http://100.64.1.2:8773"
TOKEN = "arthur_" + "s" * 40


class FakeCore:
    """El núcleo, falso: apunta lo que le llega. `refuse`: contesta 503 a los avisos con destino."""

    def __init__(self, refuse=False):
        self.sent, self.refuse = [], refuse

    def open(self, request, timeout=None):
        body = json.loads(request.data)
        if self.refuse and "destination" in body:
            raise urllib.error.HTTPError(request.full_url, 503, "x", {}, io.BytesIO(b"{}"))
        self.sent.append(body)
        return io.BytesIO(b'{"ok": true}')


class NoticeLinkTest(unittest.TestCase):
    """Tocar un aviso de Studio en el móvil abre ese vídeo ya en su canal (#v12-frutinovelas)."""

    def test_profiles_copied_in_push_are_the_channels(self):
        # El contenedor de push no lleva studio/: su copia de los perfiles tiene que seguir siendo la misma.
        self.assertEqual(STUDIO_PROFILES, channels.ORDER)

    def test_core_builds_the_link_only_for_studio(self):
        body = {"text": "Vídeo listo", "destination": "studio", "review": 12, "profile": "frutinovelas"}
        self.assertEqual(notice_destination("studio", body, None, None, STUDIO), STUDIO + "#v12-frutinovelas")
        for device in ("panel", "webchat", "movil"):
            with self.subTest(device=device), self.assertRaises(PermissionError):
                notice_destination(device, body, None, None, STUDIO)
        with self.assertRaises(LookupError):
            notice_destination("studio", body, None, None, None)
        # Lo de siempre sigue igual: sin destino, sin enlace; Studio no puede abrir el panel.
        self.assertIsNone(notice_destination("studio", {"text": "x"}, None, None, STUDIO))
        with self.assertRaises(PermissionError):
            notice_destination("studio", {"destination": "panel"}, "http://100.64.1.2:8775", None, STUDIO)

    def test_core_rejects_bad_review_or_profile(self):
        for review, profile in ((0, "frutinovelas"), (-1, "es"), ("12", "frutinovelas"), (True, "frutinovelas"),
                                (10 ** 9, "frutinovelas"), (12, "es"), (12, "otro"), (12, None), (12, "../x"),
                                (None, "frutinovelas")):
            with self.subTest(review=review, profile=profile), self.assertRaises(ValueError):
                notice_destination("studio", {"destination": "studio", "review": review, "profile": profile},
                                   None, None, STUDIO)
        with self.assertRaises(ValueError):  # el vídeo solo con el destino de Studio
            notice_destination("studio", {"review": 12, "profile": "frutinovelas"}, None, None, STUDIO)
        with self.assertRaises(ValueError):
            notice_destination("studio", {"destination": "studio", "review": 1, "profile": "frutinovelas",
                                          "client": 3}, None, None, STUDIO)

    def test_core_route_delivers_the_link(self):
        delivered, replies = [], []
        server = object.__new__(CoreServer)
        server.panel_public_url = server.clientes_public_url = None
        server.studio_public_url = STUDIO
        server.on_notice = lambda text, **options: delivered.append((text, options))
        handler = object.__new__(server._make_handler())
        handler._reply = lambda status, body: replies.append((status, body))
        handler._json_body = lambda: {"text": "Vídeo listo", "destination": "studio", "review": 7,
                                      "profile": "estoicismo_en"}
        handler._notice("studio")
        self.assertEqual(replies[-1], (200, {"ok": True}))
        self.assertEqual(delivered, [("Vídeo listo", {"target_url": STUDIO + "#v7-estoicismo_en"})])
        handler._notice("webchat")
        self.assertEqual(replies[-1][0], 403)
        server.studio_public_url = None
        handler._notice("studio")
        self.assertEqual(replies[-1][0], 503)
        self.assertEqual(len(delivered), 1)

    def test_core_server_validates_studio_public_url(self):
        from arthur.core.events import EventBus
        with self.assertRaises(ValueError):
            CoreServer(bus=EventBus(), devices=None, handle_text=lambda t: t, port=0,
                       studio_public_url="https://studio.example.com/redirect")

    def test_push_accepts_only_known_video_fragments(self):
        for value in (STUDIO + "#v12-frutinovelas", STUDIO + "#v1-estoicismo_es", STUDIO + "#v999999999-estoicismo_en",
                      STUDIO + "#c12"):
            with self.subTest(value=value):
                self.assertEqual(check_notice_target(value), value)
        for value in (STUDIO + "#v0-frutinovelas", STUDIO + "#v12-otro", STUDIO + "#v12", STUDIO + "#v12-",
                      STUDIO + "#12", STUDIO + "#v12-frutinovelas#c1", STUDIO + "#v1234567890-frutinovelas",
                      STUDIO + "#v12-frutinovelasx", STUDIO + "/x#v12-frutinovelas"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_notice_target(value)

    def test_notifier_sends_review_and_profile(self):
        core = FakeCore()
        notifier = notify.Notifier("http://core:8770", TOKEN, opener=core)
        self.assertTrue(notifier.send("Vídeo listo", destination="studio", review=12, profile="frutinovelas"))
        self.assertEqual(core.sent, [{"text": "Vídeo listo", "destination": "studio", "review": 12,
                                      "profile": "frutinovelas"}])
        for kwargs in ({"destination": "studio"}, {"destination": "studio", "review": 12},
                       {"destination": "studio", "review": 12, "profile": "es"},
                       {"destination": "studio", "review": True, "profile": "frutinovelas"},
                       {"review": 12, "profile": "frutinovelas"},
                       {"destination": "clientes", "client": 1, "review": 12, "profile": "frutinovelas"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                notifier.send("x", **kwargs)
        self.assertEqual(len(core.sent), 1)

    def test_send_with_link_falls_back_to_plain_notice(self):
        core = FakeCore()
        notify.send(notify.Notifier("http://core:8770", TOKEN, opener=core), "Subido", (3, "estoicismo_es"))
        self.assertEqual(core.sent, [{"text": "Subido", "destination": "studio", "review": 3,
                                      "profile": "estoicismo_es"}])
        # Un núcleo sin ARTHUR_STUDIO_PUBLIC_URL rechaza el enlace: el aviso llega igual, sin él.
        core = FakeCore(refuse=True)
        with self.assertLogs("arthur.studio.notify", "WARNING"):
            notify.send(notify.Notifier("http://core:8770", TOKEN, opener=core), "Subido", (3, "estoicismo_es"))
        self.assertEqual(core.sent, [{"text": "Subido"}])
        core = FakeCore()
        notify.send(notify.Notifier("http://core:8770", TOKEN, opener=core), "Sin vídeo")
        self.assertEqual(core.sent, [{"text": "Sin vídeo"}])

    def test_job_review_picks_the_video_of_the_notice(self):
        def store(outputs):
            return SimpleNamespace(steps=lambda job_id: [SimpleNamespace(name=k, output=v) for k, v in outputs.items()])

        def job(kind, status, **params):
            return SimpleNamespace(id=1, kind=kind, status=status, params=params)

        pilot = store({"guion": {"titulo": "T"}, "producir_es": {"largo": 4, "corto": 5}, "producir_en": {"largo": 6}})
        self.assertEqual(autopilot.job_review(pilot, job(autopilot.KIND, "done")), 4)
        self.assertIsNone(autopilot.job_review(pilot, job(autopilot.KIND, "failed")))
        self.assertIsNone(autopilot.job_review(pilot, job(autopilot.KIND, "running")))
        changes = store({"guion": {}, "producir": {"largo": 9}})
        self.assertEqual(autopilot.job_review(changes, job(autopilot.CHANGES_KIND, "done", revision=2)), 9)
        self.assertEqual(autopilot.job_review(store({}), job(autopilot.CHANGES_KIND, "failed", revision=2)), 2)
        self.assertIsNone(autopilot.job_review(store({}), job(autopilot.CHANGES_KIND, "failed", revision="2")))


class ReviewLinkTest(Base):
    def test_review_link_uses_the_effective_profile(self):
        es, en, fruta = self.three_channels()
        self.assertEqual(notify.review_link(self.book, fruta.id), (fruta.id, "frutinovelas"))
        self.assertEqual(notify.review_link(self.book, en.id), (en.id, "estoicismo_en"))
        self.make_old(es.id)  # sin perfil guardado: se deduce como en la lista
        self.assertEqual(notify.review_link(self.book, es.id), (es.id, "estoicismo_es"))
        for missing in (None, 999, True, "1"):
            with self.subTest(missing=missing):
                self.assertIsNone(notify.review_link(self.book, missing))


class ServiceWorkerTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node no está instalado; se omite la prueba aislada del navegador")
    def test_service_worker_opens_the_video_in_its_channel(self):
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
  await click({ body: "Vídeo listo", url: "http://100.64.1.2:8773#v12-frutinovelas" });
  await click({ body: "Malo", url: "http://100.64.1.2:8773#v12-otro" });
  await click({ body: "Malo", url: "http://100.64.1.2:8773#v12-frutinovelas&x" });
  assert.deepStrictEqual(opened, ["http://100.64.1.2:8773/#v12-frutinovelas", "/", "/"]);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", js, str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
