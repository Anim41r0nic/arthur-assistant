"""Tests de Google (autorización y calendario) contra un Google FALSO local. Sin internet."""

import json
import os
import tempfile
import time
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from arthur.integrations.google import (
    SCOPES, CalendarClient, GoogleAuth, GoogleError, MissingScope, NotConnected,
)
from arthur.security.audit import AuditLog
from arthur.tools.base import Risk, ToolCall
from arthur.tools.calendar import TIMEZONE, build_calendar_tools, parse_day

from .helpers import ScriptedConfirmer, make_router


class FakeKeyring:
    def __init__(self, value=None):
        self.value = value

    def store(self, secret):
        self.value = secret

    def load(self):
        return self.value

    def clear(self):
        self.value = None


class FakeGoogle:
    """Servidor local que imita el servidor de permisos y la API de Calendar."""

    def __init__(self):
        self.token_requests, self.api_requests = [], []
        self.event_posts = []  # (ruta, parámetros, cabeceras, cuerpo) de cada creación de evento
        self.token_error = None
        self.token_scope = None  # permisos que "dice" el servidor de permisos (None = no lo dice)
        self.event_reply = (200, {"id": "evento-1"})
        self.events = {}        # eventos que ya existen (id → evento, con su «etag»), para leer, cambiar y borrar
        self.event_writes = []  # (método, ruta, parámetros, cabeceras, cuerpo) de cada PATCH o DELETE
        self.write_reply = None  # (código, cuerpo) para forzar un error en un PATCH/DELETE
        self.calendar_list = [{"id": "usuario@example.com", "summary": "Personal"},
                              {"id": "uni#calendario", "summary": "Universidad"}]
        self.primary_items = None  # eventos del calendario principal en el listado (None = los de siempre)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
                if url.path != "/token":
                    fake.event_posts.append((url.path, urllib.parse.parse_qs(url.query), dict(self.headers),
                                             json.loads(raw)))
                    return self._json(*fake.event_reply)
                fields = dict(urllib.parse.parse_qsl(raw))
                fake.token_requests.append(fields)
                if fake.token_error:
                    return self._json(400, {"error": fake.token_error})
                scope = {"scope": fake.token_scope} if fake.token_scope else {}
                if fields["grant_type"] == "authorization_code":
                    return self._json(200, {"access_token": "acceso-1", "refresh_token": "permiso-duradero",
                                            "expires_in": 3600, **scope})
                return self._json(200, {"access_token": "acceso-2", "expires_in": 3600, **scope})

            def _write(self):
                url = urllib.parse.urlsplit(self.path)
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size)) if size else None
                fake.event_writes.append((self.command, url.path, urllib.parse.parse_qs(url.query),
                                          dict(self.headers), body))
                if fake.write_reply:
                    return self._json(*fake.write_reply)
                event = fake.events.get(url.path.rsplit("/", 1)[1])
                if event is None:
                    return self._json(404, {"error": {"code": 404}})
                if self.headers.get("If-Match") != event["etag"]:
                    return self._json(412, {"error": {"code": 412}})
                if self.command == "DELETE":
                    del fake.events[event["id"]]
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                event.update(body)
                event["etag"] += "x"
                return self._json(200, event)

            do_PATCH = do_DELETE = _write

            def do_GET(self):
                fake.api_requests.append((self.path, self.headers.get("Authorization")))
                path = urllib.parse.urlsplit(self.path).path
                if path.startswith("/calendar/v3/calendars/primary/events/"):
                    event = fake.events.get(path.rsplit("/", 1)[1])
                    return self._json(200, event) if event else self._json(404, {"error": {"code": 404}})
                if path.endswith("/calendarList"):
                    return self._json(200, {"items": fake.calendar_list})
                if "uni%23calendario" in path:
                    return self._json(200, {"items": [
                        {"summary": "Bases de Datos", "location": "Aula 2.1",
                         "start": {"dateTime": "2026-10-06T15:00:00+02:00"}, "end": {"dateTime": "2026-10-06T17:00:00+02:00"}},
                        {"summary": "Cancelada", "status": "cancelled",
                         "start": {"dateTime": "2026-10-06T10:00:00+02:00"}, "end": {"dateTime": "2026-10-06T11:00:00+02:00"}},
                    ]})
                if fake.primary_items is not None:
                    return self._json(200, {"items": fake.primary_items})
                return self._json(200, {"items": [
                    {"summary": "Cumpleaños de mamá", "start": {"date": "2026-10-06"}, "end": {"date": "2026-10-07"}},
                    {"summary": "Gimnasio", "start": {"dateTime": "2026-10-06T19:00:00+02:00"},
                     "end": {"dateTime": "2026-10-06T20:00:00+02:00"}},
                ]})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GoogleTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.client = Path(tmp.name) / "google-client.json"
        self.client.write_text(json.dumps({"installed": {"client_id": "id-123", "client_secret": "secreto"}}))
        self.google = FakeGoogle()
        self.addCleanup(self.google.close)
        self.keyring = FakeKeyring()
        self.auth = GoogleAuth(self.client, self.keyring, token_url=f"{self.google.url}/token")


