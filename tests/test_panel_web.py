"""Página y API del panel (arthur/panel/web.py): servidor de verdad en 127.0.0.1:0, todo lo demás falso."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.panel.sources.obsidian import ObsidianVault
from arthur.panel.store import PanelStore
from arthur.panel.web import BAD_KEY_DELAY, COOKIE, PanelWeb
from arthur.security.devices import DeviceRegistry

AHORA = datetime(2026, 10, 16, 9, 0, tzinfo=ZoneInfo("Europe/Madrid"))  # viernes


class PanelWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = PanelStore.in_memory(now=lambda: AHORA)
        self.devices = DeviceRegistry(root / "devices.json")
        self.key = self.devices.add("movil")
        (root / "vault").mkdir()
        self.nota = root / "vault" / "Tareas.md"
        self.nota.write_text("# Tareas\n- [ ] Yo: llamar al banco\n- [ ] Claude: otra cosa\n", encoding="utf-8")
        self.vault = ObsidianVault(root / "vault", ("Tareas.md",))
        self.briefings = []
        self.sleeps = []
        self.web = PanelWeb(self.store, self.devices, self.today, self.vault, self.briefing,
                            host="127.0.0.1", port=0, now=lambda: AHORA, sleep=self.sleeps.append)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def today(self):
        return {"date": "2026-10-16", "tasks": [], "progress": {"done": 0, "total": 0, "percent": 0},
                "events": [], "mail": [], "problems": [], "next": {"current": None, "following": None}}

    def briefing(self):
        texto = f"Resumen número {len(self.briefings) + 1}"
        self.store.save_briefing(AHORA.date(), texto, "plantilla", False)
        self.briefings.append(texto)
        return {"date": "2026-10-16", "text": texto, "engine": "plantilla"}

    def call(self, path, body=None, key=True, headers=None, raw=None):
        req = urllib.request.Request(self.web.url + path,
                                     method="POST" if body is not None or raw is not None else "GET")
        if key:
            req.add_header("Authorization", f"Bearer {self.key}")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        if raw is not None:
            req.data = raw
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def cookie(self, **extra):
        return {"Cookie": f"{COOKIE}={self.key}", **extra}

    # --- llave, cookie y CSRF ---
    def test_without_key_data_is_401(self):
        self.assertEqual(self.call("/api/today", key=False)[0], 401)
        self.assertEqual(self.call("/api/briefing", key=False)[0], 401)
        self.assertEqual(self.call("/api/tasks", {"text": "x"}, key=False)[0], 401)

    def test_page_is_public_with_security_headers(self):
        status, headers, body = self.call("/", key=False)
        self.assertEqual(status, 200)
        self.assertIn(b"panel.js", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_cookie_reads_but_changes_need_our_page(self):
        self.assertEqual(self.call("/api/today", key=False, headers=self.cookie())[0], 200)
        evil = self.cookie(**{"X-Arthur": "1", "Origin": "https://mala.example"})
        with self.assertLogs("arthur.panel.web", "WARNING"):
            self.assertEqual(self.call("/api/tasks", {"text": "pan"}, key=False, headers=self.cookie())[0], 403)
            self.assertEqual(self.call("/api/tasks", {"text": "pan"}, key=False, headers=evil)[0], 403)
        self.assertEqual(self.store.tasks_for(AHORA.date()), [])
        ok = self.call("/api/tasks", {"text": "pan"}, key=False, headers=self.cookie(**{"X-Arthur": "1"}))
        self.assertEqual(ok[0], 200)

    def test_login_sets_httponly_cookie_and_bad_key_waits(self):
        status, headers, _ = self.call("/api/login", {"key": self.key}, key=False, headers={"X-Arthur": "1"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.call("/api/login", {"key": "mala"}, key=False, headers={"X-Arthur": "1"})[0], 401)
        self.assertEqual(self.sleeps, [BAD_KEY_DELAY])
        with self.assertLogs("arthur.panel.web", "WARNING"):
            self.assertEqual(self.call("/api/today", key=False, headers={"Cookie": f"{COOKIE}=mala"})[0], 401)
        self.assertEqual(self.sleeps, [BAD_KEY_DELAY, BAD_KEY_DELAY])
        # sin X-Arthur no se puede entrar desde otra web
        self.assertEqual(self.call("/api/login", {"key": self.key}, key=False)[0], 403)

    def test_registro_de_aparatos_roto_da_503(self):
        self.devices.path.write_text("{roto", encoding="utf-8")
        with self.assertLogs("arthur.panel.web", "ERROR"):
            self.assertEqual(self.call("/api/today")[0], 503)
            self.assertEqual(self.call("/api/login", {"key": self.key}, key=False,
                                       headers={"X-Arthur": "1"})[0], 503)

    def test_logout_clears_cookie(self):
        self.assertEqual(self.call("/api/logout", raw=b"", key=False)[0], 403)
        self.assertEqual(self.call("/api/logout", raw=b"", key=False,
                                   headers={"X-Arthur": "1", "Origin": "https://mala.example"})[0], 403)
        status, headers, _ = self.call("/api/logout", raw=b"", key=False, headers={"X-Arthur": "1"})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    # --- datos ---
    def test_today_comes_from_today_fn(self):
        status, _, body = self.call("/api/today")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["date"], "2026-10-16")

    def test_add_task_understands_spanish_dates(self):
        status, _, body = self.call("/api/tasks", {"text": "pan mañana"})
        self.assertEqual(status, 200)
        tarea = json.loads(body)
        self.assertEqual((tarea["title"], tarea["due"], tarea["source"], tarea["done"]),
                         ("pan", "2026-10-17", "arthur", False))
        self.assertEqual(self.call("/api/tasks", {"text": "   "})[0], 400)
        self.assertEqual(self.call("/api/tasks", {"text": 5})[0], 400)
        self.assertEqual(self.call("/api/tasks", {"text": "x" * 201})[0], 400)
        self.assertEqual(self.call("/api/tasks", raw=b"no json", headers={"Content-Type": "application/json"})[0], 400)

    def test_toggle_task(self):
        tarea = self.store.add_task("pan", None)
        status, _, body = self.call(f"/api/tasks/{tarea.id}/toggle", raw=b"")
        self.assertEqual((status, json.loads(body)["done"]), (200, True))
        self.assertEqual(json.loads(self.call(f"/api/tasks/{tarea.id}/toggle", raw=b"")[2])["done"], False)
        self.assertEqual(self.call("/api/tasks/999/toggle", raw=b"")[0], 404)
        self.assertEqual(self.call("/api/tasks/abc/toggle", raw=b"")[0], 404)

    # --- Obsidian ---
    def test_obsidian_toggle_marks_the_line(self):
        status, _, _ = self.call("/api/obsidian/toggle", {"file": "Tareas.md", "line": 2,
                                                          "text": "Yo: llamar al banco"})
        self.assertEqual(status, 200)
        self.assertIn("- [x] Yo: llamar al banco", self.nota.read_text(encoding="utf-8"))

    def test_obsidian_changed_line_is_409_and_untouched(self):
        antes = self.nota.read_text(encoding="utf-8")
        status, _, body = self.call("/api/obsidian/toggle", {"file": "Tareas.md", "line": 2,
                                                             "text": "Yo: otra cosa"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"error": "La nota ha cambiado en Obsidian; recarga"})
        self.assertEqual(self.nota.read_text(encoding="utf-8"), antes)

    def test_obsidian_bad_requests_are_400(self):
        for body in ({"file": "../x.md", "line": 2, "text": "Yo: x"},
                     {"file": "/etc/passwd", "line": 1, "text": "Yo: x"},
                     {"file": "Tareas.md", "line": "2", "text": "Yo: llamar al banco"},
                     {"file": "Tareas.md", "line": True, "text": "Yo: llamar al banco"},
                     {"file": "Tareas.md", "line": 2, "text": None},
                     {"file": "Tareas.md", "line": 2, "text": ["Yo: llamar al banco"]},
                     {"file": 3, "line": 2, "text": "Yo: llamar al banco"}):
            with self.subTest(body=body):
                self.assertEqual(self.call("/api/obsidian/toggle", body)[0], 400)

    def test_obsidian_without_vault_is_409(self):
        self.web.vault = None
        status, _, _ = self.call("/api/obsidian/toggle", {"file": "Tareas.md", "line": 2,
                                                          "text": "Yo: llamar al banco"})
        self.assertEqual(status, 409)

    # --- resumen ---
    def test_briefing_get_and_redo(self):
        self.assertEqual(json.loads(self.call("/api/briefing")[2]), {"briefing": None})
        status, _, body = self.call("/api/briefing", raw=b"")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["briefing"]["text"], "Resumen número 1")
        guardado = json.loads(self.call("/api/briefing")[2])["briefing"]
        self.assertEqual((guardado["text"], guardado["engine"]), ("Resumen número 1", "plantilla"))

    def test_briefing_failure_is_503(self):
        def roto():
            raise RuntimeError("sin red")
        self.web.briefing_fn = roto
        with self.assertLogs("arthur.panel.web", "ERROR"):
            self.assertEqual(self.call("/api/briefing", raw=b"")[0], 503)

    # --- ficheros ---
    def test_static_files_have_fixed_names(self):
        status, headers, _ = self.call("/panel.css", key=False)
        self.assertEqual((status, headers["Content-Type"]), (200, "text/css; charset=utf-8"))
        for path in ("/panel.js", "/tokens.css", "/componentes.css", "/manifest.webmanifest", "/arthur.svg",
                     "/arthur-180.png", "/arthur-512.png", "/fonts/ibm-plex-sans-400.woff2"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, key=False)[0], 200)
        for path in ("/../store.py", "/store.py", "/web.py", "/fonts/../../web.py", "/%2e%2e/store.py"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path)[0], 404)
