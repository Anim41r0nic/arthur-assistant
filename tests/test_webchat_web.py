"""Servidor del chat público (arthur/webchat/web.py): servidor de verdad en 127.0.0.1:0, todo lo demás falso."""

import json
import logging
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from arthur.webchat.limits import Limits, RateLimiter
from arthur.webchat.session import Passes, Turnstile
from arthur.webchat.clientes_link import ClientesRejected
from arthur.webchat.web import WebchatWeb

ORIGIN = "https://ejemplo.com"
AHORA = datetime(2026, 10, 16, 9, 0, tzinfo=ZoneInfo("Europe/Madrid"))
SECRETO_VISITA = "ZXQ-texto-secreto-de-la-visita"
CONTACTO = {"name": "Nombre Raro Prueba", "email": "raro.prueba@example.org",
            "need": "Necesito algo muy concreto XYZ", "consent": True, "lang": "es"}
TELEFONO = "+34 611 22 33 44"


class FakeAnswerer:
    def __init__(self):
        self.calls = []

    def reply(self, history, message, lang):
        self.calls.append((history, message, lang))
        return {"reply": "Hola, soy el asistente", "offer_contact": True, "engine": "workers-ai"}


class FakeTurnstile:
    def __init__(self, result=True):
        self.result = result
        self.tokens = []

    def verify(self, token, ip):
        self.tokens.append(token)
        return self.result


class FakeNotifier:
    def __init__(self, boom=False):
        self.boom = boom
        self.sent = []

    def send(self, text, **options):
        if self.boom:
            raise RuntimeError("fallo con " + text)
        self.sent.append((text, options))
        return True