class AuthTest(GoogleTestBase):
    def browser(self, approve=True, tamper_state=False):
        """Hace de navegador: lee la URL de Google y vuelve a ARTHUR como lo haría Google."""
        def open_browser(url):
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            self.auth_params = params
            reply = {"state": "falso" if tamper_state else params["state"]}
            reply.update({"code": "codigo-1"} if approve else {"error": "access_denied"})
            target = f"{params['redirect_uri']}/?{urllib.parse.urlencode(reply)}"

            def visit():
                try:
                    urllib.request.urlopen(target, timeout=5).read()
                except Exception:
                    pass
            threading.Thread(target=visit, daemon=True).start()
        return open_browser

    def test_authorize_stores_refresh_token_in_keyring(self):
        self.auth.authorize(["scope-calendario"], self.browser(), timeout=5)
        self.assertEqual(self.keyring.value, "permiso-duradero")
        p = self.auth_params
        self.assertTrue(p["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertEqual((p["scope"], p["code_challenge_method"], p["access_type"]), ("scope-calendario", "S256", "offline"))
        exchange = self.google.token_requests[0]
        self.assertEqual(exchange["code"], "codigo-1")
        self.assertGreaterEqual(len(exchange["code_verifier"]), 43)  # PKCE
        self.assertEqual(self.auth.access_token(), "acceso-1")
        self.assertTrue(self.auth.connected())

    def test_denied_or_forged_answers(self):
        with self.assertRaisesRegex(GoogleError, "access_denied"):
            self.auth.authorize(["s"], self.browser(approve=False), timeout=5)
        with self.assertRaisesRegex(GoogleError, "tiempo agotado"):
            self.auth.authorize(["s"], self.browser(tamper_state=True), timeout=2)
        self.assertIsNone(self.keyring.value)

    def test_without_screen_the_address_is_pasted(self):
        # El servidor: no hay mini servidor; el usuario pega la dirección a la que volvió su navegador.
        seen = {}

        def open_browser(url):
            seen.update(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

        def paste(_prompt):
            return f"  {seen['redirect_uri']}/?state={seen['state']}&code=codigo-1&scope=s  "

        self.auth.authorize(["s"], open_browser, paste=paste)
        self.assertEqual(seen["redirect_uri"], "http://127.0.0.1:53682")
        self.assertEqual(self.keyring.value, "permiso-duradero")
        self.assertEqual(self.google.token_requests[0]["redirect_uri"], "http://127.0.0.1:53682")

    def test_a_pasted_address_from_another_attempt_is_refused(self):
        with self.assertRaisesRegex(GoogleError, "no es la de esta conexión"):
            self.auth.authorize(["s"], lambda url: None,
                                paste=lambda _: "http://127.0.0.1:53682/?state=otro&code=robado")
        self.assertEqual(self.google.token_requests, [])
        self.assertIsNone(self.keyring.value)

    def test_refresh_and_revoked_permission(self):
        self.keyring.value = "permiso-duradero"
        self.assertEqual(self.auth.access_token(), "acceso-2")
        self.assertEqual(self.google.token_requests[-1]["grant_type"], "refresh_token")
        auth = GoogleAuth(self.client, FakeKeyring("viejo"), token_url=f"{self.google.url}/token")
        self.google.token_error = "invalid_grant"
        with self.assertRaisesRegex(NotConnected, "conectar-google"):
            auth.access_token()

    def test_not_configured(self):
        with self.assertRaisesRegex(NotConnected, "conectar-google"):
            GoogleAuth(self.client, FakeKeyring()).access_token()
        with self.assertRaisesRegex(NotConnected, "guía"):
            GoogleAuth(self.client.with_name("no.json"), FakeKeyring("x")).access_token()
        self.client.write_text(json.dumps({"web": {}}))
        with self.assertRaisesRegex(GoogleError, "escritorio"):
            GoogleAuth(self.client, FakeKeyring("x")).access_token()


class CalendarTest(GoogleTestBase):
    def setUp(self):
        super().setUp()
        pin_timezone(self)
        self.keyring.value = "permiso-duradero"
        client = CalendarClient(self.auth, api_url=self.google.url)
        self.tool = build_calendar_tools(client, today=lambda: date(2026, 10, 5))[0]

    def test_day_agenda(self):
        out = self.tool.run(self.tool.validate({"day": "mañana"}))
        self.assertEqual(out.splitlines(), [
            "Martes 6 de octubre:",
            "  todo el día Cumpleaños de mamá [Personal]",
            "  15:00–17:00 Bases de Datos · Aula 2.1 [Universidad]",
            "  19:00–20:00 Gimnasio [Personal]",
        ])
        self.assertNotIn("Cancelada", out)
        self.assertTrue(all(auth == "Bearer acceso-2" for _, auth in self.google.api_requests))
        events_url = [p for p, _ in self.google.api_requests if "/events?" in p][0]
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(events_url).query))
        self.assertEqual(query["singleEvents"], "true")  # repeticiones ya expandidas por Google
        self.assertTrue(query["timeMin"].startswith("2026-10-06T00:00:00"))

    def test_parse_day(self):
        monday = date(2026, 10, 5)
        self.assertEqual(parse_day("hoy", monday), monday)
        self.assertEqual(parse_day("pasado mañana", monday), date(2026, 10, 7))
        self.assertEqual(parse_day("jueves", monday), date(2026, 10, 8))
        self.assertEqual(parse_day("el miércoles", monday), date(2026, 10, 7))
        self.assertEqual(parse_day("lunes", monday), monday)
        self.assertEqual(parse_day("2026-12-24", monday), date(2026, 12, 24))
        with self.assertRaises(Exception):
            parse_day("el día del juicio", monday)


def pin_timezone(testcase, name="Europe/Madrid"):
    """Las horas del listado salen en hora local (la de Madrid): se fija durante el test y se restaura."""
    before = os.environ.get("TZ")

    def restore():
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()

    os.environ["TZ"] = name
    time.tzset()
    testcase.addCleanup(restore)


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=TIMEZONE)  # lunes 5 de octubre de 2026, 12:00 en Madrid
EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
READ_SCOPES = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/gmail.readonly"


