"""Página y API de clientes (arthur/clientes/web.py): servidor de verdad en 127.0.0.1:0, aparatos falsos."""

import json
import logging
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from arthur.clientes.store import ClientStore
from arthur.clientes.web import BAD_KEY_DELAY, COOKIE, WEB_FILES, ClientesWeb, WebchatKey
from arthur.security.devices import DeviceRegistry

AHORA = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
CONTACTO = {"name": "Ana Ruiz", "email": "ana.ruiz@example.com", "phone": "+34 600 11 22 33",
            "need": "Quiero una web para mi tienda", "lang": "es"}


class ClientesWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ClientStore(":memory:", lambda: AHORA)
        self.devices = DeviceRegistry(root / "devices.json")
        self.key = self.devices.add("movil")
        self.webchat = WebchatKey(root / "webchat.key")
        self.webchat_key = self.webchat.rotate()
        self.sleeps = []
        self.web = ClientesWeb(self.store, self.devices, self.webchat, host="127.0.0.1", port=0,
                               sleep=self.sleeps.append)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        # Cada petición deja una línea de registro (ruta, código, tiempo): se recoge para no ensuciar la salida.
        self.logs = self.enterContext(self.assertLogs("arthur.clientes", "DEBUG"))

    def tearDown(self):
        self.web.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def call(self, path, body=None, key=None, headers=None):
        req = urllib.request.Request(self.web.url + path, method="POST" if body is not None else "GET")
        token = self.key if key is None else key
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"null") if path.startswith("/api") \
                    else r.read()
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
                return e.code, dict(e.headers), json.loads(raw) if raw else None

    def interno(self, body=CONTACTO, key=None):
        return self.call("/api/interno/contacto", body, key=self.webchat_key if key is None else key)

    def cookie(self, **extra):
        return {"Cookie": f"{COOKIE}={self.key}", **extra}

    # --- la ruta interna (webchat) ---
    def test_internal_contact_creates_and_dedupes(self):
        status, _, data = self.interno()
        self.assertEqual((status, data), (200, {"id": 1, "nuevo": True}))
        otra = dict(CONTACTO, email="", phone="600-112-233", need="Soy Ana otra vez")
        status, _, data = self.interno(otra)
        self.assertEqual((status, data), (200, {"id": 1, "nuevo": False}))
        self.assertEqual(len(self.store.get(1)["mensajes"]), 2)
        # sin teléfono (es opcional) también vale
        status, _, data = self.interno({k: v for k, v in CONTACTO.items() if k != "phone"} | {"email": "b@ex.com"})
        self.assertEqual((status, data), (200, {"id": 2, "nuevo": True}))

    def test_internal_contact_bad_fields_400_with_field(self):
        for cambio, campo in (({"phone": "abc"}, "phone"), ({"phone": "1234"}, "phone"),
                              ({"email": "no"}, "email"), ({"need": ""}, "need"), ({"name": 5}, "name"),
                              ({"email": "", "phone": ""}, "contact")):
            status, _, data = self.interno(dict(CONTACTO, **cambio))
            self.assertEqual(status, 400, cambio)
            self.assertEqual(data["field"], campo)
        self.assertEqual(self.store.list(), [])

    def test_webchat_key_cannot_list_or_delete(self):
        self.interno()
        for path, body in (("/api/clientes", None), ("/api/clientes/1", None),
                           ("/api/clientes/1/borrar", {}), ("/api/clientes", {"nombre": "X", "email": "x@ex.com",
                                                                              "origen": "otro"})):
            status, _, _ = self.call(path, body, key=self.webchat_key, headers={"X-Arthur": "1"})
            self.assertEqual(status, 403, path)
        self.assertIsNotNone(self.store.get(1))
        # ni entrar a la página con ella
        status, _, _ = self.call("/api/login", {"key": self.webchat_key}, key="", headers={"X-Arthur": "1"})
        self.assertEqual(status, 401)

    def test_device_key_cannot_post_internal(self):
        self.assertEqual(self.interno(key=self.key)[0], 403)
        status, _, _ = self.call("/api/interno/contacto", CONTACTO, key="", headers=self.cookie(**{"X-Arthur": "1"}))
        self.assertEqual(status, 403)
        self.assertEqual(self.store.list(), [])

    def test_no_key_401(self):
        self.assertEqual(self.call("/api/interno/contacto", CONTACTO, key="")[0], 401)
        self.assertEqual(self.call("/api/clientes", key="")[0], 401)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.interno(key="arthur_mala")[0], 401)
        self.assertEqual(self.call("/api/clientes", key="arthur_mala")[0], 401)
        self.assertEqual(self.sleeps, [BAD_KEY_DELAY, BAD_KEY_DELAY])

    def test_internal_without_webchat_key_file_is_401(self):
        self.webchat.path.unlink()
        self.assertEqual(self.interno()[0], 401)

    # --- página ---
    def test_page_and_files_public_with_csp(self):
        status, headers, body = self.call("/", key="")
        self.assertEqual(status, 200)
        self.assertIn(b"clientes.js", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for path in WEB_FILES:
            self.assertEqual(self.call(path, key="")[0], 200, path)
        self.assertEqual(self.call("/../store.py", key="")[0], 401)  # nada fuera de la lista

    def test_login_cookie_and_logout(self):
        status, headers, data = self.call("/api/login", {"key": self.key}, key="", headers={"X-Arthur": "1"})
        self.assertEqual((status, data["device"]), (200, "movil"))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(self.call("/api/login", {"key": self.key}, key="")[0], 403)  # sin X-Arthur
        self.assertEqual(self.call("/api/login", {"key": "mala"}, key="", headers={"X-Arthur": "1"})[0], 401)
        self.assertEqual(self.sleeps, [BAD_KEY_DELAY])
        self.assertEqual(self.call("/api/clientes", key="", headers=self.cookie())[0], 200)
        status, headers, _ = self.call("/api/logout", {}, key="", headers={"X-Arthur": "1"})
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_list_search_and_filter(self):
        self.interno()
        self.interno(dict(CONTACTO, name="Jordi", email="jordi@correu.cat", phone=""))
        self.store.update(2, estado="presupuesto")
        status, _, data = self.call("/api/clientes")
        self.assertEqual(status, 200)
        self.assertEqual([c["numero"] for c in data["clientes"]], ["C-0002", "C-0001"])
        self.assertEqual([c["id"] for c in self.call("/api/clientes?q=ana")[2]["clientes"]], [1])
        self.assertEqual([c["id"] for c in self.call("/api/clientes?q=C-0002")[2]["clientes"]], [2])
        self.assertEqual([c["id"] for c in self.call("/api/clientes?estado=presupuesto")[2]["clientes"]], [2])
        self.assertEqual(self.call("/api/clientes?estado=raro")[0], 400)

    def test_get_marks_read(self):
        self.interno()
        self.assertEqual(self.call("/api/clientes")[2]["clientes"][0]["sin_leer"], 1)
        status, _, ficha = self.call("/api/clientes/1")
        self.assertEqual(status, 200)
        self.assertEqual(ficha["numero"], "C-0001")
        self.assertEqual(ficha["mensajes"][0]["texto"], CONTACTO["need"])
        self.assertEqual(self.store.get(1)["sin_leer"], 0)
        self.assertEqual(self.call("/api/clientes/99")[0], 404)
        self.assertEqual(self.call("/api/clientes/abc")[0], 404)

    def test_edit_client(self):
        self.interno()
        self.interno(dict(CONTACTO, name="Bea", email="bea@example.com", phone=""))
        status, _, ficha = self.call("/api/clientes/1", {"estado": "hablando", "notas": "<script>x</script>"},
                                     headers={"X-Arthur": "1"})
        self.assertEqual(status, 200)
        self.assertEqual((ficha["estado"], ficha["notas"]), ("hablando", "<script>x</script>"))
        status, _, data = self.call("/api/clientes/1", {"email": "bea@example.com"})
        self.assertEqual((status, data["field"]), (409, "duplicado"))
        status, _, data = self.call("/api/clientes/1", {"estado": "raro"})
        self.assertEqual((status, data["field"]), (400, "status"))
        status, _, data = self.call("/api/clientes/1", {"id": 5})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("/api/clientes/99", {"notas": "x"})[0], 404)

    def test_manual_create_and_message(self):
        status, _, data = self.call("/api/clientes", {"nombre": "Pep", "telefono": "600 11 22 33",
                                                      "origen": "telefono", "texto": "Llamó por la tarde"})
        self.assertEqual((status, data["id"]), (200, 1))
        status, _, ficha = self.call("/api/clientes/1/mensaje", {"texto": "Le mando presupuesto",
                                                                 "origen": "whatsapp"})
        self.assertEqual(status, 200)
        self.assertEqual([m["texto"] for m in ficha["mensajes"]], ["Llamó por la tarde", "Le mando presupuesto"])
        # el mismo teléfono desde la web cae en la misma ficha
        self.assertEqual(self.interno(dict(CONTACTO, email="", phone="600112233"))[2], {"id": 1, "nuevo": False})
        status, _, data = self.call("/api/clientes", {"nombre": "Nadie", "origen": "otro"})
        self.assertEqual((status, data["field"]), (400, "contact"))
        status, _, data = self.call("/api/clientes/1/mensaje", {"texto": "", "origen": "whatsapp"})
        self.assertEqual((status, data["field"]), (400, "text"))
        self.assertEqual(self.call("/api/clientes/9/mensaje", {"texto": "x", "origen": "otro"})[0], 404)

    def test_delete_requires_post_from_our_page(self):
        self.interno()
        # con cookie: sin X-Arthur o desde otra web, no
        self.assertEqual(self.call("/api/clientes/1/borrar", {}, key="", headers=self.cookie())[0], 403)
        evil = self.cookie(**{"X-Arthur": "1", "Origin": "https://mala.example"})
        self.assertEqual(self.call("/api/clientes/1/borrar", {}, key="", headers=evil)[0], 403)
        # por GET no se borra nada
        self.assertEqual(self.call("/api/clientes/1/borrar")[0], 404)
        self.assertIsNotNone(self.store.get(1))
        ok = self.call("/api/clientes/1/borrar", {}, key="", headers=self.cookie(**{"X-Arthur": "1"}))
        self.assertEqual(ok[0], 200)
        self.assertIsNone(self.store.get(1))
        self.assertEqual(self.call("/api/clientes/1/borrar", {})[0], 404)

    def test_bad_bodies(self):
        req = urllib.request.Request(self.web.url + "/api/clientes", method="POST", data=b"nombre=x",
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 415)
        cm.exception.close()
        self.assertEqual(self.call("/api/clientes", ["lista"])[0], 400)
        self.assertEqual(self.call("/api/clientes", {"nombre": "x" * 40000})[0], 413)

    def test_broken_devices_file_503(self):
        self.devices.path.write_text("{roto", encoding="utf-8")
        self.assertEqual(self.call("/api/clientes")[0], 503)

    def test_logs_have_no_personal_data(self):
        secreto = dict(CONTACTO, name="Zacarías Únicoapellido", email="zacarias.unico@example.com",
                       phone="+34 699 88 77 66", need="Necesito algo muy privado")
        logs = self.logs
        # todo lo que pasa en la prueba queda en self.logs (setUp)
        self.interno(secreto)
        self.interno(dict(secreto, phone="abc"))
        self.call("/api/clientes?q=zacarias")
        self.call("/api/clientes/1")
        self.call("/api/clientes/1", {"notas": "nota privadísima"})
        self.call("/api/clientes/1/mensaje", {"texto": "llamada privadísima", "origen": "telefono"})
        self.call("/api/clientes", {"nombre": "Zacarías", "email": "otro.zacarias@example.com",
                                    "origen": "persona"})
        self.call("/api/clientes", key="arthur_mala")
        self.call("/api/clientes/1/borrar", {})
        text = "\n".join(logs.output).casefold()
        for dato in ("zacar", "únicoapellido", "example.com", "699", "privad", self.webchat_key.casefold(),
                     self.key.casefold(), "arthur_mala"):
            self.assertNotIn(dato, text)
        # pero sí se apunta qué ruta, qué código y cuánto tardó
        self.assertTrue(any("/api/interno/contacto 200" in line for line in logs.output), logs.output)
        self.assertTrue(any("/api/clientes 200" in line for line in logs.output))


