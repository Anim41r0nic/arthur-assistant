"""Creator Studio, paso 6: cliente de Gemini (capa gratis). Gemini FALSO: nada sale a internet."""

import io
import json
import unittest
import urllib.error
from datetime import datetime
from unittest import mock

from arthur.studio import gemini
from arthur.studio.store import StudioStore

KEY = "AIza" + "g" * 35


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeGemini:
    def __init__(self, models=("gemini-3.8-flash", "gemini-3.5-pro", "gemini-3.5-flash-lite")):
        self.models = models
        self.requests = []
        self.fail = None
        self.reply = "Respuesta."

    def open(self, request, timeout=None):
        self.requests.append(request)
        assert request.get_header("X-goog-api-key") == KEY and KEY not in request.full_url
        if self.fail:
            raise urllib.error.HTTPError(request.full_url, self.fail[0], "x", {}, io.BytesIO(self.fail[1].encode()))
        if request.full_url.endswith("pageSize=200"):
            return Response(json.dumps({"models": [{"name": f"models/{m}", "supportedGenerationMethods":
                                                    ["generateContent"]} for m in self.models]}).encode())
        body = json.loads(request.data)
        self.last_body = body
        return Response(json.dumps({"candidates": [{"content": {"parts": [{"text": self.reply}]}}]}).encode())