class CreateEventTest(GoogleTestBase):
    def setUp(self):
        super().setUp()
        self.keyring.value = "permiso-duradero"
        self.google.token_scope = f"{READ_SCOPES} {EVENTS_SCOPE}"
        self.client_api = CalendarClient(self.auth, api_url=self.google.url)
        self.confirmer = ScriptedConfirmer()
        self.router = self.make_router(NOW)

    def make_router(self, now):
        tools = build_calendar_tools(self.client_api, today=lambda: now.date(), now=lambda: now)
        return make_router(tools, confirmer=self.confirmer)

    def create(self, **args):
        args.setdefault("title", "Dentista")
        args.setdefault("date", "2026-10-08")
        return self.router.execute(ToolCall("create_event", args))

    def asked_nothing_and_sent_nothing(self):
        self.assertEqual(self.confirmer.requests, [])
        self.assertEqual(self.google.event_posts, [])
        self.assertEqual(self.google.token_requests, [])

    # --- casos válidos: pide confirmación y la petición a Google es exactamente la esperada ---------------

    def test_timed_event_is_confirmed_and_sent_exactly(self):
        self.confirmer.answers = [True]
        result = self.create(start="17:00")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output, "He apuntado en tu calendario: Dentista, jueves 8/10 de 17:00 a 18:00")
        request = self.confirmer.requests[0]
        self.assertEqual(request.summary, "Crear en tu calendario: Dentista, jueves 8/10 de 17:00 a 18:00")
        self.assertEqual(request.risk, Risk.WRITE)
        ((path, query, headers, body),) = self.google.event_posts
        self.assertEqual(path, "/calendar/v3/calendars/primary/events")
        self.assertEqual(query, {"sendUpdates": ["none"]})  # Google no avisa a nadie
        self.assertEqual(headers["Authorization"], "Bearer acceso-2")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(body, {
            "summary": "Dentista",
            "start": {"dateTime": "2026-10-08T17:00:00", "timeZone": "Europe/Madrid"},
            "end": {"dateTime": "2026-10-08T18:00:00", "timeZone": "Europe/Madrid"},
        })  # ni invitados ni nada más

    def test_explicit_end_and_location(self):
        self.confirmer.answers = [True]
        result = self.create(title="Cena con Laura", date="2026-10-10", start="21:00", end="23:00",
                             location="Casa de Laura")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.confirmer.requests[0].summary,
                         "Crear en tu calendario: Cena con Laura, sábado 10/10 de 21:00 a 23:00 (lugar: Casa de Laura)")
        body = self.google.event_posts[0][3]
        self.assertEqual(body["location"], "Casa de Laura")
        self.assertEqual(body["end"]["dateTime"], "2026-10-10T23:00:00")

    def test_all_day_event_ends_the_next_day_for_google(self):
        self.confirmer.answers = [True]
        result = self.create(title="Examen de programación", date="2026-10-20", all_day=True)
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.confirmer.requests[0].summary,
                         "Crear en tu calendario: Examen de programación, martes 20/10 (todo el día)")
        self.assertEqual(self.google.event_posts[0][3], {
            "summary": "Examen de programación",
            "start": {"date": "2026-10-20"}, "end": {"date": "2026-10-21"},
        })

    def test_other_year_is_shown_in_the_summary(self):
        self.confirmer.answers = [True]
        self.create(date="2027-01-14", start="09:30")
        self.assertIn("jueves 14/1/2027 de 09:30 a 10:30", self.confirmer.requests[0].summary)

    def test_boundaries_are_allowed(self):
        for kwargs in ({"date": "2026-10-05", "start": "12:01"},          # hoy, un minuto después de ahora
                       {"date": "2026-10-05", "all_day": True},           # todo el día de hoy
                       {"date": "2028-10-04", "start": "10:00"},          # 730 días vista
                       {"date": "2026-10-08", "start": "9:05", "end": "23:59"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.create(**kwargs).status, "cancelled")  # llegó a la confirmación (NO)
        self.assertEqual(len(self.confirmer.requests), 4)
        self.assertEqual(self.google.event_posts, [])

    # --- nada se envía sin confirmar -----------------------------------------------------------------------

    def test_nothing_is_sent_without_confirmation(self):
        result = self.create(start="17:00")  # el confirmador responde NO
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(len(self.confirmer.requests), 1)
        self.assertEqual(self.google.event_posts, [])
        self.assertEqual(self.google.token_requests, [])  # ni siquiera se ha pedido el permiso a Google

    def test_safe_mode_denies_it(self):
        router = make_router(build_calendar_tools(self.client_api, now=lambda: NOW), safe_mode=True,
                             confirmer=ScriptedConfirmer(True))
        self.assertEqual(router.execute(ToolCall("create_event", {"title": "x", "date": "2026-10-08",
                                                                  "start": "17:00"})).status, "denied")
        self.assertEqual(self.google.event_posts, [])

    # --- datos inválidos: ni se pregunta ni se envía --------------------------------------------------------

    def test_invalid_data_is_rejected_before_asking(self):
        cases = [
            ({"date": "2026-02-30", "start": "17:00"}, "Fecha no válida"),
            ({"date": "2026-13-01", "start": "17:00"}, "Fecha no válida"),
            ({"date": "20261008", "start": "17:00"}, "Fecha no válida"),
            ({"date": "2026-W41-4", "start": "17:00"}, "Fecha no válida"),
            ({"date": "2026-10-8", "start": "17:00"}, "Fecha no válida"),
            ({"date": "jueves", "start": "17:00"}, "Fecha no válida"),
            ({"date": "2026-10-08", "start": "25:00"}, "hora de inicio no válida"),
            ({"date": "2026-10-08", "start": "17:60"}, "hora de inicio no válida"),
            ({"date": "2026-10-08", "start": "5pm"}, "hora de inicio no válida"),
            ({"date": "2026-10-08", "start": "17:00", "end": "9"}, "hora de fin no válida"),
            ({"date": "2026-10-08", "start": "17:00", "end": "17:00"}, "posterior"),
            ({"date": "2026-10-08", "start": "17:00", "end": "16:00"}, "posterior"),
            ({"date": "2026-10-08", "start": "23:30"}, "día siguiente"),
            ({"date": "2026-10-08"}, "Falta la hora de inicio"),
            ({"date": "2026-10-08", "all_day": True, "start": "10:00"}, "todo el día no lleva hora"),
            ({"date": "2026-10-08", "start": "17:00", "title": "   "}, "Falta el título"),
            ({"date": "2026-10-08", "start": "17:00", "title": "Hola\nBye"}, "caracteres no válidos"),
            ({"date": "2026-10-08", "start": "17:00", "location": "a\x00b"}, "caracteres no válidos"),
            # el pasado
            ({"date": "2026-10-04", "start": "17:00"}, "ya ha pasado"),
            ({"date": "2026-10-05", "start": "11:59"}, "ya ha pasado"),
            ({"date": "2026-10-05", "start": "12:00"}, "ya ha pasado"),
            ({"date": "2026-10-04", "all_day": True}, "ya ha pasado"),
            ({"date": "2020-01-01", "start": "10:00"}, "ya ha pasado"),
            # demasiado lejos
            ({"date": "2028-10-05", "start": "10:00"}, "2 años"),
            ({"date": "2099-01-01", "all_day": True}, "2 años"),
        ]
        for args, error in cases:
            with self.subTest(args=args):
                result = self.create(**args)
                self.assertEqual(result.status, "error")
                self.assertIn(error, result.output)
        self.asked_nothing_and_sent_nothing()

    def test_argument_limits_and_unknown_arguments(self):
        for args in ({"title": "x" * 201, "start": "17:00"},
                     {"start": "17:00", "location": "x" * 201},
                     {"start": "17:00", "attendees": ["alguien@example.com"]},  # nadie invitado: ni existe el campo
                     {"start": "17:00", "all_day": "si"},
                     {"start": 1700}):
            with self.subTest(args=list(args)):
                self.assertEqual(self.create(**args).status, "error")
        self.asked_nothing_and_sent_nothing()

    def test_a_time_that_does_not_exist_is_rejected(self):
        # 29/3/2026: a las 02:00 los relojes de Madrid saltan a las 03:00, así que las 02:30 no existen.
        router = self.make_router(datetime(2026, 3, 1, 12, 0, tzinfo=TIMEZONE))
        result = router.execute(ToolCall("create_event", {"title": "x", "date": "2026-03-29", "start": "02:30"}))
        self.assertEqual(result.status, "error")
        self.assertIn("cambio de hora", result.output)
        self.asked_nothing_and_sent_nothing()

    def test_timezone_is_madrid_whatever_the_computer_says(self):
        # 22:30 en Madrid (UTC+2) ya es «mañana» en Nueva Zelanda: se cuenta con la hora de Madrid, no la del PC.
        late = datetime(2026, 10, 5, 22, 30, tzinfo=TIMEZONE).astimezone(datetime.now().astimezone().tzinfo)
        router = self.make_router(late)
        self.assertEqual(router.execute(ToolCall("create_event", {"title": "x", "date": "2026-10-05",
                                                                  "start": "22:29"})).status, "error")
        self.assertEqual(router.execute(ToolCall("create_event", {"title": "x", "date": "2026-10-05",
                                                                  "start": "22:31"})).status, "cancelled")

    def test_event_that_passes_while_waiting_for_confirmation_is_not_created(self):
        clock = [NOW]
        tools = build_calendar_tools(self.client_api, now=lambda: clock[0])

        class SlowUser:
            requests = []

            def confirm(self, request):
                clock[0] = datetime(2026, 10, 8, 17, 5, tzinfo=TIMEZONE)  # tarda: ya son las 17:05 del jueves
                return True

        router = make_router(tools, confirmer=SlowUser())
        result = router.execute(ToolCall("create_event", {"title": "x", "date": "2026-10-08", "start": "17:00"}))
        self.assertEqual(result.status, "error")
        self.assertEqual(self.google.event_posts, [])

    # --- permisos y fallos de Google ---------------------------------------------------------------------

    def test_missing_permission_says_to_reconnect_and_sends_nothing(self):
        self.google.token_scope = READ_SCOPES  # conectado antes de existir «crear eventos»
        self.confirmer.answers = [True]
        result = self.create(start="17:00")
        self.assertEqual(result.status, "error")
        self.assertIn("arthur --conectar-google", result.output)
        self.assertEqual(self.google.event_posts, [])

    def test_google_refusing_for_lack_of_permission_says_to_reconnect(self):
        self.google.token_scope = None  # el servidor de permisos no dice nada: lo dirá la API
        self.google.event_reply = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "errors": [
            {"reason": "insufficientPermissions", "message": "Insufficient Permission"}]}})
        self.confirmer.answers = [True]
        result = self.create(start="17:00")
        self.assertEqual(result.status, "error")
        self.assertIn("arthur --conectar-google", result.output)
        self.assertEqual(len(self.google.event_posts), 1)  # se intenta una vez y no se repite

    def test_other_google_errors_are_explained_and_not_retried(self):
        self.google.event_reply = (500, {"error": {"message": "boom"}})
        self.confirmer.answers = [True]
        result = self.create(start="17:00")
        self.assertEqual((result.status, len(self.google.event_posts)), ("error", 1))
        self.assertIn("500", result.output)

    def test_unconfirmed_creation_by_google_is_not_reported_as_done(self):
        self.google.event_reply = (200, {})
        self.confirmer.answers = [True]
        result = self.create(start="17:00")
        self.assertEqual(result.status, "error")
        self.assertIn("mira tu calendario", result.output)

    def test_timeout_after_sending_warns_that_it_may_exist(self):
        class Slow:
            def open(self, request, timeout):
                raise TimeoutError

        client = CalendarClient(self.auth, api_url=self.google.url, opener=Slow())
        with self.assertRaisesRegex(GoogleError, "no sé si se ha guardado"):
            client.create_event({"summary": "x", "start": {"date": "2026-10-08"}, "end": {"date": "2026-10-09"}})

    def test_client_only_accepts_the_known_fields(self):
        with self.assertRaisesRegex(GoogleError, "attendees"):
            self.client_api.create_event({"summary": "x", "attendees": [{"email": "a@example.org"}],
                                          "start": {"date": "2026-10-08"}, "end": {"date": "2026-10-09"}})
        self.assertEqual(self.google.event_posts, [])

    def test_missing_scope_is_its_own_error(self):
        self.google.token_scope = READ_SCOPES
        with self.assertRaises(MissingScope):
            self.client_api.create_event({"summary": "x", "start": {"date": "2026-10-08"},
                                          "end": {"date": "2026-10-09"}})

    def test_scopes_asked_on_connect(self):
        # Solo se añade «calendar.events»; Gmail sigue en solo lectura (nada de enviar, componer ni modificar).
        self.assertEqual(sorted(SCOPES), sorted([
            "https://www.googleapis.com/auth/calendar.readonly", EVENTS_SCOPE,
            "https://www.googleapis.com/auth/gmail.readonly"]))

    def test_google_is_only_ever_asked_to_create(self):
        self.confirmer.answers = [True]
        self.create(start="17:00")
        self.assertEqual(len(self.google.event_posts), 1)
        self.assertEqual(self.google.api_requests, [])  # ni lecturas, ni borrados, ni cambios


