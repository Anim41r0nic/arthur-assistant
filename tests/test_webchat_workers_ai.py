"""Motor de Cloudflare Workers AI del chat de la web (webchat/workers_ai.py). Sin red: un opener falso."""

import io
import json
import logging
import unittest
import urllib.error

from arthur.notes.engines import EngineError, EngineRefused
from arthur.webchat import workers_ai
from arthur.webchat.workers_ai import DEFAULT_MODEL, WorkersAIEngine

CUENTA = "0123456789abcdef0123456789abcdef"
TOKEN = "TokEnSecReto_" + "x" * 27   # 40 caracteres, como los de Cloudflare


class Opener:
    """Devuelve (o lanza) lo que se le dé, en orden; apunta las peticiones y el plazo."""

    def __init__(self, *results):
        self.results, self.requests, self.timeouts = list(results), [], []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return io.BytesIO(json.dumps(result).encode() if not isinstance(result, bytes) else result)


def ok_openai(text):
    return {"success": True, "errors": [], "result": {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3}}}


def ok_legacy(text):
    return {"success": True, "errors": [], "result": {"response": text, "usage": {}}}


def http_error(status, code=None):
    body = {"success": False, "errors": [{"code": code, "message": "x"}] if code else [], "result": None}
    return urllib.error.HTTPError("https://api.cloudflare.com/…", status, "error", {},
                                  io.BytesIO(json.dumps(body).encode()))


class WorkersAIEngineTest(unittest.TestCase):
    def setUp(self):
        self.dia = "2026-10-17"

    def motor(self, opener, model=None):
        return WorkersAIEngine(CUENTA, TOKEN, model, opener=opener, day=lambda: self.dia)

    def test_request_shape(self):
        o = Opener(ok_openai("Hola"))
        self.assertEqual(self.motor(o).ask("pregunta", max_tokens=400, timeout=20), "Hola")
        req = o.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, f"https://api.cloudflare.com/client/v4/accounts/{CUENTA}/ai/run/{DEFAULT_MODEL}")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data), {"messages": [{"role": "user", "content": "pregunta"}],
                                                "max_completion_tokens": 400,
                                                "chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(o.timeouts, [20])

    def test_default_model_is_on_the_free_plan(self):
        self.assertEqual(DEFAULT_MODEL, "@cf/google/gemma-4-26b-a4b-it")

    def test_legacy_response_format_also_works(self):
        self.assertEqual(self.motor(Opener(ok_legacy("  Hola  "))).ask("p"), "Hola")

    def test_other_model_goes_in_the_url(self):
        o = Opener(ok_legacy("ok"))
        self.motor(o, "@cf/meta/llama-3.3-70b-instruct-fp8-fast").ask("p")
        self.assertTrue(o.requests[0].full_url.endswith("/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast"))

    def test_empty_or_strange_answers_are_errors(self):
        for raro in ({"result": {"response": ""}}, {"result": {"choices": []}}, {"result": None}, [],
                     {"result": {"choices": [{"message": {"content": None}}]}}, b"no es json"):
            with self.assertRaises(EngineError):
                self.motor(Opener(raro)).ask("p")

    def test_429_is_passing_error(self):
        o = Opener(http_error(429, 3040), ok_openai("otra vez"))
        m = self.motor(o)
        with self.assertRaises(EngineError) as cm:
            m.ask("p")
        self.assertNotIsInstance(cm.exception, EngineRefused)
        self.assertEqual(m.ask("p"), "otra vez")   # «sin capacidad» es pasajero: se vuelve a probar

    def test_daily_allocation_used_stops_calling_until_next_utc_day(self):
        o = Opener(http_error(429, 3036), ok_openai("nuevo día"))
        m = self.motor(o)
        with self.assertLogs("arthur.webchat.workers_ai", "INFO"), self.assertRaises(EngineError):
            m.ask("p")
        with self.assertRaises(EngineError):
            m.ask("p")
        self.assertEqual(len(o.requests), 1)   # el segundo ni llama: hoy ya no hay capa gratis
        self.dia = "2026-10-18"
        self.assertEqual(m.ask("p"), "nuevo día")

    def test_auth_and_server_errors(self):
        for status, tipo in ((401, EngineRefused), (403, EngineRefused), (400, EngineRefused), (500, EngineError),
                             (503, EngineError)):
            with self.assertRaises(tipo):
                self.motor(Opener(http_error(status, 10000))).ask("p")

    def test_network_errors(self):
        for fallo in (urllib.error.URLError("caído"), TimeoutError()):
            with self.assertRaises(EngineError):
                self.motor(Opener(fallo)).ask("p")

    def test_token_never_in_errors_logs_or_repr(self):
        fallos = (http_error(401, 10000), http_error(429, 3036), http_error(500), urllib.error.URLError(TOKEN),
                  b"basura")
        textos = [repr(self.motor(Opener())), str(self.motor(Opener()).__dict__.get("url"))]
        with self.assertLogs("arthur.webchat.workers_ai", logging.DEBUG) as cm:
            logging.getLogger("arthur.webchat.workers_ai").debug("marca")
            for fallo in fallos:
                try:
                    self.motor(Opener(fallo)).ask("p")
                except EngineError as e:
                    textos.append(str(e))
        for texto in textos + cm.output:
            self.assertNotIn(TOKEN, texto)
            self.assertNotIn(TOKEN[:12], texto)

    def test_bad_config_is_refused_without_echoing_the_token(self):
        malos = [(None, TOKEN, None), ("cuenta/../otra", TOKEN, None), ("z" * 32, TOKEN, None),
                 (CUENTA, None, None), (CUENTA, "corto", None), (CUENTA, TOKEN + " con espacio", None),
                 (CUENTA, TOKEN, "llama"), (CUENTA, TOKEN, "@cf/../../user/tokens"), (CUENTA, TOKEN, "@cf/x/y?z=1")]
        for cuenta, token, modelo in malos:
            with self.assertRaises(EngineRefused) as cm:
                WorkersAIEngine(cuenta, token, modelo)
            self.assertNotIn(TOKEN, str(cm.exception))

    def test_utc_day(self):
        from datetime import datetime, timedelta, timezone
        madrid = timezone(timedelta(hours=2))
        # 01:30 en Madrid del 17 = 23:30 UTC del 16: para Cloudflare aún es el 16.
        self.assertEqual(workers_ai.utc_day(datetime(2026, 10, 17, 1, 30, tzinfo=madrid)).isoformat(), "2026-10-16")
        self.assertEqual(workers_ai.utc_day(datetime(2026, 10, 17, 2, 30, tzinfo=madrid)).isoformat(), "2026-10-17")


if __name__ == "__main__":
    unittest.main()
