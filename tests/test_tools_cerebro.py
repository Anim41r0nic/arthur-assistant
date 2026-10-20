"""«Pregúntale a mi cerebro» desde el chat (tools/services.py: preguntar_cerebro), contra un cerebro falso. Sin red."""

import json
import unittest

from arthur.core.config import config_from_dict
from arthur.tools import build_registry
from arthur.tools.base import Risk, ToolArgumentError
from arthur.tools.services import ServiceLink, build_cerebro_tools

from .test_services import FakeService

RESPUESTA = {"answer": "Se usa L'Hôpital cuando sale 0/0 [n1].", "engine": "workers-ai", "found": True,
             "citations": [{"id": "n1", "title": "Derivadas", "source": "apuntes", "url": "http://a:8774/#n4"},
                           {"id": "n2", "title": "Límites", "source": "obsidian:cerebro",
                            "url": "obsidian://open?vault=Cerebro&file=Uni%2FL%C3%ADmites"}]}


class CerebroToolTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeService()
        self.link = ServiceLink("Cerebro", self.fake.url, "arthur_llave")
        self.tools = {t.name: t for t in build_cerebro_tools(self.link)}

    def tearDown(self):
        self.fake.close()

    def run_tool(self, **args):
        tool = self.tools["preguntar_cerebro"]
        return tool.run(tool.validate(args))

    def test_riesgo_bajo_y_respuesta_directa(self):
        tool = self.tools["preguntar_cerebro"]
        self.assertEqual(tool.risk, Risk.LOW)
        self.assertTrue(tool.direct_reply)

    def test_tool_texto_y_fuentes(self):
        self.fake.routes[("POST", "/api/ask")] = (200, RESPUESTA)
        out = self.run_tool(pregunta="  ¿cuándo se usa L'Hôpital? ")
        self.assertTrue(out.startswith("Se usa L'Hôpital cuando sale 0/0 [n1]."))
        self.assertIn("Fuentes:", out)
        self.assertIn("[n1] Derivadas (http://a:8774/#n4)", out)
        self.assertIn("[n2] Límites", out)
        method, path, auth, ctype, body = self.fake.calls[0]
        self.assertEqual((method, path, auth), ("POST", "/api/ask", "Bearer arthur_llave"))
        self.assertEqual(json.loads(body), {"q": "¿cuándo se usa L'Hôpital?"})

    def test_x_arthur_en_los_post(self):
        # El cerebro exige X-Arthur en todo POST: ServiceLink la pone siempre.
        seen = []
        handler = self.fake.httpd.RequestHandlerClass
        original = handler.do_POST

        def spy(h):
            seen.append(h.headers.get("X-Arthur"))
            original(h)

        handler.do_POST = spy
        try:
            self.fake.routes[("POST", "/api/ask")] = (200, RESPUESTA)
            self.run_tool(pregunta="hola")
        finally:
            handler.do_POST = original
        self.assertEqual(seen, ["1"])

    def test_sin_citas(self):
        self.fake.routes[("POST", "/api/ask")] = (200, {"answer": "No lo encuentro en tus notas.", "engine": "ninguno",
                                                         "found": False, "citations": []})
        self.assertEqual(self.run_tool(pregunta="¿capital de Mongolia?"), "No lo encuentro en tus notas.")

    def test_tool_cerebro_caido(self):
        self.fake.close()
        self.assertEqual(self.run_tool(pregunta="hola"), "El cerebro no está disponible ahora. Prueba en un rato.")
        self.fake = FakeService()

    def test_cerebro_con_error(self):
        self.fake.routes[("POST", "/api/ask")] = (503, {"error": "roto"})
        self.assertEqual(self.run_tool(pregunta="hola"), "El cerebro no está disponible ahora. Prueba en un rato.")

    def test_pregunta_obligatoria(self):
        with self.assertRaises(ToolArgumentError):
            self.run_tool()
        with self.assertRaises(ToolArgumentError):
            self.run_tool(pregunta="x" * 501)
        self.assertEqual(self.fake.calls, [])


class WiringTest(unittest.TestCase):
    def test_solo_en_el_servidor_y_con_enlace(self):
        cfg = config_from_dict({}).security
        link = ServiceLink("Cerebro", "http://127.0.0.1:9", "k")
        self.assertIn("preguntar_cerebro", {t.name for t in build_registry(cfg, profile="core", cerebro=link).all()})
        self.assertNotIn("preguntar_cerebro", {t.name for t in build_registry(cfg, profile="core").all()})


if __name__ == "__main__":
    unittest.main()