class PageSourceTests(unittest.TestCase):
    def test_user_text_only_with_textcontent(self):
        # Un nombre, una nota o un mensaje con «<script>» debe verse como texto: nada que interprete HTML.
        js = (Path(__file__).resolve().parents[1] / "arthur" / "clientes" / "web" / "clientes.js").read_text("utf-8")
        for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, js)
        self.assertIn("textContent", js)

    def test_page_has_no_external_resources(self):
        web = Path(__file__).resolve().parents[1] / "arthur" / "clientes" / "web"
        for name in ("index.html", "clientes.js", "clientes.css"):
            text = (web / name).read_text("utf-8")
            self.assertNotRegex(text, r"(src|href)=\"https?://", name)
            self.assertNotIn("@import", text)


class WebchatKeyTests(unittest.TestCase):
    def test_rotate_stores_only_hash_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = WebchatKey(Path(tmp) / "datos" / "webchat.key")
            first = key_file.rotate()
            self.assertTrue(key_file.check(first))
            self.assertNotIn(first, key_file.path.read_text(encoding="utf-8"))
            self.assertEqual(key_file.path.stat().st_mode & 0o777, 0o600)
            second = key_file.rotate()
            self.assertNotEqual(first, second)
            self.assertFalse(key_file.check(first))  # rotar invalida la anterior
            self.assertTrue(key_file.check(second))
            self.assertFalse(key_file.check(""))
            self.assertFalse(key_file.check(None))
            self.assertFalse(WebchatKey(Path(tmp) / "no-existe").check(second))


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