class KeyTest(unittest.TestCase):
    def test_new_and_old_key_formats(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gemini-key"
            for key in (KEY, "AQ.Ab8RN6" + "x" * 50, "gk_" + "y" * 40):
                gemini.save_gemini_key(f" {key}\n", path)
                self.assertEqual(path.read_text(), key)
            for bad in ("corta", "con espacio dentro de la clave larga", "a" * 30 + "\nb"):
                with self.assertRaises(ValueError):
                    gemini.save_gemini_key(bad, path)


class GeminiTest(unittest.TestCase):
    def setUp(self):
        self.store = StudioStore.in_memory()
        self.fake = FakeGemini()
        self.client = gemini.GeminiClient(KEY, self.store, opener=self.fake, now=lambda: datetime(2026, 10, 13, 12))

    def tearDown(self):
        self.store.close()

    def test_picks_a_free_flash_model_never_pro(self):
        self.assertEqual(self.client.model, "gemini-3.8-flash")
        only_new = gemini.GeminiClient(KEY, self.store, opener=FakeGemini(("gemini-9-pro", "gemini-9-flash")))
        self.assertEqual(only_new.model, "gemini-9-flash")
        with self.assertRaises(gemini.GeminiRefused):
            gemini.GeminiClient(KEY, self.store, opener=FakeGemini(("gemini-9-pro",))).model

    def test_generate_counts_and_stops_before_the_free_limit(self):
        self.client.daily_limit = 2
        self.assertEqual(self.client.generate("hola", system="sé breve"), "Respuesta.")
        self.assertEqual(self.fake.last_body["systemInstruction"]["parts"][0]["text"], "sé breve")
        self.client.generate("otra")
        # 3.8-flash ha hecho lo suyo: se sigue con el siguiente gratuito (nunca el «pro»), y cuando ese también…
        self.client.generate("una más")
        self.assertEqual(self.client.model, "gemini-3.5-flash-lite")
        self.client.generate("y otra")
        with self.assertRaises(gemini.GeminiRefused):
            self.client.generate("ya no")
        self.assertEqual(self.client.used_today("gemini-3.8-flash"), 2)
        self.assertEqual(self.client.used_today("gemini-3.5-pro"), 0)

    def test_json_and_errors(self):
        self.fake.reply = '```json\n{"titulo": "x"}\n```'
        self.assertEqual(self.client.generate_json("dame json"), {"titulo": "x"})
        self.assertEqual(self.fake.last_body["generationConfig"]["responseMimeType"], "application/json")
        self.fake.reply = 'Aquí tienes el guion:\n{"titulo": "x"}\nEspero que sirva.'   # los lite a veces hablan de más
        self.assertEqual(self.client.generate_json("dame json"), {"titulo": "x"})
        self.fake.reply = '{"titulo": "x", "escenas": [{"texto": "cortado a la mit'
        with self.assertRaises(gemini.GeminiError) as caught:
            self.client.generate_json("dame json")
        self.assertIn("caracteres", str(caught.exception))   # dice cuánto llegó, para saber si vino cortado
        self.fake.fail = (429, "RESOURCE_EXHAUSTED")
        with self.assertRaises(gemini.GeminiError) as caught:
            self.client.generate("x")
        self.assertNotIsInstance(caught.exception, gemini.GeminiRefused)  # pasajero
        self.fake.fail = (400, "API key not valid")
        with self.assertRaises(gemini.GeminiRefused):
            self.client.generate("x")


    def test_retired_model_falls_back_too(self):
        fake = FakeGemini(("gemini-2.5-flash", "gemini-3.5-flash-lite"))
        original = fake.open

        def open_(request, timeout=None):
            if "models/gemini-2.5-flash:" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 404, "x", {}, io.BytesIO(b"no longer available"))
            return original(request, timeout)

        with mock.patch.object(fake, "open", side_effect=open_):
            client = gemini.GeminiClient(KEY, self.store, opener=fake)
            self.assertEqual(client.generate("hola"), "Respuesta.")
            self.assertEqual(client.model, "gemini-3.5-flash-lite")

    def test_busy_model_falls_back_to_the_next_free_one(self):
        fake = FakeGemini(("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-pro"))
        busy = {"gemini-3.8-flash"}
        original = fake.open

        def open_(request, timeout=None):
            if any(f"models/{m}:" in request.full_url for m in busy):
                raise urllib.error.HTTPError(request.full_url, 503, "x", {}, io.BytesIO(b"overloaded"))
            return original(request, timeout)

        fake_open = mock.patch.object(fake, "open", side_effect=open_)
        with fake_open:
            client = gemini.GeminiClient(KEY, self.store, opener=fake)
            self.assertEqual(client.generate("hola"), "Respuesta.")
            self.assertEqual(client.model, "gemini-3.7-flash")   # sigue con el que funciona; nunca el «pro»
            self.assertEqual(client.used_today("gemini-3.8-flash"), 1)   # la que falló también cuenta
            self.assertEqual(client.used_today(), 1)
            busy.add("gemini-3.7-flash")
            with self.assertRaises(gemini.GeminiBusy):
                client.generate("otra")

    def test_when_every_model_failed_the_next_retry_starts_over(self):
        # 5.ª tanda (2026-10-13): todos saturados un momento y el cliente (uno por trabajo) se quedaba sin ninguno
        # para siempre, apuntando a uno retirado; los reintentos fallaban al instante sin preguntar a nadie.
        fake = FakeGemini(("gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash-lite"))
        busy = {"gemini-3.8-flash", "gemini-3.7-flash"}
        original = fake.open

        def open_(request, timeout=None):
            if "models/gemini-2.5-flash-lite:" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 404, "x", {}, io.BytesIO(b"no longer available"))
            if any(f"models/{m}:" in request.full_url for m in busy):
                raise urllib.error.HTTPError(request.full_url, 503, "x", {}, io.BytesIO(b"overloaded"))
            return original(request, timeout)

        with mock.patch.object(fake, "open", side_effect=open_):
            client = gemini.GeminiClient(KEY, self.store, opener=fake)
            with self.assertRaises(gemini.GeminiBusy):
                client.generate("hola")
            busy.clear()   # dos minutos después ya no están saturados
            self.assertEqual(client.generate("otra vez"), "Respuesta.")
            self.assertEqual(client.model, "gemini-3.8-flash")   # vuelve al mejor
            busy.add("gemini-3.8-flash")
            fake.requests.clear()
            self.assertEqual(client.generate("y otra"), "Respuesta.")
            self.assertEqual(client.model, "gemini-3.7-flash")
            # el retirado no se vuelve a probar nunca
            busy.add("gemini-3.7-flash")
            for _ in range(2):
                fake.requests.clear()
                with self.assertRaises(gemini.GeminiBusy):
                    client.generate("más")
                self.assertFalse(any("2.5-flash-lite" in r.full_url for r in fake.requests))


if __name__ == "__main__":
    unittest.main()
