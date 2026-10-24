"""Rutas de la revisión en la web del panel (/api/revision*): llave, CSRF, validación y la nota del portátil."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.panel import juego
from arthur.panel.revision import Revision
from arthur.panel.store import PanelStore
from arthur.panel.web import COOKIE, PanelWeb, WEB_FILES
from arthur.security.devices import DeviceRegistry

AHORA = datetime(2026, 10, 24, 21, 45, tzinfo=ZoneInfo("Europe/Madrid"))  # sábado
INICIO = datetime(2026, 10, 24, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))


class RevisionWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = PanelStore.in_memory(now=lambda: AHORA)
        self.devices = DeviceRegistry(root / "devices.json")
        self.key = self.devices.add("movil")
        self.portatil = self.devices.add("portatil")
        self.rev = Revision(self.store, self.today, now=lambda: AHORA)
        self.web = PanelWeb(self.store, self.devices, self.today, None, lambda: {}, host="127.0.0.1", port=0,
                            now=lambda: AHORA, sleep=lambda s: None, revision=self.rev)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def today(self):
        ev = {"title": "Estudio: MTP", "start": INICIO.isoformat(),
              "end": (INICIO + timedelta(minutes=50)).isoformat(), "all_day": False}
        return self.rev.decorate({"date": "2026-10-24", "tasks": [], "events": [ev], "mail": [], "problems": [],
                                  "repaso": None, "next": {"current": None, "following": None}})

    def call(self, path, body=None, key=None, headers=None):
        req = urllib.request.Request(self.web.url + path, method="POST" if body is not None else "GET")
        if key is not False:
            req.add_header("Authorization", f"Bearer {key or self.key}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_sin_llave_401_y_cookie_sin_x_arthur_403(self):
        self.assertEqual(self.call("/api/revision", key=False)[0], 401)
        with self.assertLogs("arthur.panel.web", "WARNING"):
            code, _ = self.call("/api/revision/descanso", {"activo": True}, key=False,
                                headers={"Cookie": f"{COOKIE}={self.key}"})
        self.assertEqual(code, 403)
        self.assertFalse(self.rev.live()["pausa"])

    def test_tarjeta_y_bloques(self):
        code, data = self.call("/api/today")
        self.assertEqual(code, 200)
        ev = data["events"][0]
        self.assertEqual((ev["study"], ev["mark"]), (True, None))
        code, data = self.call("/api/revision/bloque", {"fecha": "2026-10-24", "clave": ev["key"], "marca": "hecho"})
        self.assertEqual((code, data["marca"]), (200, "hecho"))
        code, live = self.call("/api/revision")
        self.assertEqual(code, 200)
        self.assertEqual(live["hoy"]["estudio"]["hecho"], 1)
        self.assertEqual(live["juego"]["puntos_hoy"]["total"], 20)
        self.assertEqual(self.call("/api/today")[1]["events"][0]["mark"], "hecho")
        for body in ({"fecha": "2026-10-24", "clave": ev["key"], "marca": "genial"},
                     {"fecha": "2026-10-01", "clave": ev["key"], "marca": "hecho"},
                     {"fecha": 5, "clave": ev["key"], "marca": "hecho"},
                     {"fecha": "2026-10-24", "clave": "x" * 400, "marca": "hecho"},
                     {"fecha": "2026-10-24", "clave": ev["key"], "marca": True}):
            self.assertEqual(self.call("/api/revision/bloque", body)[0], 400, body)
        self.assertEqual(self.call("/api/revision/bloque", {"fecha": "2026-10-24", "clave": "otro", "marca": None})[0], 404)

    def test_descanso_ocultar_y_semana(self):
        self.assertEqual(self.call("/api/revision/descanso", {"activo": True})[1], {"fecha": "2026-10-24", "pausa": True})
        self.assertEqual(self.call("/api/revision/descanso", {"activo": "sí"})[0], 400)
        self.assertEqual(self.call("/api/revision/ocultar", {"ocultar": True})[1], {"oculto": True})
        self.assertIsNone(self.call("/api/revision")[1]["juego"])
        self.assertEqual(self.call("/api/revision/ocultar", {})[0], 400)
        self.assertEqual(self.call("/api/revision/semana/leida", {})[0], 404)   # aún no hay resumen semanal

    def test_nota_solo_para_el_portatil(self):
        self.assertEqual(self.call("/api/revision/nota?fecha=2026-10-24&tipo=noche")[0], 403)
        self.assertEqual(self.call("/api/revision/nota?fecha=2026-10-24&tipo=noche", key=self.portatil)[0], 404)
        self.rev.run("noche")
        code, data = self.call("/api/revision/nota?fecha=2026-10-24&tipo=noche", key=self.portatil)
        self.assertEqual((code, data["nombre"]), (200, "2026-10-24.md"))
        self.assertIn("# Sábado 24 de octubre", data["markdown"])
        for q in ("fecha=ayer&tipo=noche", "fecha=2026-10-24&tipo=mes", "tipo=noche"):
            self.assertEqual(self.call("/api/revision/nota?" + q, key=self.portatil)[0], 400, q)

    def test_insignias_servidas_como_ficheros_fijos(self):
        for b in juego.BADGES:
            self.assertIn(f"/insignias/{b['id']}.svg", WEB_FILES)
        req = urllib.request.Request(self.web.url + "/insignias/primer-paso.svg")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"<svg", r.read())


class SinRevisionTests(unittest.TestCase):
    def test_503_si_no_hay_revision(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = PanelStore.in_memory(now=lambda: AHORA)
        devices = DeviceRegistry(Path(tmp.name) / "d.json")
        key = devices.add("movil")
        web = PanelWeb(store, devices, lambda: {}, None, lambda: {}, host="127.0.0.1", port=0, now=lambda: AHORA)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.shutdown)
        req = urllib.request.Request(web.url + "/api/revision", headers={"Authorization": f"Bearer {key}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 503)
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