def timed(event_id, title, start, end, **more):
    return {"id": event_id, "etag": f'"v-{event_id}"', "status": "confirmed", "summary": title,
            "start": {"dateTime": start, "timeZone": "Europe/Madrid"},
            "end": {"dateTime": end, "timeZone": "Europe/Madrid"}, **more}


def all_day(event_id, title, start, end, **more):
    return {"id": event_id, "etag": f'"v-{event_id}"', "status": "confirmed", "summary": title,
            "start": {"date": start}, "end": {"date": end}, **more}


DENTIST = "abc12345"   # jueves 8/10/2026, 17:00–18:00
EXAM = "all12345"      # martes 20/10/2026, todo el día


class EditEventsTestBase(GoogleTestBase):
    def setUp(self):
        super().setUp()
        self.keyring.value = "permiso-duradero"
        self.google.token_scope = f"{READ_SCOPES} {EVENTS_SCOPE}"
        self.google.events = {e["id"]: e for e in [
            timed(DENTIST, "Dentista", "2026-10-08T17:00:00+02:00", "2026-10-08T18:00:00+02:00",
                  location="Clínica Sol"),
            all_day(EXAM, "Examen", "2026-10-20", "2026-10-21"),
            timed("rec12345", "Gimnasio", "2026-10-09T19:00:00+02:00", "2026-10-09T20:00:00+02:00",
                  recurringEventId="rec00000"),
            timed("inv12345", "Reunión", "2026-10-09T10:00:00+02:00", "2026-10-09T11:00:00+02:00",
                  attendees=[{"email": "otra@example.com"}, {"email": "usuario@example.com", "self": True}]),
            timed("bd123456", "Cumple", "2026-10-09T10:00:00+02:00", "2026-10-09T11:00:00+02:00",
                  eventType="birthday"),
            all_day("multi123", "Viaje", "2026-10-20", "2026-10-23"),
            timed("night123", "Fiesta", "2026-10-09T23:00:00+02:00", "2026-10-10T02:00:00+02:00"),
            timed("past1234", "Ayer", "2026-10-04T10:00:00+02:00", "2026-10-04T11:00:00+02:00"),
            {**timed("canc1234", "Borrado", "2026-10-09T10:00:00+02:00", "2026-10-09T11:00:00+02:00"),
             "status": "cancelled"},
        ]}
        self.client_api = CalendarClient(self.auth, api_url=self.google.url)
        self.confirmer = ScriptedConfirmer()

    def make_router(self, now=NOW, **security):
        tools = build_calendar_tools(self.client_api, today=lambda: now.date(), now=lambda: now)
        return make_router(tools, confirmer=self.confirmer, **security)

    def nothing_written(self):
        self.assertEqual(self.google.event_writes, [])
        self.assertEqual(self.google.event_posts, [])