class FakeLink:
    """Hace de ClientesLink: `result` es lo que devuelve deliver ((id, nuevo), None = encolado) o una excepción."""

    def __init__(self, result=(12, True)):
        self.result = result
        self.delivered = []

    def deliver(self, contact):
        self.delivered.append(contact)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class WebchatWebTests(unittest.TestCase):
    def setUp(self):
        self.answerer = FakeAnswerer()
        self.turnstile = FakeTurnstile()
        self.notifier = FakeNotifier()
        self.contacts = FakeLink()
        self.start()

    def start(self, notifier="default", limits=None):
        if hasattr(self, "web"):
            self.web.shutdown()
        limiter = RateLimiter(limits or Limits(), lambda: AHORA)
        self.passes = Passes(b"clave-de-prueba", lambda: AHORA.timestamp())
        self.web = WebchatWeb(self.answerer, limiter, self.passes, self.turnstile, self.contacts,
                              self.notifier if notifier == "default" else notifier,
                              origin=ORIGIN, host="127.0.0.1", port=0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()

    def call(self, path, body=None, origin=ORIGIN, ip="203.0.113.7", raw=None, ctype="application/json"):
        req = urllib.request.Request(self.web.url + path, method="GET" if body is None and raw is None else "POST")
        if origin is not None:
            req.add_header("Origin", origin)
        if ip is not None:
            req.add_header("CF-Connecting-IP", ip)
        if body is not None:
            raw = json.dumps(body).encode()
        if raw is not None:
            req.data = raw
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                data = e.read()
                return e.code, dict(e.headers), json.loads(data) if data else None

    def token(self, ip="203.0.113.7"):
        status, _, data = self.call("/api/chat/session", {"turnstile": "tok"}, ip=ip)
        self.assertEqual(status, 200)
        return data["token"]

    def chat(self, token, **over):
        body = {"token": token, "lang": "es", "history": [], "message": "hola"}
        body.update(over)
        return self.call("/api/chat", body)

    # --- flujo completo ---
    def test_full_flow(self):
        status, headers, data = self.call("/api/chat/session", {"turnstile": "tok"})
        self.assertEqual(status, 200)
        self.assertIn("expires", data)
        token = data["token"]
        hist = [{"role": "visita", "text": "a"}, {"role": "asistente", "text": "b"}]
        status, _, data = self.chat(token, history=hist, lang="fr")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"reply": "Hola, soy el asistente", "offer_contact": True, "engine": "workers-ai"})
        self.assertEqual(self.answerer.calls[0][0], hist)
        status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token))
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertEqual(self.notifier.sent, [("Cliente nuevo C-0012", {"destination": "clientes", "client": 12})])
        self.assertEqual(self.contacts.delivered, [{"name": CONTACTO["name"], "email": CONTACTO["email"], "phone": "",
                                                    "need": CONTACTO["need"], "lang": "es"}])

    def test_security_headers_on_every_answer(self):
        for status, headers, _ in (self.call("/api/chat/health"), self.call("/nada"),
                                   self.call("/api/chat", {"x": 1}, origin=None)):
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotIn("Set-Cookie", headers)

    def raw_http(self, data: bytes) -> bytes:
        import socket
        host, port = self.web.httpd.server_address[:2]
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(data)
            out = b""
            while chunk := sock.recv(4096):
                out += chunk
        return out

    def assert_secure(self, raw: bytes, code: bytes):
        head, _, body = raw.partition(b"\r\n\r\n")
        low = head.lower()
        self.assertTrue(head.startswith(b"HTTP/1.0 " + code) or head.startswith(b"HTTP/1.1 " + code), head)
        self.assertIn(b"x-content-type-options: nosniff", low)
        self.assertIn(b"referrer-policy: no-referrer", low)
        self.assertIn(b"cache-control: no-store", low)
        self.assertNotIn(b"<html", body.lower())
        self.assertEqual(low.count(b"cache-control"), 1)

    def test_stdlib_errors_carry_headers(self):
        for method in (b"OPTIONS", b"PUT", b"HEAD", b"DELETE"):
            self.assert_secure(self.raw_http(method + b" /api/chat/XSS<b> HTTP/1.1\r\nHost: x\r\n"
                                             b"Connection: close\r\n\r\n"), b"501")
        self.assert_secure(self.raw_http(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n"), b"414")
        self.assert_secure(self.raw_http(b"GET / HTTP/1.1\r\nHost: x\r\nX-A: " + b"a" * 70000 + b"\r\n\r\n"),
                           b"431")
        self.assert_secure(self.raw_http(b"basura\r\n\r\n"), b"400")

    # --- origen ---
    def test_foreign_or_missing_origin_is_403(self):
        self.assertEqual(self.call("/api/chat/session", {"turnstile": "t"}, origin="https://malo.example")[0], 403)
        self.assertEqual(self.call("/api/chat/session", {"turnstile": "t"}, origin=None)[0], 403)
        self.assertEqual(self.call("/api/chat/session", {"turnstile": "t"}, origin=ORIGIN + ".malo.example")[0], 403)
        self.assertEqual(self.turnstile.tokens, [])

    # --- pase ---
    def test_pass_of_another_ip_is_401(self):
        token = self.token(ip="203.0.113.7")
        status, _, _ = self.call("/api/chat", {"token": token, "lang": "es", "history": [], "message": "hola"},
                                 ip="198.51.100.9")
        self.assertEqual(status, 401)
        status, _, _ = self.call("/api/chat/contact", dict(CONTACTO, token=token), ip="198.51.100.9")
        self.assertEqual(status, 401)
        self.assertEqual(self.answerer.calls, [])

    def test_bad_or_missing_pass_is_401(self):
        self.assertEqual(self.chat("basura")[0], 401)
        self.assertEqual(self.chat(None)[0], 401)

    def test_without_cf_header_the_socket_ip_is_limited(self):
        # sin CF-Connecting-IP todos vienen de 127.0.0.1: el 4.º pase se pasa del límite
        codes = [self.call("/api/chat/session", {"turnstile": "t"}, ip=None)[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_garbage_cf_header_falls_back_to_socket_ip(self):
        codes = [self.call("/api/chat/session", {"turnstile": "t"}, ip=f"basura{i}")[0] for i in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    # --- límites ---
    def test_21st_message_is_429(self):
        token = self.token()
        codes = [self.chat(token)[0] for _ in range(21)]
        self.assertEqual(codes[:20], [200] * 20)
        status, _, data = self.chat(token)
        self.assertEqual((codes[20], status), (429, 429))
        self.assertEqual(data, {"error": "demasiados"})

    def test_session_limit_is_429_before_turnstile(self):
        for _ in range(3):
            self.call("/api/chat/session", {"turnstile": "t"})
        status, _, data = self.call("/api/chat/session", {"turnstile": "t"})
        self.assertEqual((status, data), (429, {"error": "demasiados"}))
        self.assertEqual(len(self.turnstile.tokens), 3)

    def test_contact_limit_is_429(self):
        token = self.token()
        codes = [self.call("/api/chat/contact", dict(CONTACTO, token=token))[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    # --- Turnstile ---
    def test_turnstile_false_is_401_robot(self):
        self.turnstile.result = False
        status, _, data = self.call("/api/chat/session", {"turnstile": "tok"})
        self.assertEqual((status, data), (401, {"error": "robot"}))

    def test_turnstile_down_is_503(self):
        self.turnstile.result = None
        self.assertEqual(self.call("/api/chat/session", {"turnstile": "tok"})[0], 503)

    def test_local_token_is_not_special(self):
        self.turnstile.result = False
        status, _, _ = self.call("/api/chat/session", {"turnstile": "local"})
        self.assertEqual(status, 401)
        self.assertEqual(self.turnstile.tokens, ["local"])

    def test_real_turnstile_without_secret_is_503(self):
        self.turnstile = Turnstile(None)
        self.start()
        self.assertEqual(self.call("/api/chat/session", {"turnstile": "tok"})[0], 503)

    # --- validación ---
    def test_invalid_bodies_are_400(self):
        token = self.token()
        ok = {"role": "visita", "text": "x"}
        casos = [
            {"history": [{"role": "system", "text": "x"}]},
            {"history": [dict(ok, extra=1)]},
            {"history": [{"role": "visita"}]},
            {"history": [{"role": "visita", "text": 5}]},
            {"history": [{"role": "visita", "text": "x" * 501}]},
            {"history": ["texto"]},
            {"history": "hola"},
            {"history": [ok] * 11},
            {"message": "x" * 501},
            {"message": "   "},
            {"message": 5},
            {"message": None},
        ]
        for caso in casos:
            self.assertEqual(self.chat(token, **caso)[0], 400, caso)
        self.assertEqual(self.chat(token, history=[ok] * 10, message="x" * 500)[0], 200)
        self.assertEqual(self.answerer.calls[-1][1], "x" * 500)

    def test_missing_history_or_message_is_400(self):
        token = self.token()
        self.assertEqual(self.call("/api/chat", {"token": token, "lang": "es", "message": "hola"})[0], 400)
        self.assertEqual(self.call("/api/chat", {"token": token, "lang": "es", "history": []})[0], 400)

    def test_non_json_is_400(self):
        self.assertEqual(self.call("/api/chat/session", raw=b"{}", ctype="text/plain")[0], 400)
        self.assertEqual(self.call("/api/chat/session", raw=b"no es json")[0], 400)
        self.assertEqual(self.call("/api/chat/session", raw=b"[1,2]")[0], 400)
        self.assertEqual(self.call("/api/chat/session", raw=b"")[0], 400)

    def test_9kb_body_is_413(self):
        raw = json.dumps({"turnstile": "x" * 9000}).encode()
        self.assertEqual(self.call("/api/chat/session", raw=raw)[0], 413)
        self.assertEqual(self.turnstile.tokens, [])

    # --- contacto ---
    def test_contact_without_consent_is_400_consent(self):
        token = self.token()
        status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token, consent=False))
        self.assertEqual((status, data), (400, {"error": "consent"}))
        self.assertEqual(self.contacts.delivered, [])
        self.assertEqual(self.notifier.sent, [])

    def test_contact_invalid_email_names_field(self):
        token = self.token()
        status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token, email="roto"))
        self.assertEqual((status, data), (400, {"error": "email"}))

    def test_notifier_raising_still_200_and_saved(self):
        self.notifier = FakeNotifier(boom=True)
        self.start(self.notifier)
        token = self.token()
        with self.assertLogs("arthur", level=logging.ERROR) as cm:
            status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token))
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertEqual(len(self.contacts.delivered), 1)
        self.assertNotIn(CONTACTO["name"], "\n".join(cm.output))
        self.assertNotIn(CONTACTO["need"], "\n".join(cm.output))

    def test_no_notifier_is_fine(self):
        self.start(None)
        token = self.token()
        self.assertEqual(self.call("/api/chat/contact", dict(CONTACTO, token=token))[0], 200)

    def test_contact_with_phone_goes_to_clientes(self):
        token = self.token()
        status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token, phone=TELEFONO))
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertEqual(self.contacts.delivered[0]["phone"], TELEFONO)

    def test_bad_phone_400_field_phone(self):
        token = self.token()
        for phone in ("abc", "1234"):
            status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token, phone=phone))
            self.assertEqual((status, data), (400, {"error": "phone"}))
        self.assertEqual(self.contacts.delivered, [])
        self.assertEqual(self.notifier.sent, [])

    def test_repeat_contact_notice(self):
        self.contacts.result = (7, False)
        token = self.token()
        self.assertEqual(self.call("/api/chat/contact", dict(CONTACTO, token=token))[0], 200)
        self.assertEqual(self.notifier.sent, [("C-0007 ha vuelto a escribir", {"destination": "clientes", "client": 7})])

    def test_queued_contact_still_received_and_notice_without_destination(self):
        # `clientes` caído o lento: la visita ve «recibido»; el aviso no lleva destino (aún no hay ficha).
        self.contacts.result = None
        token = self.token()
        status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token))
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertEqual(self.notifier.sent, [("Contacto nuevo en cola (clientes no responde)", {})])

    def test_clientes_rejects_field_is_400(self):
        self.contacts.result = ClientesRejected("email")
        token = self.token()
        status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token))
        self.assertEqual((status, data), (400, {"error": "email"}))
        self.assertEqual(self.notifier.sent, [])

    def test_without_clientes_link_contact_is_503(self):
        # Sin ARTHUR_WEBCHAT_CLIENTES_URL no hay dónde guardar: se dice «no disponible», no se pierde en silencio.
        self.contacts = None
        self.start()
        token = self.token()
        status, _, data = self.call("/api/chat/contact", dict(CONTACTO, token=token))
        self.assertEqual((status, data), (503, {"error": "no disponible"}))
        self.assertEqual(self.notifier.sent, [])

    def test_queue_write_failure_is_503(self):
        self.contacts.result = OSError("disco lleno")
        token = self.token()
        with self.assertLogs("arthur", level=logging.ERROR):
            status, _, _ = self.call("/api/chat/contact", dict(CONTACTO, token=token))
        self.assertEqual(status, 503)
        self.assertEqual(self.notifier.sent, [])

    def test_unexpected_error_is_500_json_without_content_in_logs(self):
        # Un fallo que nadie esperaba (aquí, el Turnstile lanzando con texto de la visita dentro) debe dar un 500
        # JSON con las cabeceras de seguridad, no cortar la conexión ni volcar nada de la visita al registro.
        class Explota:
            def verify(self, token, ip):
                raise RuntimeError(f"fallo con {token} de {ip}")   # repite lo que recibió

        self.turnstile = Explota()
        self.start()
        with self.assertLogs("arthur", level=logging.ERROR) as cm:
            status, headers, data = self.call("/api/chat/session", {"turnstile": SECRETO_VISITA})
        self.assertEqual((status, data), (500, {"error": "interno"}))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        todo = "\n".join(cm.output)
        self.assertIn("RuntimeError", todo)
        self.assertNotIn(SECRETO_VISITA, todo)
        self.assertNotIn("203.0.113.7", todo)
        # y el servidor sigue atendiendo
        self.assertEqual(self.call("/api/chat/health")[0], 200)

    def test_lone_surrogate_in_contact_is_400(self):
        # «\ud800» es JSON válido, pero no se puede guardar ni reenviar como UTF-8: se rechaza el campo.
        token = self.token()
        raw = json.dumps(dict(CONTACTO, token=token, need="Hola \ud800 adiós")).encode()
        status, _, data = self.call("/api/chat/contact", raw=raw)
        self.assertEqual((status, data), (400, {"error": "need"}))
        self.assertEqual(self.contacts.delivered, [])

    def test_unexpected_error_in_get_is_500(self):
        class Raro:
            @property
            def _workers_ai(self):
                raise RuntimeError("roto")

        self.web.answerer = Raro()
        with self.assertLogs("arthur", level=logging.ERROR):
            status, _, data = self.call("/api/chat/health")
        self.assertEqual((status, data), (500, {"error": "interno"}))

    def test_server_errors_never_print_the_peer_address(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertLogs("arthur", level=logging.ERROR) as cm:
            try:
                raise ValueError("algo")
            except ValueError:
                self.web.httpd.handle_error(None, ("198.51.100.9", 4444))
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("198.51.100.9", "\n".join(cm.output))

    # --- rutas ---
    def test_health_without_data(self):
        status, _, data = self.call("/api/chat/health", origin=None, ip=None)
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"ok", "engines"})
        self.assertTrue(data["ok"])

    def test_unknown_route_is_404(self):
        self.assertEqual(self.call("/nada")[0], 404)
        self.assertEqual(self.call("/api/chat/otra", {"a": 1})[0], 404)

    def test_get_on_post_route_is_404_or_405(self):
        self.assertIn(self.call("/api/chat")[0], (404, 405))

    # --- registros ---
    def test_logs_never_contain_visitor_text_or_form(self):
        with self.assertLogs("arthur", level=logging.DEBUG) as cm:
            logging.getLogger("arthur.prueba").info("arranque")
            token = self.token()
            self.chat(token, message=SECRETO_VISITA, history=[{"role": "visita", "text": SECRETO_VISITA}])
            self.call("/api/chat/contact", dict(CONTACTO, token=token, phone=TELEFONO))
            self.call("/api/chat/contact", dict(CONTACTO, token=token, consent=False))
            self.contacts.result = None
            self.call("/api/chat/contact", dict(CONTACTO, token=token, phone=TELEFONO))
            self.call("/api/chat/otra?q=" + SECRETO_VISITA, {"a": 1})
            self.call("/api/chat/session", {"turnstile": "t"}, origin="https://malo.example")
        todo = "\n".join(cm.output)
        for secreto in (SECRETO_VISITA, CONTACTO["name"], CONTACTO["email"], CONTACTO["need"], TELEFONO,
                        "203.0.113.7", "malo.example", token):
            self.assertNotIn(secreto, todo)
        self.assertIn("/api/chat", todo)


if __name__ == "__main__":
    unittest.main()
