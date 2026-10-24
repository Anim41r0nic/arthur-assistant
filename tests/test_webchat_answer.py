import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from arthur.notes.engines import EngineError, LocalEngine
from arthur.webchat.answer import FIXED, Answerer
from arthur.webchat.knowledge import Knowledge
from arthur.webchat.limits import Limits, RateLimiter


class Motor:
    """Motor falso: devuelve `result` o lanza si es una excepción; apunta cómo lo llamaron."""

    def __init__(self, result="Hola"):
        self.result, self.calls = result, []

    def ask(self, prompt, **kw):
        self.calls.append(kw)
        self.prompts = getattr(self, "prompts", []) + [prompt]
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Limitador:
    def __init__(self, turno="workers-ai"):
        self.turno, self.reservas, self.liberados = turno, 0, 0

    def reserve_answer(self):
        self.reservas += 1
        return self.turno

    def release_workers_ai(self):
        self.liberados += 1


class AnswerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ruta = Path(self.tmp.name) / "conocimiento.md"
        self.ruta.write_text("Marc hace webs.", encoding="utf-8")

    def respondedor(self, nube, local, turno="workers-ai", ruta=None, reloj=None):
        self.lim = Limitador(turno)
        return Answerer(Knowledge(ruta or self.ruta), self.lim, nube, local, **({"clock": reloj} if reloj else {}))

    def test_workers_ai_answers(self):
        g, l = Motor("Claro"), Motor("local")
        r = self.respondedor(g, l).reply([], "hola", "es")
        self.assertEqual(r, {"reply": "Claro", "offer_contact": False, "engine": "workers-ai"})
        self.assertEqual(g.calls, [{"max_tokens": 400, "timeout": 20}])
        self.assertEqual((l.calls, self.lim.liberados), ([], 0))

    def test_workers_ai_error_falls_to_local_and_releases(self):
        g, l = Motor(EngineError("x")), Motor("Desde local")
        r = self.respondedor(g, l, reloj=lambda: 100.0).reply([], "hola", "es")
        self.assertEqual((r["engine"], r["reply"]), ("local", "Desde local"))
        self.assertEqual(l.calls, [{"num_predict": 160, "timeout": 62}])
        self.assertEqual(self.lim.liberados, 1)

    def test_local_gets_what_is_left_of_the_budget(self):
        # Workers AI se agota a los 20 s: a Ollama le quedan 42 s (antes, 40 fijos aunque Workers AI fallase al
        # momento). Y nunca menos de 15 s, aunque el reloj diga que ya no queda nada.
        for gastado, esperado in ((20.0, 42), (0.5, 61), (70.0, 15)):
            tiempos = iter((0.0, gastado))
            l = Motor("ok")
            self.respondedor(Motor(TimeoutError()), l, reloj=lambda: next(tiempos)).reply([], "hola", "es")
            self.assertEqual(l.calls[0]["timeout"], esperado)

    def test_local_gets_the_short_prompt(self):
        g, l = Motor(EngineError("x")), Motor("ok")
        self.respondedor(g, l).reply([{"role": "visita", "text": "antes"}], "hola", "es")
        self.assertIn("Reglas", g.prompts[0])
        self.assertIn("es un extracto", l.prompts[0])        # el corto, no el completo
        self.assertNotIn("es un extracto", g.prompts[0])
        self.assertIn("Marc hace webs.", l.prompts[0])

    def test_network_errors_also_fall_over(self):
        for fallo in (urllib.error.URLError("caído"), TimeoutError(), ConnectionResetError()):
            r = self.respondedor(Motor(fallo), Motor("ok")).reply([], "hola", "es")
            self.assertEqual(r["engine"], "local")

    def test_empty_or_only_mark_falls_to_local(self):
        for vacio in ("", "  ", "[[CONTACTO]]"):
            g = Motor(vacio)
            r = self.respondedor(g, Motor("ok")).reply([], "hola", "es")
            self.assertEqual(r["engine"], "local")
            self.assertEqual(self.lim.liberados, 1)

    def test_both_fail_gives_fixed_in_language(self):
        for lang in ("es", "en", "ca"):
            r = self.respondedor(Motor(EngineError("a")), Motor(EngineError("b"))).reply([], "hola", lang)
            self.assertEqual(r, {"reply": FIXED[lang], "offer_contact": True, "engine": "fijo"})
        self.assertIn("hola@ejemplo.com", FIXED["es"])

    def test_unknown_language_uses_default(self):
        r = self.respondedor(None, None, turno="fijo").reply([], "hola", "zz")
        self.assertEqual(r["reply"], FIXED["en"])  # norm_lang cae a inglés

    def test_local_turn_does_not_call_workers_ai(self):
        g, l = Motor("no"), Motor("local")
        r = self.respondedor(g, l, turno="local").reply([], "hola", "es")
        self.assertEqual((r["engine"], g.calls, self.lim.liberados), ("local", [], 0))

    def test_fixed_turn_calls_nobody(self):
        g, l = Motor("no"), Motor("no")
        r = self.respondedor(g, l, turno="fijo").reply([], "hola", "en")
        self.assertEqual((r["engine"], g.calls, l.calls), ("fijo", [], []))

    def test_missing_engines_are_skipped(self):
        r = self.respondedor(None, Motor("ok")).reply([], "hola", "es")
        self.assertEqual(r["engine"], "local")
        r = self.respondedor(None, None).reply([], "hola", "es")
        self.assertEqual(r["engine"], "fijo")

    def test_no_knowledge_is_fixed_and_keeps_quota(self):
        g = Motor("no")
        r = self.respondedor(g, g, ruta=Path(self.tmp.name) / "no-existe.md").reply([], "hola", "es")
        self.assertEqual(r["engine"], "fijo")
        self.assertEqual((self.lim.reservas, g.calls), (0, []))

    def test_contact_mark_offers_contact(self):
        r = self.respondedor(Motor("Escríbele [[CONTACTO]]"), None).reply([], "hola", "es")
        self.assertEqual((r["reply"], r["offer_contact"]), ("Escríbele", True))

    def test_real_limiter_gives_back_workers_ai_slot(self):
        lim = RateLimiter(Limits(workers_ai_per_day=1), lambda: datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
        a = Answerer(Knowledge(self.ruta), lim, Motor(EngineError("x")), Motor("ok"))
        a.reply([], "hola", "es")
        self.assertEqual(lim.reserve_answer(), "workers-ai")  # el cupo se devolvió

    def test_daily_cap_then_local_then_fixed(self):
        # Orden completo con el limitador de verdad: Workers AI hasta su tope, luego Ollama, y con el tope duro
        # gastado, el mensaje fijo sin llamar a nadie.
        lim = RateLimiter(Limits(workers_ai_per_day=2, hard_per_day=3),
                          lambda: datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
        nube, local = Motor("nube"), Motor("local")
        a = Answerer(Knowledge(self.ruta), lim, nube, local)
        motores = [a.reply([], "hola", "es")["engine"] for _ in range(4)]
        self.assertEqual(motores, ["workers-ai", "workers-ai", "local", "fijo"])
        self.assertEqual((len(nube.calls), len(local.calls)), (2, 1))


class LocalEngineAskTest(unittest.TestCase):
    def test_num_predict_and_timeout(self):
        class Opener:
            def open(self, request, timeout=None):
                self.request, self.timeout = request, timeout
                import io
                return io.BytesIO(json.dumps({"message": {"content": "ok"}}).encode())
        o = Opener()
        LocalEngine("http://o:1", "m", opener=o).ask("hola", num_predict=400, timeout=40)
        self.assertEqual(json.loads(o.request.data)["options"]["num_predict"], 400)
        self.assertEqual(o.timeout, 40)

    def test_num_ctx_and_keep_alive_from_env(self):
        import io
        from unittest import mock

        class Opener:
            def open(self, request, timeout=None):
                self.body = json.loads(request.data)
                return io.BytesIO(json.dumps({"message": {"content": "ok"}}).encode())
        casos = (({"ARTHUR_OLLAMA_NUM_CTX": "16384", "ARTHUR_OLLAMA_KEEP_ALIVE": "5m"}, 16384, "5m"),
                 ({"ARTHUR_OLLAMA_KEEP_ALIVE": "-1m"}, 8192, "-1m"),
                 # Valores raros: los de siempre, sin tumbar nada
                 ({"ARTHUR_OLLAMA_NUM_CTX": "mucho", "ARTHUR_OLLAMA_KEEP_ALIVE": "30 min; rm"}, 8192, "30m"),
                 ({"ARTHUR_OLLAMA_NUM_CTX": "512"}, 8192, "30m"))
        for env, ctx, keep in casos:
            with mock.patch.dict("os.environ", env, clear=False):
                o = Opener()
                LocalEngine("http://o:1", "m", opener=o).ask("hola")
            self.assertEqual((o.body["options"]["num_ctx"], o.body["keep_alive"]), (ctx, keep))



class WorkersAIFallbackTest(unittest.TestCase):
    """El motor de verdad (con un opener falso, sin red) dentro del Answerer: un 429 o la capa gratis gastada
    hacen contestar a Ollama, y el cupo de Workers AI se devuelve."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ruta = Path(tmp.name) / "conocimiento.md"
        self.ruta.write_text("Marc hace webs.", encoding="utf-8")

    def motor(self, opener):
        from arthur.webchat.workers_ai import WorkersAIEngine
        return WorkersAIEngine("a" * 32, "t" * 40, opener=opener, day=lambda: "2026-10-17")

    def test_429_goes_to_local(self):
        from tests.test_webchat_workers_ai import Opener, http_error
        for codigo in (3036, 3040):
            opener = Opener(http_error(429, codigo))
            lim = Limitador()
            r = Answerer(Knowledge(self.ruta), lim, self.motor(opener), Motor("Desde Ollama")).reply([], "hola", "es")
            self.assertEqual((r["engine"], r["reply"], lim.liberados), ("local", "Desde Ollama", 1))

    def test_ok_answer(self):
        from tests.test_webchat_workers_ai import Opener, ok_openai
        r = Answerer(Knowledge(self.ruta), Limitador(), self.motor(Opener(ok_openai("Hola [[CONTACTO]]"))),
                     Motor("no")).reply([], "hola", "es")
        self.assertEqual(r, {"reply": "Hola", "offer_contact": True, "engine": "workers-ai"})


if __name__ == "__main__":
    unittest.main()