class ListedIdsTest(EditEventsTestBase):
    def test_only_editable_events_show_their_id(self):
        pin_timezone(self)
        self.google.calendar_list = [{"id": "usuario@example.com", "summary": "Personal", "primary": True},
                                     {"id": "uni#calendario", "summary": "Universidad"}]
        self.google.primary_items = [
            timed(DENTIST, "Dentista", "2026-10-06T17:00:00+02:00", "2026-10-06T18:00:00+02:00"),
            timed("rec12345", "Gimnasio", "2026-10-06T19:00:00+02:00", "2026-10-06T20:00:00+02:00",
                  recurringEventId="rec00000"),
            timed("inv12345", "Reunión", "2026-10-06T10:00:00+02:00", "2026-10-06T11:00:00+02:00",
                  attendees=[{"email": "otra@example.com"}]),
        ]
        tool = build_calendar_tools(self.client_api, today=lambda: date(2026, 10, 5))[0]
        out = tool.run(tool.validate({"day": "mañana"}))
        lines = out.splitlines()
        self.assertIn("  17:00–18:00 Dentista [Personal] #1", lines)  # un número, no el id largo de Google
        self.assertIn("  19:00–20:00 Gimnasio [Personal]", lines)      # se repite: sin id
        self.assertIn("  10:00–11:00 Reunión [Personal]", lines)       # con invitados: sin id
        self.assertTrue(any("Bases de Datos" in line and "#" not in line for line in lines))  # otro calendario


