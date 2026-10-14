"""Creator Studio, paso 3a: guion con fuentes obligatorias e imágenes de Commons con su licencia.

Wikimedia FALSO: nada sale a internet.
"""

import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from arthur.studio import assets, project
from arthur.studio.store import StudioStore

IMAGE = b"\xff\xd8\xff" + b"jpeg falso" * 100

GUION = """
titulo = "Su mejor general lo traicionó"
descripcion = "La rebelión de Avidio Casio."
etiquetas = ["estoicismo", "marco aurelio"]

[[fuentes]]
id = "dion"
obra = "Historia romana, libro LXXII"
autor = "Dion Casio"
url = "https://penelope.uchicago.edu/"

[[escenas]]
texto = "En el año 175, llegó una noticia."
imagen = "File:Marco.jpg"
fuentes = ["dion"]

[[escenas]]
texto = "Otra escena con la misma imagen."
imagen = "File:Marco.jpg"
fuentes = ["dion"]

[[escenas]]
texto = "Y esto es lo que podemos aprender."
imagen = "File:Casio.jpg"
opinion = true
"""


def meta(license_code, short, artist="<a href='//x'>Fulano</a>", attribution="false", restrictions=""):
    return {"License": {"value": license_code}, "LicenseShortName": {"value": short},
            "Artist": {"value": artist}, "AttributionRequired": {"value": attribution},
            "Restrictions": {"value": restrictions}, "LicenseUrl": {"value": ""}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeCommons:
    def __init__(self):
        self.files = {
            "File:Marco.jpg": (meta("pd", "Public domain"), "image/jpeg"),
            "File:Casio.jpg": (meta("cc-by-4.0", "CC BY 4.0", attribution="true"), "image/jpeg"),
            "File:Compartida.jpg": (meta("cc-by-sa-4.0", "CC BY-SA 4.0"), "image/jpeg"),
            "File:NoComercial.jpg": (meta("cc-by-nc-2.0", "CC BY-NC 2.0"), "image/jpeg"),
            "File:Marca.jpg": (meta("cc0", "CC0", restrictions="trademarked"), "image/jpeg"),
            "File:Dibujo.svg": (meta("pd", "Public domain"), "image/svg+xml"),
        }
        self.requests = []
        self.download_host = "upload.wikimedia.org"
        self.throttle = 0  # cuántas respuestas 429 dar antes de contestar

    def open(self, request, timeout=None):
        self.requests.append(request.full_url)
        assert request.get_header("User-agent", "").startswith("ARTHUR-Studio/")
        if self.throttle:
            self.throttle -= 1
            raise urllib.error.HTTPError(request.full_url, 429, "too many", {"Retry-After": "7"}, io.BytesIO(b""))
        url = urllib.parse.urlsplit(request.full_url)
        if url.hostname != "commons.wikimedia.org":
            return FakeResponse(IMAGE)
        params = dict(urllib.parse.parse_qsl(url.query))
        if params.get("list") == "search":
            body = {"query": {"search": [{"title": "File:Marco.jpg"}, {"title": "mal"}]}}
        else:
            title = params["titles"]
            if title not in self.files:
                body = {"query": {"pages": [{"title": title, "missing": True}]}}
            else:
                extmeta, mime = self.files[title]
                body = {"query": {"pages": [{"title": title, "imageinfo": [{
                    "thumburl": f"https://{self.download_host}/thumb/{title}", "mime": mime,
                    "descriptionurl": f"https://commons.wikimedia.org/wiki/{title}", "extmetadata": extmeta}]}]}}
        return FakeResponse(json.dumps(body).encode())


class LicenseTest(unittest.TestCase):
    def test_verdicts(self):
        ok = lambda code, short="", restr="": assets.license_verdict(meta(code, short, restrictions=restr))[0]
        self.assertTrue(ok("pd", "Public domain"))
        self.assertTrue(ok("pd-old-100"))
        self.assertTrue(ok("cc0"))
        self.assertTrue(ok("cc-by-4.0"))
        self.assertTrue(ok("", "Public domain"))
        self.assertFalse(ok("cc-by-sa-4.0"))
        self.assertFalse(ok("cc-by-nc-2.0"))
        self.assertFalse(ok("cc-by-nd-3.0"))
        self.assertFalse(ok("gfdl"))
        self.assertFalse(ok(""))
        self.assertFalse(ok("cc0", restr="trademarked"))


class CommonsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.store = StudioStore.in_memory()
        self.fake = FakeCommons()
        self.sleeps = []
        self.commons = assets.CommonsClient(self.store, opener=self.fake, sleep=self.sleeps.append)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_public_domain_is_downloaded_and_registered(self):
        asset = self.commons.fetch("File:Marco.jpg", "p", self.folder, "img-1")
        self.assertEqual((asset.status, asset.license, asset.name), ("ok", "pd", "img-1.jpg"))
        self.assertEqual((self.folder / "img-1.jpg").read_bytes(), IMAGE)
        self.assertEqual(asset.sha256, hashlib.sha256(IMAGE).hexdigest())
        self.assertEqual(asset.author, "Fulano")  # sin el HTML
        self.assertEqual(self.store.assets("p"), [asset])

    def test_bad_licences_are_blocked_and_not_downloaded(self):
        for title in ("File:Compartida.jpg", "File:NoComercial.jpg", "File:Marca.jpg", "File:Dibujo.svg"):
            asset = self.commons.fetch(title, "p", self.folder, title[5:9].lower())
            self.assertEqual(asset.status, "blocked", title)
            self.assertTrue(asset.reason)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(len([u for u in self.fake.requests if "upload" in u]), 0)

    def test_download_only_from_wikimedia(self):
        self.fake.download_host = "malo.example.com"
        asset = self.commons.fetch("File:Marco.jpg", "p", self.folder, "img-1")
        self.assertEqual(asset.status, "blocked")
        self.assertIn("servidores de Wikimedia", asset.reason)

    def test_missing_and_invalid_titles(self):
        with self.assertRaises(assets.AssetError):
            self.commons.fetch("File:NoExiste.jpg", "p", self.folder, "x")
        with self.assertRaises(assets.AssetError):
            self.commons.info("Marco.jpg")  # sin «File:»

    def test_waits_between_requests_and_after_429(self):
        clock = iter(range(100))
        commons = assets.CommonsClient(self.store, opener=self.fake, sleep=self.sleeps.append,
                                       monotonic=lambda: next(clock) * 0.25)
        commons.search("a")
        commons.search("b")
        self.assertTrue(self.sleeps and self.sleeps[-1] > 0)  # la segunda esperó
        self.sleeps.clear()
        self.fake.throttle = 1
        self.assertEqual(commons.search("c"), ["File:Marco.jpg"])
        self.assertIn(7.0, self.sleeps)  # lo que pidió Retry-After
        self.fake.throttle = 2
        with self.assertRaises(assets.AssetError):
            commons.search("d")  # solo se reintenta una vez

    def test_search_returns_only_files(self):
        self.assertEqual(self.commons.search("Marco Aurelio"), ["File:Marco.jpg"])


class ScriptTest(unittest.TestCase):
    def test_parses_and_estimates(self):
        import tomllib
        script = project.parse_script(tomllib.loads(GUION))
        self.assertEqual(len(script.escenas), 3)
        self.assertTrue(script.escenas[2].opinion)
        self.assertEqual(script.words, 20)
        self.assertEqual(script.etiquetas, ("estoicismo", "marco aurelio"))

    def test_rejects_scenes_without_sources(self):
        import tomllib
        cases = {
            "sin fuente": GUION.replace('opinion = true', ''),
            "fuente desconocida": GUION.replace('fuentes = ["dion"]', 'fuentes = ["otra"]', 1),
            "imagen sin File": GUION.replace('"File:Casio.jpg"', '"Casio.jpg"'),
            "url sin https": GUION.replace("https://penelope", "http://penelope"),
            "título largo": GUION.replace("Su mejor general lo traicionó", "x" * 101),
        }
        for what, text in cases.items():
            with self.assertRaises(project.ScriptError, msg=what):
                project.parse_script(tomllib.loads(text))

    def test_bad_project_names(self):
        for name in ("../etc", "Mayúsculas", "a", "con espacio"):
            with self.assertRaises(project.ScriptError):
                project.project_dir(name)


class PrepareTest(unittest.TestCase):
    def test_prepare_downloads_each_image_once_and_builds_description(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            folder = project.project_dir("marco-casio")
            folder.mkdir(parents=True)
            (folder / "guion.toml").write_text(GUION, encoding="utf-8")
            store = StudioStore(Path(tmp) / "studio.sqlite3")
            fake = FakeCommons()
            commons = assets.CommonsClient(store, opener=fake, sleep=lambda s: None)
            result = project.prepare("marco-casio", store, commons)
            self.assertTrue(result.ready)
            self.assertEqual(len(result.assets), 2)  # Marco.jpg se usa dos veces pero se baja una
            downloads = len([u for u in fake.requests if "upload" in u])
            self.assertEqual(downloads, 2)
            # Repetir no vuelve a descargar.
            project.prepare("marco-casio", store, commons)
            self.assertEqual(len([u for u in fake.requests if "upload" in u]), downloads)
            text = project.full_description(result.script, result.assets)
            self.assertIn("Dion Casio, Historia romana, libro LXXII", text)
            self.assertIn("Casio.jpg — Fulano (CC-BY-4.0)", text)
            store.close()

    def test_blocked_image_makes_it_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            folder = project.project_dir("marco-casio")
            folder.mkdir(parents=True)
            (folder / "guion.toml").write_text(GUION.replace("File:Casio.jpg", "File:Compartida.jpg"), encoding="utf-8")
            store = StudioStore.in_memory()
            result = project.prepare("marco-casio", store, assets.CommonsClient(store, opener=FakeCommons(), sleep=lambda s: None))
            self.assertFalse(result.ready)
            self.assertEqual([a.title for a in result.blocked], ["File:Compartida.jpg"])
            store.close()


if __name__ == "__main__":
    unittest.main()
