"""La web de chat del servidor (fase S5): ficheros, entrar con la llave, cookie y defensas contra otras webs.

Todo en local: carpeta temporal, el servidor escucha en 127.0.0.1 en un puerto libre y el «cerebro» es una función
falsa. No se abre ningún navegador.
"""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import arthur.api.core_api as api
from arthur.api.core_api import COOKIE, CoreServer
from arthur.core.events import EventBus
from arthur.security.broker import ConfirmationBroker
from arthur.security.devices import DeviceRegistry


class CoreWebTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.devices = DeviceRegistry(Path(self.dir.name) / "devices.json")
        self.key = self.devices.add("movil")
        self.asked = []

        def handle_text(text):
            self.asked.append(text)
            return f"eco: {text}"

        bus = EventBus()
        self.server = CoreServer(bus=bus, devices=self.devices, handle_text=handle_text,
                                 broker=ConfirmationBroker(bus, timeout=5), host="127.0.0.1", port=0)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.port}"
        delay, api.BAD_KEY_DELAY = api.BAD_KEY_DELAY, 0
        self.addCleanup(setattr, api, "BAD_KEY_DELAY", delay)

    def tearDown(self):
        self.server.stop()
        self.dir.cleanup()

    def request(self, method, path, body=None, headers=None):
        """(código, cabeceras, cuerpo), sin lanzar con los errores HTTP."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def login(self, key=None, **headers):
        return self.request("POST", "/api/v1/login", {"key": key or self.key}, {"X-Arthur": "1", **headers})

    @staticmethod
    def cookie_of(response_headers):
        return response_headers["Set-Cookie"].split(";")[0]  # «arthur_llave=…»

    def test_the_page_is_served_without_key_and_with_strict_headers(self):
        for path, kind in (("/", "text/html"), ("/chat.js", "text/javascript"), ("/chat.css", "text/css"),
                           ("/mic.js", "text/javascript")):
            with self.subTest(path=path):
                code, headers, body = self.request("GET", path)
                self.assertEqual(code, 200)
                self.assertTrue(headers["Content-Type"].startswith(kind))
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("microphone=(self)", headers["Permissions-Policy"])
                self.assertTrue(body)
        # Solo los nombres fijos: ni otros ficheros ni escapes «../» (todo lo demás pide llave).
        for path in ("/index.html", "/../core_api.py", "/web/chat.js", "/%2e%2e/core_api.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 401)

    def test_the_page_never_injects_server_text_as_html(self):
        js = (api.WEB_DIR / "chat.js").read_text()
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("eval(", js)
        self.assertNotIn("<script>", (api.WEB_DIR / "index.html").read_text())  # la CSP los prohíbe

    def test_login_sets_an_httponly_strict_cookie_that_then_works(self):
        code, headers, body = self.login()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"device": "movil", "name": "ARTHUR", "voice": False, "vision": False,
                                            "push": False, "push_on": False})
        cookie = headers["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={api.COOKIE_DAYS * 86400}"):
            self.assertIn(part, cookie)
        self.assertNotIn("Secure", cookie)  # por HTTP (Tailscale) el navegador la rechazaría con Secure
        jar = {"Cookie": self.cookie_of(headers)}
        code, _, body = self.request("GET", "/api/v1/whoami", headers=jar)
        info = json.loads(body)
        self.assertEqual((code, info["device"], info["voice"]), (200, "movil", False))  # sin arthur-voz
        code, _, body = self.request("POST", "/api/v1/message", {"text": "hola"}, {**jar, "X-Arthur": "1"})
        self.assertEqual((code, json.loads(body)), (200, {"reply": "eco: hola"}))

    def test_behind_https_the_cookie_is_secure(self):
        _, headers, _ = self.login(**{"X-Forwarded-Proto": "https"})
        self.assertIn("; Secure", headers["Set-Cookie"])

    def test_bad_key_does_not_log_in(self):
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            code, headers, _ = self.login("arthur_falsa")
        self.assertEqual(code, 401)
        self.assertIsNone(headers["Set-Cookie"])
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            self.assertEqual(self.request("POST", "/api/v1/login", {"key": 3}, {"X-Arthur": "1"})[0], 401)

    def test_other_websites_cannot_use_the_session(self):
        jar = {"Cookie": self.cookie_of(self.login()[1])}
        # Sin la cabecera X-Arthur (un formulario o un fetch sencillo desde otra web): rechazado.
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            self.assertEqual(self.request("POST", "/api/v1/message", {"text": "hola"}, jar)[0], 401)
        # Con X-Arthur pero desde otro origen: rechazado.
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            code = self.request("POST", "/api/v1/message", {"text": "hola"},
                                {**jar, "X-Arthur": "1", "Origin": "https://malvada.example"})[0]
        self.assertEqual(code, 401)
        self.assertEqual(self.asked, [])
        # Desde la propia web (mismo origen): sí.
        code = self.request("POST", "/api/v1/message", {"text": "hola"},
                            {**jar, "X-Arthur": "1", "Origin": self.url})[0]
        self.assertEqual(code, 200)
        # Y entrar tampoco se puede desde otra web.
        self.assertEqual(self.request("POST", "/api/v1/login", {"key": self.key})[0], 403)
        self.assertEqual(self.login(Origin="https://malvada.example")[0], 403)

    def test_revoked_key_stops_the_cookie(self):
        jar = {"Cookie": self.cookie_of(self.login()[1])}
        self.devices.revoke("movil")
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            self.assertEqual(self.request("GET", "/api/v1/whoami", headers=jar)[0], 401)

    def test_logout_clears_the_cookie(self):
        code, headers, _ = self.request("POST", "/api/v1/logout", {}, {"X-Arthur": "1"})
        self.assertEqual(code, 200)
        self.assertTrue(headers["Set-Cookie"].startswith(f"{COOKIE}=;"))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("POST", "/api/v1/logout", {})[0], 403)

    def test_without_any_key_the_web_gets_a_quiet_401(self):
        # La web pregunta «¿quién soy?» al abrirse: sin cookie todavía no es un ataque (ni aviso ni espera).
        with self.assertNoLogs("arthur.api.core_api", "WARNING"):
            self.assertEqual(self.request("GET", "/api/v1/whoami")[0], 401)

    def test_the_web_can_talk_with_its_cookie(self):
        # La voz de la web usa las mismas rutas que el portátil: con cookie, X-Arthur obligatorio.
        jar = {"Cookie": self.cookie_of(self.login()[1])}
        req = urllib.request.Request(self.url + "/api/v1/voice", data=b"RIFF", method="POST",
                                     headers={**jar, "Content-Type": "audio/wav"})
        with self.assertLogs("arthur.api.core_api", "WARNING"), self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 401)
        e.exception.close()
        req.add_header("X-Arthur", "1")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 503)  # pasa la puerta; este servidor de prueba no tiene voz
        e.exception.close()

    def test_history_needs_the_key(self):
        self.server.get_history = lambda: [{"role": "user", "text": "hola", "photo": False}]
        jar = {"Cookie": self.cookie_of(self.login()[1])}
        code, _, body = self.request("GET", "/api/v1/history", headers=jar)
        self.assertEqual((code, json.loads(body)["messages"][0]["text"]), (200, "hola"))
        self.assertEqual(self.request("GET", "/api/v1/history")[0], 401)

    def test_a_program_with_its_key_still_works_as_before(self):
        code, _, body = self.request("POST", "/api/v1/message", {"text": "hola"},
                                     {"Authorization": f"Bearer {self.key}"})
        self.assertEqual((code, json.loads(body)), (200, {"reply": "eco: hola"}))


class InstallAsAppTest(unittest.TestCase):
    """Fase S5, paso 5: icono y manifiesto (pantalla de inicio del iPhone, app en Chrome/Edge) y lanzadores."""

    def test_icons_and_manifest_are_served(self):
        bus = EventBus()
        with tempfile.TemporaryDirectory() as d:
            server = CoreServer(bus=bus, devices=DeviceRegistry(Path(d) / "devices.json"), handle_text=lambda t: t,
                                host="127.0.0.1", port=0)
            server.start()
            try:
                base = f"http://127.0.0.1:{server.port}"
                with urllib.request.urlopen(base + "/manifest.webmanifest", timeout=5) as r:
                    manifest = json.loads(r.read())
                    self.assertEqual(r.headers["Content-Type"], "application/manifest+json")
                self.assertEqual((manifest["start_url"], manifest["display"]), ("/", "standalone"))
                for icon in manifest["icons"]:
                    with urllib.request.urlopen(base + icon["src"], timeout=5) as r:
                        body = r.read()
                        self.assertEqual(r.headers["Content-Type"], icon["type"])
                    if icon["type"] == "image/png":
                        self.assertTrue(body.startswith(b"\x89PNG"))
                    else:
                        self.assertNotIn(b"<script", body)  # un SVG puede llevar código: el nuestro no
            finally:
                server.stop()

    def test_web_address_is_checked_and_remembered(self):
        from arthur.interfaces.core import RemoteError, load_web_url, save_web_url

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "conf" / "web.txt"
            self.assertIsNone(load_web_url(path))
            self.assertEqual(save_web_url("https://arthur.example.ts.net/", path), "https://arthur.example.ts.net")
            self.assertEqual(load_web_url(path), "https://arthur.example.ts.net")
            with self.assertRaises(RemoteError):
                save_web_url("http://arthur.example.com", path)  # sin cifrar por internet: no
            path.write_text("javascript:alert(1)")
            self.assertIsNone(load_web_url(path))  # lo que no sea una dirección válida no se abre

    def test_windows_launcher(self):
        cmd = (Path(__file__).resolve().parents[1] / "scripts" / "windows" / "arthur.cmd").read_bytes()
        self.assertIn(b"\r\n", cmd)
        self.assertNotIn(b"\n", cmd.replace(b"\r\n", b""))  # todas las líneas en CRLF (si no, cmd falla)
        # Ninguna dirección de nadie (las redes de Tailscale son «tail» + 6 letras): cada uno configura la suya.
        self.assertNotRegex(cmd.decode(), r"tail[0-9a-f]{6}\.ts\.net")
        for line in cmd.decode().splitlines():
            if line.lstrip().lower().startswith("echo"):
                self.assertTrue(line.isascii(), line)  # cmd no muestra bien los acentos


if __name__ == "__main__":
    unittest.main()