class EventNumbersTest(EditEventsTestBase):
    """El modelo pequeño inventaba ids («1»): ahora usa números #N de la última lista y la herramienta traduce."""

    def setUp(self):
        super().setUp()
        pin_timezone(self)
        self.google.calendar_list = [{"id": "usuario@example.com", "summary": "Personal", "primary": True}]
        self.listed = [self.google.events[k] for k in (DENTIST, "inv12345", "rec12345", EXAM)]
        self.google.primary_items = self.listed
        tools = build_calendar_tools(self.client_api, today=lambda: NOW.date(), now=lambda: NOW)
        self.router = make_router(tools, confirmer=self.confirmer)

    def call(self, tool, **args):
        return self.router.execute(ToolCall(tool, args))

    def list_events(self):
        result = self.call("calendar_events", day="2026-10-08", days=7)
        self.assertEqual(result.status, "ok")
        return result.output

    def patch_paths(self):
        return [path for method, path, *_ in self.google.event_writes]

    def test_listing_numbers_only_editable_events(self):
        lines = self.list_events().splitlines()
        self.assertIn("  17:00–18:00 Dentista · Clínica Sol [Personal] #1", lines)
        self.assertIn("  todo el día Examen [Personal] #2", lines)
        self.assertIn("  19:00–20:00 Gimnasio [Personal]", lines)   # se repite: sin número
        self.assertIn("  10:00–11:00 Reunión [Personal]", lines)    # con invitados: sin número
        self.assertNotIn(DENTIST, "\n".join(lines))                # el id real no sale de la herramienta

    def test_update_by_number_shows_title_date_and_weekday_never_just_the_number(self):
        self.list_events()
        self.confirmer.answers = [True]
        result = self.call("update_event", id="#1", date="2026-10-09")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.patch_paths(), [f"/calendar/v3/calendars/primary/events/{DENTIST}"])
        summary = self.confirmer.requests[0].summary
        self.assertIn("Dentista, jueves 8/10 de 17:00 a 18:00", summary)
        self.assertIn("Dentista, viernes 9/10 de 17:00 a 18:00", summary)
        self.assertNotIn("#1", summary)
        self.assertNotIn(DENTIST, summary)

    def test_a_bare_number_works_too(self):
        self.list_events()
        self.confirmer.answers = [True]
        self.assertEqual(self.call("update_event", id="1", title="Revisión").status, "ok")
        self.assertEqual(self.patch_paths(), [f"/calendar/v3/calendars/primary/events/{DENTIST}"])

    def test_delete_by_number_and_the_number_dies_with_the_event(self):
        self.list_events()
        self.confirmer.answers = [True]
        result = self.call("delete_event", id="#2")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.patch_paths(), [f"/calendar/v3/calendars/primary/events/{EXAM}"])
        self.assertIn("BORRAR de tu calendario: Examen, martes 20/10 (todo el día)", self.confirmer.requests[0].summary)
        again = self.call("delete_event", id="#2")
        self.assertEqual(again.status, "error")
        self.assertIn("Llama primero a calendar_events y usa el número #N de la lista", again.output)
        self.assertEqual(len(self.confirmer.requests), 1)  # no se vuelve a preguntar por algo que ya no existe

    def test_unknown_number_asks_to_list_first_without_asking_or_calling_google(self):
        self.list_events()
        requests_before = len(self.google.api_requests)
        for ref in ("#9", "9", "0", "#0"):
            with self.subTest(ref=ref):
                for tool, extra in (("update_event", {"title": "Otro"}), ("delete_event", {})):
                    result = self.call(tool, id=ref, **extra)
                    self.assertEqual(result.status, "error")
                    self.assertEqual(result.output, "Llama primero a calendar_events y usa el número #N de la lista")
        self.assertEqual(len(self.google.api_requests), requests_before)  # ni una consulta más
        self.assertEqual(self.confirmer.requests, [])
        self.nothing_written()

    def test_without_a_table_numbers_do_not_work(self):
        for tool, extra in (("update_event", {"title": "Otro"}), ("delete_event", {})):
            result = self.call(tool, id="#1", **extra)
            self.assertEqual(result.status, "error")
            self.assertEqual(result.output, "Llama primero a calendar_events y usa el número #N de la lista")
        self.assertEqual(self.google.api_requests, [])
        self.nothing_written()

    def test_real_google_ids_still_work_with_and_without_a_table(self):
        self.confirmer.answers = [True, True]
        self.assertEqual(self.call("update_event", id=DENTIST, title="Uno").status, "ok")   # sin tabla
        self.list_events()
        self.assertEqual(self.call("update_event", id=DENTIST, title="Dos").status, "ok")   # con tabla
        self.assertEqual(self.patch_paths(), [f"/calendar/v3/calendars/primary/events/{DENTIST}"] * 2)

    def test_garbage_references_are_rejected(self):
        self.list_events()
        for ref in ("#abc", "-1", "uno", "# 1", "1 2", "../x", "", "#12345678"):
            with self.subTest(ref=ref):
                result = self.call("update_event", id=ref, title="Otro")
                self.assertEqual(result.status, "error")
                self.assertIn("Llama primero a calendar_events", result.output)
        self.nothing_written()

    def test_five_digit_numbers_are_ids_not_table_entries(self):
        self.list_events()  # hay un #1, pero «12345» no es un número de la tabla: es un id (que no existe)
        result = self.call("update_event", id="12345", title="Otro")
        self.assertEqual(result.status, "error")
        self.assertIn("No encuentro ese evento", result.output)
        self.nothing_written()

    def test_the_table_is_renewed_on_every_listing(self):
        self.list_events()
        self.google.primary_items = [self.google.events[EXAM]]
        self.assertIn("todo el día Examen [Personal] #1", self.list_events())
        self.confirmer.answers = [True]
        self.assertEqual(self.call("update_event", id="#1", title="Examen final").status, "ok")
        self.assertEqual(self.patch_paths(), [f"/calendar/v3/calendars/primary/events/{EXAM}"])  # el #1 de ahora
        result = self.call("update_event", id="#2", title="Otro")  # el #2 de antes ya no existe
        self.assertIn("Llama primero a calendar_events", result.output)

    def test_an_empty_listing_also_clears_the_table(self):
        self.list_events()
        self.google.primary_items = []
        self.assertEqual(self.call("calendar_events", day="2026-10-08").output, "No tienes nada en el calendario ese día")
        self.assertIn("Llama primero a calendar_events", self.call("delete_event", id="#1").output)

    def test_what_you_confirm_is_what_changes_even_if_the_table_is_renewed_meanwhile(self):
        self.list_events()
        test, router = self, self.router

        class ListsWhileYouDecide:
            requests = []

            def confirm(self, request):
                test.google.primary_items = [test.google.events[EXAM]]
                router.execute(ToolCall("calendar_events", {"day": "2026-10-08", "days": 7}))  # ahora #1 = Examen
                return True

        tools = build_calendar_tools(self.client_api, today=lambda: NOW.date(), now=lambda: NOW)
        self.router = router = make_router(tools, confirmer=ListsWhileYouDecide())
        self.list_events()
        self.google.primary_items = self.listed
        self.list_events()  # #1 = Dentista
        result = self.call("update_event", id="#1", title="Otro")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.patch_paths(), [f"/calendar/v3/calendars/primary/events/{DENTIST}"])

    def test_create_event_returns_a_number_to_change_or_delete_it_later(self):
        self.google.events["nuevo1234"] = timed("nuevo1234", "Cena", "2026-10-10T21:00:00+02:00",
                                                "2026-10-10T22:00:00+02:00")
        self.google.event_reply = (200, {"id": "nuevo1234"})
        self.confirmer.answers = [True, True, True]
        created = self.call("create_event", title="Cena", date="2026-10-10", start="21:00")
        self.assertEqual(created.status, "ok")
        self.assertEqual(created.output, "He apuntado en tu calendario: Cena, sábado 10/10 de 21:00 a 22:00 (#1)")
        changed = self.call("update_event", id="#1", start="21:30")
        self.assertEqual(changed.status, "ok")
        self.assertIn("Cena, sábado 10/10 de 21:00 a 22:00  →  Cena, sábado 10/10 de 21:30 a 22:30", changed.output)
        self.assertEqual(self.patch_paths(), ["/calendar/v3/calendars/primary/events/nuevo1234"])
        self.assertEqual(self.call("delete_event", id="#1").status, "ok")
        self.assertNotIn("nuevo1234", self.google.events)

    def test_create_event_continues_the_numbering_of_the_last_list(self):
        self.list_events()  # #1 y #2
        self.google.event_reply = (200, {"id": "nuevo1234"})
        self.confirmer.answers = [True]
        created = self.call("create_event", title="Cena", date="2026-10-10", start="21:00")
        self.assertTrue(created.output.endswith("(#3)"))

    def test_create_event_with_a_strange_id_is_created_without_a_number(self):
        self.confirmer.answers = [True]
        created = self.call("create_event", title="Cena", date="2026-10-10", start="21:00")  # el id por defecto del falso
        self.assertEqual(created.status, "ok")
        self.assertNotIn("#", created.output)

    def test_tool_descriptions_stay_short_and_mention_the_number(self):
        schemas = {t.name: t.json_schema() for t in self.router.registry.all()}
        for name in ("calendar_events", "update_event", "delete_event"):
            self.assertIn("#N", schemas[name]["description"])
            self.assertLess(len(schemas[name]["description"]), 100)


