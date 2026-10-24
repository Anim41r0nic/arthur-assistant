"""Studio y Apuntes desde el chat (tools/services.py), contra un servidor falso que hace de los dos."""

import base64
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from arthur.core.config import config_from_dict
from arthur.tools import build_registry
from arthur.tools.base import Risk, ToolArgumentError, ToolError
from arthur.tools.services import ServiceLink, build_apuntes_tools, build_panel_tools, build_studio_tools, find_folder, plain

from .test_core import EchoBrain, make_assistant

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
FOLDERS = [{"id": 0, "path": "Apuntes (principal)"}, {"id": 1, "path": "ALGEBRA"},
           {"id": 2, "path": "ALGEBRA / Apunts de teoria"}, {"id": 3, "path": "CALCULO"},
           {"id": 4, "path": "CALCULO / COMPLEJOS"}, {"id": 5, "path": "CALCULO / FUNCIONES 1 VARIABLE"},
           {"id": 6, "path": "MTP 1 / TEMAS"}, {"id": 7, "path": "ETC 1 / TEMAS"}]


class FakeService:
    """Contesta lo que se le diga por (método, ruta) y apunta lo que le llega (con la llave)."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, method):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                fake.calls.append((method, self.path, self.headers.get("Authorization"),
                                   self.headers.get("Content-Type"), body))
                status, payload = fake.routes.get((method, self.path), (404, {"error": "no existe"}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._answer("GET")

            def do_POST(self):
                self._answer("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self):
        return [(path, body) for method, path, _, _, body in self.calls if method == "POST"]


class BrokenOpener:
    """Un opener que falla como falla la red de verdad: al abrir, o a mitad de leer la respuesta."""

    def __init__(self, on_open=None, on_read=None):
        self.on_open, self.on_read = on_open, on_read

    def open(self, req, timeout=None):
        if self.on_open is not None:
            raise self.on_open
        on_read = self.on_read

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise on_read

        return Response()


class ServiceLinkNetworkErrorsTest(unittest.TestCase):
    def test_cut_connections_are_no_contesta(self):
        # Una respuesta cortada o una conexión reseteada no puede subir tal cual: tumbaría /api/today del panel
        for opener in (BrokenOpener(on_read=http.client.IncompleteRead(b"{", 10)),
                       BrokenOpener(on_read=ConnectionResetError(104, "Connection reset by peer")),
                       BrokenOpener(on_open=ConnectionResetError(104, "Connection reset by peer")),
                       BrokenOpener(on_open=http.client.RemoteDisconnected("cerrada")),
                       BrokenOpener(on_open=http.client.BadStatusLine("basura"))):
            link = ServiceLink("Cerebro", "http://cerebro:8778", "llave-secreta", opener=opener)
            with self.assertRaises(ToolError) as caught:
                link.call("/api/repaso/resumen")
            self.assertIn("no contesta", str(caught.exception))
            self.assertNotIn("llave-secreta", str(caught.exception))


class FindFolderTest(unittest.TestCase):
    def test_names_as_marc_says_them(self):
        self.assertEqual(find_folder(FOLDERS, "")["id"], 0)
        self.assertEqual(find_folder(FOLDERS, "Cálculo")["id"], 3)
        self.assertEqual(find_folder(FOLDERS, "cálculo > complejos")["id"], 4)
        self.assertEqual(find_folder(FOLDERS, "Complejos")["id"], 4)
        self.assertEqual(find_folder(FOLDERS, "funciones")["id"], 5)
        self.assertEqual(find_folder(FOLDERS, "álgebra / apunts")["id"], 2)

    def test_ambiguous_or_missing_asks_instead_of_guessing(self):
        with self.assertRaises(ToolError) as e:
            find_folder(FOLDERS, "temas")
        self.assertIn("MTP 1 / TEMAS", str(e.exception))
        with self.assertRaises(ToolError) as e:
            find_folder(FOLDERS, "Física")
        self.assertIn("ALGEBRA, CALCULO", str(e.exception))

    def test_plain(self):
        self.assertEqual(plain("  Cálculo  ›  Tema 1 "), "calculo / tema 1")


class ToolCase(unittest.TestCase):
    def run_tool(self, name, **args):
        """Como el router: validar, comprobar (antes de preguntar) y ejecutar."""
        tool = self.tools[name]
        args = tool.validate(args)
        if tool.check:
            tool.check(args)
        return tool.run(args)


class ApuntesToolsTest(ToolCase):
    def setUp(self):
        self.fake = FakeService()
        self.fake.routes[("GET", "/api/folders")] = (200, {"folders": FOLDERS})
        self.link = ServiceLink("Apuntes", self.fake.url, "arthur_llave", "http://100.64.1.2:8774")
        self.image = None
        self.tools = {t.name: t for t in build_apuntes_tools(self.link, lambda: self.image)}

    def tearDown(self):
        self.fake.close()

    def test_risks(self):
        self.assertEqual({n: t.risk for n, t in self.tools.items()},
                         {"apuntes_ver": Risk.READ, "apuntes_crear_carpeta": Risk.WRITE,
                          "apuntes_guardar_foto": Risk.WRITE})

    def test_list_a_folder_with_the_key(self):
        self.fake.routes[("GET", "/api/notes?carpeta=3")] = (200, {
            "folders": [{"name": "COMPLEJOS", "notes": 2, "folders": 0}],
            "notes": [{"title": "Taylor", "status_label": "Listo"}]})
        out = self.run_tool("apuntes_ver", carpeta="calculo")
        self.assertIn("📁 COMPLEJOS (2 apuntes", out)
        self.assertIn("📄 Taylor — Listo", out)
        self.assertEqual(self.fake.calls[0][2], "Bearer arthur_llave")

    def test_create_folder(self):
        self.fake.routes[("POST", "/api/folders")] = (200, {"id": 9, "name": "Tema 4"})
        out = self.run_tool("apuntes_crear_carpeta", nombre="Tema 4", dentro_de="Cálculo")
        self.assertEqual(out, "He creado la carpeta «Tema 4» dentro de CALCULO.")
        self.assertEqual(json.loads(self.fake.posts()[0][1]), {"parent": 3, "name": "Tema 4"})
        summary = self.tools["apuntes_crear_carpeta"].summary({"nombre": "Tema 4", "dentro_de": "Cálculo"})
        self.assertIn("dentro de Cálculo", summary)

    def test_save_photo_needs_a_photo_before_asking(self):
        with self.assertRaises(ToolError) as e:
            self.run_tool("apuntes_guardar_foto", carpeta="Complejos")
        self.assertIn("No me has mandado ninguna foto", str(e.exception))
        self.image = base64.b64encode(b"RIFF....WEBPVP8 ").decode()
        with self.assertRaises(ToolError):
            self.run_tool("apuntes_guardar_foto", carpeta="Complejos")
        self.image = "no es base64 !!"
        with self.assertRaises(ToolError):
            self.run_tool("apuntes_guardar_foto", carpeta="Complejos")
        self.image = base64.b64encode(JPEG).decode()
        with self.assertRaises(ToolError):  # carpeta que no está clara: tampoco se pregunta
            self.run_tool("apuntes_guardar_foto", carpeta="temas")
        self.assertEqual(self.fake.posts(), [])  # nada creado

    def test_save_photo_creates_uploads_and_reads(self):
        self.image = base64.b64encode(JPEG).decode()
        self.fake.routes[("POST", "/api/notes")] = (200, {"id": 42, "title": "Integrales"})
        self.fake.routes[("POST", "/api/notes/42/upload")] = (200, {"id": 42})
        self.fake.routes[("POST", "/api/notes/42/start")] = (200, {"id": 42})
        out = self.run_tool("apuntes_guardar_foto", carpeta="cálculo / complejos", titulo="Integrales")
        self.assertIn("Guardada como «Integrales» en CALCULO / COMPLEJOS", out)
        self.assertIn("http://100.64.1.2:8774/#n=42", out)
        posts = self.fake.posts()
        self.assertEqual([p for p, _ in posts], ["/api/notes", "/api/notes/42/upload", "/api/notes/42/start"])
        self.assertEqual(json.loads(posts[0][1]), {"title": "Integrales", "engine": "auto", "lang": "es", "folder": 4})
        self.assertEqual(posts[1][1], JPEG)
        upload = next(c for c in self.fake.calls if c[1].endswith("/upload"))
        self.assertEqual(upload[3], "image/jpeg")

    def test_save_photo_without_reading(self):
        self.image = base64.b64encode(PNG).decode()
        self.fake.routes[("POST", "/api/notes")] = (200, {"id": 5, "title": "Foto del chat"})
        self.fake.routes[("POST", "/api/notes/5/upload")] = (200, {})
        self.run_tool("apuntes_guardar_foto", carpeta="Álgebra", pasar_a_texto=False)
        self.assertEqual([p for p, _ in self.fake.posts()], ["/api/notes", "/api/notes/5/upload"])
        upload = next(c for c in self.fake.calls if c[1].endswith("/upload"))
        self.assertEqual(upload[3], "image/png")

    def test_service_errors_are_explained(self):
        self.fake.routes[("POST", "/api/folders")] = (409, {"error": "Ya hay una carpeta «Tema 4» aquí"})
        with self.assertRaises(ToolError) as e:
            self.run_tool("apuntes_crear_carpeta", nombre="Tema 4", dentro_de="Cálculo")
        self.assertEqual(str(e.exception), "Apuntes: Ya hay una carpeta «Tema 4» aquí")
        down = ServiceLink("Apuntes", "http://127.0.0.1:9", "x")
        with self.assertRaises(ToolError) as e:
            down.call("/api/folders")
        self.assertIn("no contesta", str(e.exception))


class StudioToolsTest(ToolCase):
    def setUp(self):
        self.fake = FakeService()
        self.fake.routes[("GET", "/api/reviews")] = (200, {"reviews": [
            {"id": 7, "title": "Séneca frente a la muerte", "status": "pending", "kind": "largo", "language": "es"},
            {"id": 8, "title": "Marco Aurelio y la ira", "status": "pending", "kind": "largo", "language": "es"},
            {"id": 3, "title": "Ya publicado", "status": "uploaded", "kind": "largo", "language": "es"}]})
        self.fake.routes[("GET", "/api/pilot")] = (200, {"job": None})
        self.link = ServiceLink("Studio", self.fake.url, "arthur_llave", "http://100.64.1.2:8773")
        self.tools = {t.name: t for t in build_studio_tools(self.link)}

    def tearDown(self):
        self.fake.close()

    def test_risks(self):
        self.assertEqual({n: t.risk for n, t in self.tools.items()},
                         {"studio_estado": Risk.READ, "studio_preparar_video": Risk.WRITE,
                          "studio_publicar": Risk.DANGEROUS})

    def test_status(self):
        self.fake.routes[("GET", "/api/pilot")] = (200, {"job": {
            "id": 12, "status": "running", "tema": "Séneca", "error": None, "steps_done": ["idea", "guion"]}})
        out = self.run_tool("studio_estado")
        self.assertIn("trabajo #12, tema: Séneca). Hecho: idea, guion.", out)
        self.assertIn("#7 «Séneca frente a la muerte»", out)
        self.assertNotIn("Ya publicado", out)

    def test_prepare_now(self):
        self.fake.routes[("POST", "/api/pilot")] = (200, {"job": {"id": 13, "tema": "la ira"}})
        out = self.run_tool("studio_preparar_video", tema="la ira")
        self.assertIn("preparando un vídeo sobre «la ira» (trabajo #13)", out)
        self.assertEqual(json.loads(self.fake.posts()[0][1]), {"tema": "la ira"})
        self.fake.routes[("POST", "/api/pilot")] = (409, {"error": "ya se está haciendo un vídeo (trabajo #13)"})
        with self.assertRaises(ToolError) as e:
            self.run_tool("studio_preparar_video")
        self.assertIn("ya se está haciendo", str(e.exception))

    def test_publish_only_without_doubts(self):
        self.fake.routes[("GET", "/api/reviews/7")] = (200, {"video_sha256": "abc", "warnings": ["Paulina sobrevivió"]})
        with self.assertRaises(ToolError) as e:
            self.run_tool("studio_publicar", video="séneca")
        self.assertIn("1 dudas del revisor: Paulina sobrevivió", str(e.exception))
        self.assertIn("PUBLICAR", str(e.exception))
        self.assertEqual(self.fake.posts(), [])  # nada aprobado

        self.fake.routes[("GET", "/api/reviews/8")] = (200, {"video_sha256": "def", "warnings": []})
        self.fake.routes[("POST", "/api/reviews/8/approve")] = (200, {"id": 8, "status": "approved"})
        out = self.run_tool("studio_publicar", video="#8")
        self.assertIn("se está subiendo a YouTube", out)
        self.assertEqual(self.fake.posts(), [("/api/reviews/8/approve", b'{"video_sha256": "def"}')])
        self.assertIn("#8 «Marco Aurelio y la ira»", self.tools["studio_publicar"].summary({"video": "8"}))

    def test_publish_needs_a_clear_video(self):
        with self.assertRaises(ToolError) as e:
            self.run_tool("studio_publicar", video="e")  # «e» está en los dos títulos
        self.assertIn("Hay varios", str(e.exception))
        with self.assertRaises(ToolError) as e:
            self.run_tool("studio_publicar", video="#3")  # ya publicado: no está pendiente
        self.assertIn("No encuentro", str(e.exception))


class PanelToolsTest(ToolCase):
    def setUp(self):
        self.fake = FakeService()
        self.link = ServiceLink("Panel", self.fake.url, "arthur_llave", "http://100.64.1.2:8775")
        self.tools = {t.name: t for t in build_panel_tools(self.link)}

    def tearDown(self):
        self.fake.close()

    def test_risks(self):
        self.assertEqual({n: t.risk for n, t in self.tools.items()},
                         {"tareas_de_hoy": Risk.LOW, "anadir_tarea": Risk.LOW})

    def test_today_shows_progress_and_pending(self):
        tasks = [{"title": f"hecha {i}", "done": True} for i in range(3)]
        tasks += [{"title": "comprar pan", "done": False}, {"title": "llamar al dentista", "done": False}]
        tasks += [{"title": f"otra {i}", "done": False} for i in range(2)]
        self.fake.routes[("GET", "/api/today")] = (200, {"tasks": tasks,
                                                          "progress": {"done": 3, "total": 7, "percent": 43}})
        out = self.run_tool("tareas_de_hoy")
        self.assertTrue(out.startswith("3 de 7 hechas (43 %)"))
        self.assertIn("- comprar pan", out)
        self.assertNotIn("hecha 0", out)
        self.assertEqual(self.fake.calls[0][2], "Bearer arthur_llave")

    def test_today_without_tasks(self):
        self.fake.routes[("GET", "/api/today")] = (200, {"tasks": [], "progress": {"done": 0, "total": 0, "percent": 0}})
        self.assertEqual(self.run_tool("tareas_de_hoy"), "No tienes tareas para hoy.")

    def test_add_task_posts_the_text(self):
        self.fake.routes[("POST", "/api/tasks")] = (200, {"title": "pan", "due": "2026-10-17"})
        out = self.run_tool("anadir_tarea", texto="  pan mañana ")
        self.assertIn("«pan»", out)
        self.assertIn("2026-10-17", out)
        path, body = self.fake.posts()[0]
        self.assertEqual((path, json.loads(body)), ("/api/tasks", {"text": "pan mañana"}))

    def test_add_task_needs_text(self):
        with self.assertRaises(ToolArgumentError):
            self.run_tool("anadir_tarea")
        with self.assertRaises(ToolError):
            self.run_tool("anadir_tarea", texto="   ")
        with self.assertRaises(ToolArgumentError):
            self.run_tool("anadir_tarea", texto="x" * 201)
        self.assertEqual(self.fake.posts(), [])

    def test_panel_down_is_a_clear_error(self):
        self.fake.close()
        with self.assertRaises(ToolError) as e:
            self.run_tool("tareas_de_hoy")
        self.assertIn("Panel no contesta", str(e.exception))
        self.fake = FakeService()  # para que tearDown tenga algo que cerrar


class WiringTest(unittest.TestCase):
    def test_panel_tools_only_when_linked(self):
        cfg = config_from_dict({}).security
        link = ServiceLink("Panel", "http://127.0.0.1:9", "k")
        names = {t.name for t in build_registry(cfg, profile="core", panel=link).all()}
        self.assertTrue({"tareas_de_hoy", "anadir_tarea"} <= names)
        names = {t.name for t in build_registry(cfg, profile="core").all()}
        self.assertFalse({"tareas_de_hoy", "anadir_tarea"} & names)

    def test_tools_only_in_the_server_and_only_when_linked(self):
        cfg = config_from_dict({}).security
        link = ServiceLink("x", "http://127.0.0.1:9", "k")
        registry = build_registry(cfg, profile="core", studio=link, apuntes=link)
        names = {t.name for t in registry.all()}
        self.assertTrue({"studio_estado", "studio_preparar_video", "studio_publicar", "apuntes_ver",
                         "apuntes_crear_carpeta", "apuntes_guardar_foto"} <= names)
        self.assertIsNone(build_registry(cfg, profile="core").get("studio_publicar"))

    def test_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "llave"
            key.write_text("arthur_abc\n", encoding="utf-8")
            env = {"ARTHUR_APUNTES_URL": "http://apuntes-web:8774/", "ARTHUR_APUNTES_LLAVE_FILE": str(key),
                   "ARTHUR_APUNTES_PUBLIC_URL": "http://100.64.1.2:8774"}
            link = ServiceLink.from_env(env, "Apuntes", "ARTHUR_APUNTES", "apuntes_core")
            self.assertEqual((link.url, link.public_url, link._key),
                             ("http://apuntes-web:8774", "http://100.64.1.2:8774", "arthur_abc"))
            self.assertIsNone(ServiceLink.from_env({}, "Apuntes", "ARTHUR_APUNTES", "apuntes_core"))
            with self.assertRaises(ValueError):
                ServiceLink.from_env({"ARTHUR_STUDIO_URL": "http://x", "ARTHUR_STUDIO_LLAVE_FILE": "/no/existe"},
                                     "Studio", "ARTHUR_STUDIO", "studio_core")

    def test_latest_image_of_the_chat(self):
        assistant, _ = make_assistant(EchoBrain())
        self.assertIsNone(assistant.latest_image())
        assistant.handle_text("mira", speak=False, images=("FOTO1",))
        assistant.handle_text("guárdala en cálculo", speak=False)
        self.assertEqual(assistant.latest_image(), "FOTO1")  # la foto de un mensaje anterior también vale
        assistant.handle_text("y esta", speak=False, images=("FOTO2",))
        self.assertEqual(assistant.latest_image(), "FOTO2")


if __name__ == "__main__":
    unittest.main()