class UpdateEventTest(EditEventsTestBase):
    def update(self, router=None, **args):
        args.setdefault("id", DENTIST)
        return (router or self.make_router()).execute(ToolCall("update_event", args))

    def patched(self):
        ((method, path, query, headers, body),) = self.google.event_writes
        self.assertEqual((method, path), ("PATCH", f"/calendar/v3/calendars/primary/events/{DENTIST}"))
        self.assertEqual(query, {"sendUpdates": ["none"]})
        self.assertEqual(headers["If-Match"], f'"v-{DENTIST}"')  # la versión que viste al confirmar
        return body

    def test_changing_the_day_shows_before_and_after_and_sends_only_that(self):
        self.confirmer.answers = [True]
        result = self.update(date="2026-10-09")
        self.assertEqual(result.status, "ok")
        request = self.confirmer.requests[0]
        self.assertEqual(request.risk, Risk.WRITE)
        self.assertEqual(request.summary,
                         "Cambiar en tu calendario: Dentista, jueves 8/10 de 17:00 a 18:00 (lugar: Clínica Sol)  →  "
                         "Dentista, viernes 9/10 de 17:00 a 18:00 (lugar: Clínica Sol)")
        self.assertEqual(self.patched(), {
            "start": {"dateTime": "2026-10-09T17:00:00", "timeZone": "Europe/Madrid"},
            "end": {"dateTime": "2026-10-09T18:00:00", "timeZone": "Europe/Madrid"}})  # sin título ni invitados
        self.assertIn("He cambiado en tu calendario", result.output)

    def test_event_is_looked_up_before_asking(self):
        self.confirmer.answers = [True]
        self.update(title="Revisión")
        self.assertEqual(self.google.api_requests[0][0], f"/calendar/v3/calendars/primary/events/{DENTIST}")
        self.assertEqual(self.google.event_writes[0][0], "PATCH")

    def test_title_only(self):
        self.confirmer.answers = [True]
        self.assertEqual(self.update(title="Revisión dental").status, "ok")
        self.assertEqual(self.patched(), {"summary": "Revisión dental"})

    def test_new_start_keeps_the_duration(self):
        self.confirmer.answers = [True]
        self.update(start="16:30")
        body = self.patched()
        self.assertEqual((body["start"]["dateTime"], body["end"]["dateTime"]),
                         ("2026-10-08T16:30:00", "2026-10-08T17:30:00"))

    def test_new_end_and_location(self):
        self.confirmer.answers = [True]
        self.update(end="19:00", location="Otra clínica")
        body = self.patched()
        self.assertEqual((body["start"]["dateTime"], body["end"]["dateTime"], body["location"]),
                         ("2026-10-08T17:00:00", "2026-10-08T19:00:00", "Otra clínica"))

    def test_all_day_event_can_change_day_but_not_get_hours(self):
        self.confirmer.answers = [True]
        self.assertEqual(self.update(id=EXAM, date="2026-10-22").status, "ok")
        ((_, _, _, _, body),) = self.google.event_writes
        self.assertEqual(body, {"start": {"date": "2026-10-22"}, "end": {"date": "2026-10-23"}})
        self.google.event_writes.clear()
        result = self.update(id=EXAM, start="10:00")
        self.assertEqual(result.status, "error")
        self.assertIn("bórralo y crea uno nuevo", result.output)

    def test_nothing_is_changed_without_confirmation(self):
        result = self.update(title="Otro")  # el confirmador responde NO
        self.assertEqual(result.status, "cancelled")
        self.nothing_written()
        self.assertEqual(self.google.events[DENTIST]["summary"], "Dentista")

    def test_invalid_requests_are_rejected_before_asking(self):
        cases = [
            ({}, "No has indicado qué cambiar"),
            ({"title": "   "}, "No has indicado qué cambiar"),
            ({"title": "Dentista"}, "ya está así"),
            ({"date": "2026-10-08", "start": "17:00", "end": "18:00"}, "ya está así"),
            ({"date": "2026-02-30"}, "Fecha no válida"),
            ({"date": "jueves"}, "Fecha no válida"),
            ({"date": "2026-10-04"}, "ya ha pasado"),            # mover al pasado
            ({"start": "10:00", "date": "2026-10-05"}, "ya ha pasado"),
            ({"date": "2028-10-05"}, "2 años"),
            ({"start": "25:00"}, "hora de inicio no válida"),
            ({"end": "16:00"}, "posterior"),
            ({"start": "23:30"}, "medianoche"),                 # 1 h de duración llegaría al día siguiente
            ({"title": "a\nb"}, "caracteres no válidos"),
        ]
        for args, error in cases:
            with self.subTest(args=args):
                result = self.update(**args)
                self.assertEqual(result.status, "error")
                self.assertIn(error, result.output)
        self.assertEqual(self.confirmer.requests, [])
        self.nothing_written()

    def test_ids_must_come_from_google_not_from_free_text(self):
        for bad in ("../../calendarList", "Dentista", "ABC12345", "abc 12345", "a", "", DENTIST + "/x", "x" * 101):
            with self.subTest(bad=bad):
                self.assertEqual(self.update(id=bad, title="Otro").status, "error")
        self.assertEqual(self.google.api_requests, [])  # ni siquiera se consulta a Google
        self.assertEqual(self.confirmer.requests, [])
        self.nothing_written()

    def test_unknown_event_is_reported_before_asking(self):
        result = self.update(id="none1234", title="Otro")
        self.assertEqual(result.status, "error")
        self.assertIn("No encuentro ese evento", result.output)
        self.assertEqual(self.confirmer.requests, [])
        self.nothing_written()

    def test_events_that_arthur_must_not_touch(self):
        cases = {"rec12345": "se repite", "inv12345": "tiene invitados", "bd123456": "no es un evento normal",
                 "multi123": "varios días", "night123": "medianoche", "canc1234": "No encuentro ese evento"}
        for event_id, why in cases.items():
            with self.subTest(event=event_id):
                result = self.update(id=event_id, title="Otro")
                self.assertEqual(result.status, "error")
                self.assertIn(why, result.output)
        self.assertEqual(self.confirmer.requests, [])
        self.nothing_written()

    def test_past_events_are_not_edited(self):
        result = self.update(id="past1234", title="Otro")
        self.assertEqual(result.status, "error")
        self.assertIn("ya ha pasado", result.output)
        self.nothing_written()

    def test_an_event_changed_while_you_were_deciding_is_not_overwritten(self):
        google = self.google

        class Slow:
            requests = []

            def confirm(self, request):
                google.events[DENTIST]["etag"] = '"otro-cambio"'  # alguien lo toca mientras decides
                return True

        result = make_router(build_calendar_tools(self.client_api, now=lambda: NOW),
                             confirmer=Slow()).execute(ToolCall("update_event", {"id": DENTIST, "title": "Otro"}))
        self.assertEqual(result.status, "error")
        self.assertIn("ha cambiado desde que lo viste", result.output)
        self.assertEqual(google.events[DENTIST]["summary"], "Dentista")

    def test_no_invitations_and_no_other_fields(self):
        for extra in ({"attendees": ["a@example.com"]}, {"sendUpdates": "all"}, {"description": "x"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.update(title="Otro", **extra).status, "error")
        with self.assertRaisesRegex(GoogleError, "attendees"):
            self.client_api.patch_event(DENTIST, {"summary": "x", "attendees": []}, '"v"')
        self.nothing_written()

    def test_missing_permission_says_to_reconnect(self):
        self.google.token_scope = READ_SCOPES
        self.confirmer.answers = [True]
        result = self.update(title="Otro")
        self.assertEqual(result.status, "error")
        self.assertIn("arthur --conectar-google", result.output)
        self.nothing_written()

    def test_google_refusing_for_lack_of_permission(self):
        self.google.token_scope = None
        self.google.write_reply = (403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}})
        self.confirmer.answers = [True]
        result = self.update(title="Otro")
        self.assertIn("arthur --conectar-google", result.output)
        self.assertEqual(len(self.google.event_writes), 1)  # un solo intento

    def test_a_timeout_while_writing_is_never_retried(self):
        real = urllib.request.build_opener()
        attempts = []

        class DropsTheAnswer:
            def open(self, request, timeout):
                if request.get_method() != "PATCH":
                    return real.open(request, timeout=timeout)
                attempts.append(request.full_url)
                raise TimeoutError

        self.client_api = CalendarClient(self.auth, api_url=self.google.url, opener=DropsTheAnswer())
        self.confirmer.answers = [True]
        result = self.update(title="Otro")
        self.assertEqual(result.status, "error")
        self.assertIn("no sé si se ha guardado", result.output)  # avisa de que quizá se aplicó
        self.assertEqual(len(attempts), 1)  # un solo intento: repetirlo podría aplicarlo dos veces

    def test_safe_mode_denies_it(self):
        self.confirmer.answers = [True]
        self.assertEqual(self.update(self.make_router(safe_mode=True), title="Otro").status, "denied")
        self.assertEqual(self.google.event_writes, [])


class DeleteEventTest(EditEventsTestBase):
    def delete(self, router=None, **args):
        args.setdefault("id", DENTIST)
        return (router or self.make_router()).execute(ToolCall("delete_event", args))

    def test_confirmation_shows_title_date_and_weekday_then_deletes_exactly_that(self):
        self.confirmer.answers = [True]
        result = self.delete()
        self.assertEqual(result.status, "ok")
        request = self.confirmer.requests[0]
        self.assertEqual(request.risk, Risk.DANGEROUS)
        self.assertEqual(request.summary, "BORRAR de tu calendario: Dentista, jueves 8/10 de 17:00 a 18:00 "
                                          "(lugar: Clínica Sol). ARTHUR no puede deshacerlo")
        ((method, path, query, headers, body),) = self.google.event_writes
        self.assertEqual((method, path, body), ("DELETE", f"/calendar/v3/calendars/primary/events/{DENTIST}", None))
        self.assertEqual(query, {"sendUpdates": ["none"]})
        self.assertEqual(headers["If-Match"], f'"v-{DENTIST}"')
        self.assertNotIn(DENTIST, self.google.events)
        self.assertIn(EXAM, self.google.events)  # nada más desaparece
        self.assertTrue(result.output.startswith("He borrado de tu calendario: Dentista"))

    def test_all_day_summary(self):
        self.confirmer.answers = [True]
        self.delete(id=EXAM)
        self.assertEqual(self.confirmer.requests[0].summary,
                         "BORRAR de tu calendario: Examen, martes 20/10 (todo el día). ARTHUR no puede deshacerlo")

    def test_no_means_nothing_is_deleted(self):
        result = self.delete()
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.google.event_writes, [])
        self.assertIn(DENTIST, self.google.events)

    def test_it_always_asks_even_with_writes_unconfirmed_in_the_config(self):
        router = self.make_router(confirm_writes=False)
        self.assertEqual(self.delete(router).status, "cancelled")
        self.assertEqual(len(self.confirmer.requests), 1)  # un WRITE no habría preguntado; esto sí
        self.assertEqual(self.google.event_writes, [])

    def test_unknown_invalid_or_untouchable_events_never_get_to_ask(self):
        for event_id in ("none1234", "../x", "ABC", "rec12345", "inv12345", "bd123456", "multi123", "canc1234"):
            with self.subTest(event=event_id):
                self.assertEqual(self.delete(id=event_id).status, "error")
        self.assertEqual(self.confirmer.requests, [])
        self.assertEqual(self.google.event_writes, [])
        self.assertEqual(len(self.google.events), 9)

    def test_a_timeout_while_deleting_is_never_retried(self):
        real = urllib.request.build_opener()
        attempts = []

        class DropsTheAnswer:
            def open(self, request, timeout):
                if request.get_method() != "DELETE":
                    return real.open(request, timeout=timeout)
                attempts.append(request.full_url)
                raise TimeoutError

        self.client_api = CalendarClient(self.auth, api_url=self.google.url, opener=DropsTheAnswer())
        self.confirmer.answers = [True]
        result = self.delete()
        self.assertEqual(result.status, "error")
        self.assertIn("no sé si se ha guardado", result.output)
        self.assertEqual(len(attempts), 1)  # un solo intento: una escritura nunca se reintenta sola
        self.assertIn(DENTIST, self.google.events)

    def test_a_past_event_can_be_deleted(self):
        self.confirmer.answers = [True]
        self.assertEqual(self.delete(id="past1234").status, "ok")
        self.assertNotIn("past1234", self.google.events)

    def test_an_event_changed_while_you_were_deciding_is_not_deleted(self):
        google = self.google

        class Slow:
            requests = []

            def confirm(self, request):
                google.events[DENTIST]["etag"] = '"otro-cambio"'
                return True

        result = make_router(build_calendar_tools(self.client_api, now=lambda: NOW),
                             confirmer=Slow()).execute(ToolCall("delete_event", {"id": DENTIST}))
        self.assertEqual(result.status, "error")
        self.assertIn(DENTIST, google.events)

    def test_missing_permission_says_to_reconnect(self):
        self.google.token_scope = READ_SCOPES
        self.confirmer.answers = [True]
        result = self.delete()
        self.assertIn("arthur --conectar-google", result.output)
        self.assertEqual(self.google.event_writes, [])

    def test_the_audit_log_records_what_was_deleted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "audit.jsonl"
        self.confirmer.answers = [True]
        self.delete(self.make_router(audit=AuditLog(path)))
        (line,) = path.read_text().splitlines()
        entry = json.loads(line)
        self.assertEqual((entry["tool"], entry["risk"], entry["decision"], entry["status"]),
                         ("delete_event", "dangerous", "confirmed", "ok"))
        self.assertEqual(entry["arguments"], {"id": DENTIST})
        self.assertIn("BORRAR de tu calendario: Dentista, jueves 8/10", entry["summary"])  # qué se borró
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_audit_log_also_records_a_refusal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "audit.jsonl"
        self.delete(self.make_router(audit=AuditLog(path)))  # NO
        entry = json.loads(path.read_text())
        self.assertEqual((entry["decision"], entry["status"]), ("rejected", "cancelled"))
        self.assertIn("Dentista", entry["summary"])


class DefaultAnswerIsNoTest(unittest.TestCase):
    def test_terminal_asks_with_no_as_default(self):
        from arthur.interfaces.cli import CliConfirmer
        from arthur.security.confirm import ConfirmationRequest
        request = ConfirmationRequest("delete_event", Risk.DANGEROUS, "BORRAR de tu calendario: x")
        for typed, expected in (("", False), ("n", False), ("quizá", False), ("s", True)):
            with self.subTest(typed=typed):
                self.assertEqual(CliConfirmer(lambda prompt, t=typed: t, lambda text: None).confirm(request), expected)
        self.assertEqual(CliConfirmer(lambda prompt: (_ for _ in ()).throw(EOFError()), lambda t: None).confirm(request),
                         False)


if __name__ == "__main__":
    unittest.main()
