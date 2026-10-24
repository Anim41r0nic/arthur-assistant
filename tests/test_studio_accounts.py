"""Studio, «Cuentas» (2026-10-17): tres perfiles de canal separados del idioma (estoicismo es/en y Frutinovelas).

Todo con falsos: Google y YouTube son «openers» de mentira; nada sale a internet ni se sube nada.
Son las pruebas de aceptación del encargo (cuentas-studio.md):
  - la pantalla lista exactamente los tres perfiles y distingue los estados, sin correo ni permisos;
  - un vídeo de Frutinovelas con el permiso de estoicismo falla antes de subir, y al revés (también en inglés);
  - las revisiones y permisos antiguos de estoicismo siguen yendo al mismo canal;
  - conectar con la cuenta equivocada no deja un permiso a medias; «Comprobar» no publica nada;
  - conectar Frutinovelas no lanza trabajos ni subidas (su publicación sigue desactivada).
"""

import dataclasses
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from arthur.integrations.google import MANUAL_PORT
from arthur.security.devices import DeviceRegistry
from arthur.studio import accounts, channels, project, publish
from arthur.studio.project import ScriptError, parse_script
from arthur.studio.review import ReviewBook
from arthur.studio.store import MIGRATIONS, StudioStore, studio_dir
from arthur.studio.web import StudioWeb

GUION = """
titulo = "Prueba"
descripcion = "Descripción."
idioma = "{idioma}"
{perfil}
[[fuentes]]
id = "f"
obra = "Obra"
autor = "Autor"
[[escenas]]
texto = "Uno."
imagen = "File:A.jpg"
fuentes = ["f"]
"""

REFRESH = "1//permiso-secreto-de-prueba"
CLIENT_SECRET = "secreto-del-cliente-de-prueba"
EMAIL = "cuenta.de.prueba@example.com"   # nunca debe salir hacia la página


class Response(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeGoogle:
    """Google (token) + YouTube (channels.list y subida). `handle`: el canal que devuelve el permiso."""

    def __init__(self, handle="@TuCanalEstoicismo", channel_id="UCestoicismo"):
        self.handle, self.channel_id = handle, channel_id
        self.calls = []
        self.token_status = 200

    def open(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        self.calls.append((request.get_method(), url.hostname, url.path))
        if url.hostname == "oauth2.googleapis.com":
            if self.token_status != 200:
                raise urllib.error.HTTPError(request.full_url, self.token_status, "x", {},
                                             io.BytesIO(b'{"error": "invalid_grant"}'))
            return Response(json.dumps({"access_token": "acceso", "refresh_token": REFRESH, "expires_in": 3600,
                                        "scope": " ".join(publish.SCOPES)}).encode())
        if url.path.endswith("/channels"):
            # Lo que da YouTube de verdad: el @ en minúsculas en customUrl. No lleva el correo (ni lo pedimos).
            return Response(json.dumps({"items": [{"id": self.channel_id, "snippet": {
                "title": "Canal", "customUrl": self.handle.lower()}}]}).encode())
        raise AssertionError(f"petición inesperada: {request.get_method()} {request.full_url}")

    def uploads(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT") and c[1] != "oauth2.googleapis.com"]


class FakeAuth:
    def access_token(self):
        return "acceso"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        self.env.start()
        self.store = StudioStore.in_memory()
        self.book = ReviewBook(self.store)
        studio_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def make_project(self, name, idioma="es", perfil="", kind="largo"):
        folder = project.project_dir(name)
        out = folder / "salida" if kind == "largo" else folder / "salida" / "corto"
        out.mkdir(parents=True, exist_ok=True)
        line = f'perfil = "{perfil}"' if perfil else ""
        (folder / "guion.toml").write_text(GUION.format(idioma=idioma, perfil=line), encoding="utf-8")
        (out / "video.mp4").write_bytes(os.urandom(2000))
        (out / "miniatura.jpg").write_bytes(b"jpg")
        (out / "control.json").write_text('{"ok": true}')
        return folder

    def approved(self, name, idioma="es", perfil="", kind="largo"):
        self.make_project(name, idioma, perfil, kind)
        review = self.book.submit(name, kind)
        return self.book.approve(review.id, review.video_sha256, "movil")

    def write_client(self):
        publish.client_file().write_text(json.dumps({"installed": {
            "client_id": "cliente", "client_secret": CLIENT_SECRET}}))


class ProfilesTest(Base):
    def test_exactly_three_profiles_in_order(self):
        self.assertEqual(channels.ORDER, ("estoicismo_es", "estoicismo_en", "frutinovelas"))
        self.assertEqual(channels.PROFILES["frutinovelas"].handle, "@TuCanalFrutinovelas")
        self.assertEqual(channels.PROFILES["frutinovelas"].language, "es")
        self.assertTrue(channels.PROFILES["frutinovelas"].publishing)   # encendida el 18-oct-2026 (0.66.0)
        self.assertEqual(channels.get("es").id, "estoicismo_es")  # alias viejo de la terminal
        self.assertEqual(channels.get("en").id, "estoicismo_en")
        with self.assertRaises(channels.UnknownProfile):
            channels.get("fr")

    def test_script_profile(self):
        base = {"titulo": "T", "descripcion": "D", "fuentes": [{"id": "f", "obra": "O", "autor": "A"}],
                "escenas": [{"texto": "x", "imagen": "File:A.jpg", "fuentes": ["f"]}]}
        self.assertEqual(parse_script({**base, "idioma": "es"}).perfil, "")
        self.assertEqual(parse_script({**base, "idioma": "es", "perfil": "frutinovelas"}).perfil, "frutinovelas")
        for wrong in ({"idioma": "en", "perfil": "frutinovelas"},     # Frutinovelas es en español
                      {"idioma": "es", "perfil": "estoicismo_en"},
                      {"idioma": "es", "perfil": "es"},               # en el guion, el nombre completo
                      {"idioma": "es", "perfil": "otro"},
                      {"idioma": "es", "perfil": 3}):
            with self.subTest(wrong=wrong), self.assertRaises(ScriptError):
                parse_script({**base, **wrong})

    def test_review_gets_a_stable_profile(self):
        self.make_project("estoico")
        self.make_project("stoic", "en")
        self.make_project("fruta", "es", "frutinovelas")
        es, en, fruta = (self.book.submit(name) for name in ("estoico", "stoic", "fruta"))
        self.assertEqual((es.profile, es.channel), ("estoicismo_es", "@TuCanalEstoicismo"))
        self.assertEqual((en.profile, en.channel), ("estoicismo_en", "@TuCanalStoicEN"))
        self.assertEqual((fruta.profile, fruta.channel, fruta.language), ("frutinovelas", "@TuCanalFrutinovelas", "es"))

    def test_old_reviews_keep_their_channel_after_migration(self):
        path = Path(self.tmp.name) / "viejo.sqlite3"
        db = sqlite3.connect(path)
        for script in MIGRATIONS[:5]:  # una base de antes de los perfiles (esquema 5)
            for statement in filter(str.strip, script.split(";")):
                db.execute(statement)
        db.execute("PRAGMA user_version = 5")
        for project_name, language, channel in (("viejo-es", "es", "@TuCanalEstoicismo"),
                                                ("viejo-en", "en", "@TuCanalStoicEN")):
            db.execute("INSERT INTO reviews (project, language, channel, status, title, video_sha256, thumb_sha256, "
                       "meta_sha256, created_at, updated_at) VALUES (?, ?, ?, 'published', 't', 'v', 't', 'm', "
                       "'2026-10-12T10:00:00', '2026-10-12T10:00:00')", (project_name, language, channel))
        db.commit()
        db.close()
        store = StudioStore(path)
        try:
            reviews = {r.project: r for r in ReviewBook(store).list()}
        finally:
            store.close()
        # La migración no escribe @ (dependen de la configuración): el perfil se resuelve al leer, por su @.
        self.assertEqual(reviews["viejo-es"].target.id, "estoicismo_es")
        self.assertEqual(reviews["viejo-en"].target.id, "estoicismo_en")
        self.assertEqual(reviews["viejo-es"].target.handle, "@TuCanalEstoicismo")

    def test_review_without_profile_still_goes_to_its_channel(self):
        review = self.approved("estoico")
        with self.store._lock, self.store._db:  # una fila rara sin perfil (como si viniera de antes)
            self.store._db.execute("UPDATE reviews SET profile = '' WHERE id = ?", (review.id,))
        asked = []
        yt = FakeGoogle()

        def uploader_for(name):
            asked.append(name)
            return publish.YouTubeUploader(name, auth=FakeAuth(), opener=yt, api="https://api.example/youtube/v3",
                                           upload="https://upload.example/youtube/v3")

        with mock.patch.object(publish.YouTubeUploader, "upload", return_value=("VIDEO1", None)):
            publish.publish(self.book, review.id, uploader_for=uploader_for)
        self.assertEqual(asked, ["estoicismo_es"])

    def test_old_permission_files_are_still_read(self):
        legacy = studio_dir() / "youtube-oauth-es"
        legacy.write_text("viejo")
        (studio_dir() / "youtube-canal-es.json").write_text('{"id": "UCviejo"}')
        self.assertEqual(publish.token_file("estoicismo_es"), legacy)
        self.assertEqual(publish.token_file("es"), legacy)
        self.assertEqual(publish.saved_channel("estoicismo_es"), {"id": "UCviejo"})
        # Frutinovelas también es en español, pero NUNCA usa el permiso de estoicismo.
        self.assertEqual(publish.token_file("frutinovelas").name, "youtube-oauth-frutinovelas")
        self.assertIsNone(publish.saved_channel("frutinovelas"))
        self.assertEqual(publish.token_file("estoicismo_en").name, "youtube-oauth-estoicismo_en")
        # Cuando hay uno nuevo (se volvió a conectar), manda el nuevo.
        (studio_dir() / "youtube-oauth-estoicismo_es").write_text("nuevo")
        self.assertEqual(publish.token_file("es").name, "youtube-oauth-estoicismo_es")
        self.assertTrue(legacy.exists())  # el viejo no se toca


class PublishTest(Base):
    def uploader_for(self, yt, force=None):
        def make(name):
            return publish.YouTubeUploader(force or name, auth=FakeAuth(), opener=yt,
                                           api="https://api.example/youtube/v3",
                                           upload="https://upload.example/youtube/v3")
        return make

    def enable_fruta(self):
        on = dataclasses.replace(channels.PROFILES["frutinovelas"], publishing=True)
        return mock.patch.dict(channels.PROFILES, {"frutinovelas": on})

    def disable_fruta(self):
        # Desde 0.66.0 la publicación de Frutinovelas está encendida; estas pruebas comprueban el interruptor.
        off = dataclasses.replace(channels.PROFILES["frutinovelas"], publishing=False)
        return mock.patch.dict(channels.PROFILES, {"frutinovelas": off})

    def test_frutinovelas_short_with_stoic_permission_fails_before_upload(self):
        review = self.approved("fruta", "es", "frutinovelas", "corto")
        yt = FakeGoogle("@TuCanalEstoicismo")  # el permiso que hay es el de estoicismo ES
        with self.enable_fruta(), self.assertRaises(publish.PublishError) as caught:
            publish.publish(self.book, review.id, uploader_for=self.uploader_for(yt))
        self.assertIn("@TuCanalFrutinovelas", str(caught.exception))
        self.assertEqual(yt.uploads(), [])
        self.assertEqual(self.book.get(review.id).status, "failed")

    def test_stoic_short_cannot_use_frutinovelas(self):
        for name, idioma in (("estoico", "es"), ("stoic", "en")):
            with self.subTest(idioma=idioma):
                review = self.approved(name, idioma, kind="corto")
                yt = FakeGoogle("@TuCanalFrutinovelas")
                # Aunque alguien prepare el permiso de Frutinovelas para este vídeo, no se sube.
                with self.enable_fruta(), self.assertRaises(publish.PublishError):
                    publish.publish(self.book, review.id, uploader_for=self.uploader_for(yt, force="frutinovelas"))
                self.assertEqual(yt.calls, [])
                # Y con el permiso de Frutinovelas guardado en el sitio del de estoicismo, tampoco.
                self.book.retry(review.id, "movil")
                with self.assertRaises(publish.PublishError):
                    publish.publish(self.book, review.id, uploader_for=self.uploader_for(yt))
                self.assertEqual(yt.uploads(), [])

    def test_frutinovelas_publishing_is_off(self):
        review = self.approved("fruta", "es", "frutinovelas")
        asked = []
        with self.disable_fruta(), self.assertRaises(publish.PublishError) as caught:
            publish.publish(self.book, review.id, uploader_for=asked.append)
        self.assertIn("desactivada", str(caught.exception))
        self.assertEqual(asked, [])  # ni siquiera se prepara el permiso
        self.assertEqual(self.book.get(review.id).status, "failed")

    def test_frutinovelas_with_its_own_permission_would_upload_when_enabled(self):
        review = self.approved("fruta", "es", "frutinovelas")
        yt = FakeGoogle("@TuCanalFrutinovelas", "UCfruta")
        with self.enable_fruta(), mock.patch.object(publish.YouTubeUploader, "upload",
                                                    return_value=("VIDEO9", None)) as upload:
            publish.publish(self.book, review.id, uploader_for=self.uploader_for(yt))
        self.assertEqual(upload.call_args[0][0]["channel"], "@TuCanalFrutinovelas")


class ConnectTest(Base):
    def setUp(self):
        super().setUp()
        self.write_client()
        self.urls = []

    def browser(self, url):
        self.urls.append(url)

    def paste(self, _prompt):
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.urls[-1]).query))["state"]
        return f"http://127.0.0.1:{MANUAL_PORT}/?code=codigo&state={state}"

    def leftovers(self):
        return sorted(p.name for p in studio_dir().iterdir() if "pendiente" in p.name or p.suffix == ".tmp")

    def test_wrong_account_leaves_nothing_and_keeps_the_old_permission(self):
        old = studio_dir() / "youtube-oauth-es"
        old.write_text("permiso-bueno-anterior")
        google = FakeGoogle("@OtroCanal")
        with self.assertRaises(publish.PublishError) as caught:
            publish.connect("es", self.browser, self.paste, opener=google)
        self.assertIn("@otrocanal", str(caught.exception).lower())
        self.assertEqual(old.read_text(), "permiso-bueno-anterior")
        self.assertFalse((studio_dir() / "youtube-oauth-estoicismo_es").exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(google.uploads(), [])

    def test_frutinovelas_with_the_stoic_account_is_not_saved(self):
        google = FakeGoogle("@TuCanalEstoicismo")
        with self.assertRaises(publish.PublishError):
            publish.connect("frutinovelas", self.browser, self.paste, opener=google)
        self.assertFalse(publish.token_file("frutinovelas").exists())
        self.assertIsNone(publish.saved_channel("frutinovelas"))
        self.assertEqual(self.leftovers(), [])

    def test_same_handle_but_different_channel_is_refused(self):
        (studio_dir() / "youtube-canal-frutinovelas.json").write_text('{"id": "UCfruta"}')
        with self.assertRaises(publish.PublishError):
            publish.connect("frutinovelas", self.browser, self.paste,
                            opener=FakeGoogle("@TuCanalFrutinovelas", "UCimpostor"))
        self.assertFalse(publish.token_file("frutinovelas").exists())

    def test_connect_frutinovelas_checks_pins_id_and_launches_nothing(self):
        google = FakeGoogle("@TuCanalFrutinovelas", "UCfruta")
        recorded = []
        channel = publish.connect("frutinovelas", self.browser, self.paste, opener=google,
                                  record=lambda p, c: recorded.append((p.id, c["id"])))
        self.assertEqual(channel["id"], "UCfruta")
        token = publish.token_file("frutinovelas")
        self.assertEqual(token.name, "youtube-oauth-frutinovelas")
        self.assertEqual(token.read_text(), REFRESH)
        self.assertEqual(oct(token.stat().st_mode & 0o777), "0o600")
        self.assertEqual(publish.saved_channel("frutinovelas")["id"], "UCfruta")
        self.assertEqual(recorded, [("frutinovelas", "UCfruta")])
        self.assertEqual(self.leftovers(), [])
        # Solo se ha pedido el permiso y se ha preguntado qué canal es: ni subidas ni trabajos.
        self.assertEqual(google.uploads(), [])
        self.assertEqual(self.store.list_jobs(), [])
        self.assertIn("youtube.upload", self.urls[0])
        # Conectar no cambia el interruptor de publicación (desde 0.66.0 está encendido en el código).
        self.assertTrue(channels.get("frutinovelas").publishing)

    def test_reconnecting_stoic_keeps_old_files_and_uses_the_new_one(self):
        (studio_dir() / "youtube-oauth-es").write_text("viejo")
        (studio_dir() / "youtube-canal-es.json").write_text('{"id": "UCestoicismo"}')
        publish.connect("estoicismo_es", self.browser, self.paste, opener=FakeGoogle())
        self.assertEqual(publish.token_file("es").name, "youtube-oauth-estoicismo_es")
        self.assertTrue((studio_dir() / "youtube-oauth-es").exists())

    def test_cli_accepts_the_three_profiles_and_the_old_aliases(self):
        from arthur import __main__ as cli

        for name in (*channels.ORDER, "es", "en"):
            with self.subTest(name=name), mock.patch.object(cli, "studio_connect_command", return_value=0) as run:
                self.assertEqual(cli.main(["--studio-conectar-youtube", name]), 0)
                run.assert_called_once_with(name)

    def test_cli_connect_records_connected_for_the_page(self):
        from arthur import __main__ as cli

        google = FakeGoogle("@TuCanalFrutinovelas", "UCfruta")
        real = publish.connect
        # El navegador y el «pegar» de la terminal, falsos; el resto es la orden de verdad.
        fake = lambda name, _browser, paste=None, **k: real(name, self.browser, self.paste, opener=google, **k)
        with mock.patch.object(publish, "connect", side_effect=fake), mock.patch("builtins.print"):
            self.assertEqual(cli.studio_connect_command("frutinovelas"), 0)
        row = accounts.row("frutinovelas")
        self.assertEqual(row["status"], "connected")
        self.assertEqual(row["channel_id"], "UCfruta")


class AccountsTest(Base):
    def accounts(self, yt):
        return accounts.Accounts(uploader_for=lambda name: publish.YouTubeUploader(
            name, auth=FakeAuth(), opener=yt, api="https://api.example/youtube/v3",
            upload="https://upload.example/youtube/v3"))

    def test_three_rows_initially_disconnected(self):
        rows = accounts.Accounts().rows()
        self.assertEqual([r["profile"] for r in rows], ["estoicismo_es", "estoicismo_en", "frutinovelas"])
        self.assertEqual([r["status"] for r in rows], ["disconnected"] * 3)
        self.assertEqual(rows[2]["detail"], "Canal creado · pendiente de conectar")
        self.assertEqual(rows[2]["handle"], "@TuCanalFrutinovelas")
        self.assertTrue(rows[2]["publishing"])
        self.assertEqual(rows[2]["connect_command"], "arthur --studio-conectar-youtube frutinovelas")

    def test_states_are_told_apart_and_nothing_secret_leaks(self):
        self.write_client()
        for name in ("youtube-oauth-es", "youtube-oauth-estoicismo_en", "youtube-oauth-frutinovelas"):
            (studio_dir() / name).write_text(REFRESH)
        # Un fichero de canal con más cosas de las que tocan: solo sale el id.
        (studio_dir() / "youtube-canal-es.json").write_text(json.dumps({"id": "UCestoicismo", "email": EMAIL}))
        moments = iter(f"2026-10-17T1{n}:00:00" for n in range(10))
        from datetime import datetime
        yt = FakeGoogle()
        acc = self.accounts(yt)
        acc.now = lambda: datetime.fromisoformat(next(moments))
        # Permiso guardado pero sin preguntar a Google: no se da por conectado.
        self.assertEqual([r["status"] for r in acc.rows()], ["unverified"] * 3)
        connected = acc.check("estoicismo_es")
        self.assertEqual((connected["status"], connected["checked_at"]), ("connected", "2026-10-17T10:00:00"))
        self.assertEqual(connected["channel_id"], "UCestoicismo")
        wrong = acc.check("frutinovelas")   # el permiso de Frutinovelas resulta ser del canal de estoicismo
        self.assertEqual(wrong["status"], "wrong_account")
        yt.channel_id = "UCotro"           # mismo @ que el conectado, pero otro canal
        self.assertEqual(acc.check("estoicismo_es")["status"], "wrong_account")
        with mock.patch.object(publish.YouTubeUploader, "my_channel", side_effect=publish.PermissionLost("caducó")):
            self.assertEqual(acc.check("estoicismo_en")["status"], "reconnect")
        with mock.patch.object(publish.YouTubeUploader, "my_channel", side_effect=publish.PublishError("sin red")):
            self.assertEqual(acc.check("estoicismo_en")["status"], "unavailable")
        (studio_dir() / "youtube-oauth-estoicismo_en").unlink()
        self.assertEqual(acc.check("estoicismo_en")["status"], "disconnected")
        # «Comprobar» solo pregunta qué canal es: ninguna subida, ningún trabajo.
        self.assertEqual(yt.uploads(), [])
        self.assertTrue(all(c == ("GET", "api.example", "/youtube/v3/channels") for c in yt.calls))
        self.assertEqual(self.store.list_jobs(), [])
        page = json.dumps(acc.rows(), ensure_ascii=False)
        for secret in (REFRESH, CLIENT_SECRET, EMAIL, "acceso"):
            self.assertNotIn(secret, page)

    def test_expired_permission_from_google_means_reconnect(self):
        self.write_client()
        (studio_dir() / "youtube-oauth-frutinovelas").write_text(REFRESH)
        google = FakeGoogle("@TuCanalFrutinovelas")
        google.token_status = 400  # invalid_grant: caducó o se retiró
        acc = accounts.Accounts(uploader_for=lambda name: publish.YouTubeUploader(name, opener=google))
        self.assertEqual(acc.check("frutinovelas")["status"], "reconnect")

    def test_missing_client_is_unavailable_not_reconnect(self):
        (studio_dir() / "youtube-oauth-frutinovelas").write_text(REFRESH)
        self.assertEqual(accounts.Accounts().check("frutinovelas")["status"], "unavailable")

    def test_newer_permission_makes_an_old_check_stale(self):
        token = studio_dir() / "youtube-oauth-frutinovelas"
        token.write_text(REFRESH)
        from datetime import datetime
        accounts.remember(channels.get("frutinovelas"), "connected", "ok", datetime(2026, 10, 17, 9))
        self.assertEqual(accounts.row("frutinovelas")["status"], "connected")
        later = time.time() + 5
        os.utime(token, (later, later))  # se ha vuelto a conectar después de comprobar
        self.assertEqual(accounts.row("frutinovelas")["status"], "unverified")


class AccountsWebTest(Base):
    def setUp(self):
        super().setUp()
        self.write_client()
        (studio_dir() / "youtube-oauth-frutinovelas").write_text(REFRESH)
        self.yt = FakeGoogle("@TuCanalFrutinovelas", "UCfruta")
        acc = accounts.Accounts(uploader_for=lambda name: publish.YouTubeUploader(
            name, auth=FakeAuth(), opener=self.yt, api="https://api.example/youtube/v3"))
        self.devices = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.key = self.devices.add("movil")
        self.web = StudioWeb(self.book, self.devices, "127.0.0.1", 0, accounts=acc)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        super().tearDown()

    def call(self, path, method="GET", key=True, headers=None):
        req = urllib.request.Request(self.web.url + path, method=method, data=b"" if method == "POST" else None)
        if key:
            req.add_header("Authorization", f"Bearer {self.key}")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_accounts_need_a_key(self):
        self.assertEqual(self.call("/api/accounts", key=False)[0], 401)
        self.assertEqual(self.call("/api/accounts/frutinovelas/check", "POST", key=False)[0], 401)
        # Con la cookie (desde el navegador) hace falta además la cabecera de la página.
        cookie = {"Cookie": f"studio_llave={self.key}"}
        self.assertEqual(self.call("/api/accounts/frutinovelas/check", "POST", key=False, headers=cookie)[0], 401)
        self.assertEqual(self.call("/api/accounts/frutinovelas/check", "POST", key=False,
                                   headers={**cookie, "X-Arthur": "1"})[0], 200)

    def test_list_and_check(self):
        status, body = self.call("/api/accounts")
        self.assertEqual(status, 200)
        rows = json.loads(body)["accounts"]
        self.assertEqual([r["profile"] for r in rows], list(channels.ORDER))
        self.assertEqual(rows[2]["status"], "unverified")
        status, body = self.call("/api/accounts/frutinovelas/check", "POST")
        self.assertEqual(status, 200)
        checked = json.loads(body)
        self.assertEqual((checked["status"], checked["channel_id"]), ("connected", None))
        self.assertNotIn(REFRESH, body.decode())
        self.assertEqual(self.yt.uploads(), [])
        self.assertEqual(self.store.list_jobs(), [])  # conectar/comprobar Frutinovelas no lanza trabajos

    def test_unknown_or_alias_profile_is_404(self):
        for name in ("otro", "es", "en", "../x"):
            with self.subTest(name=name):
                self.assertEqual(self.call(f"/api/accounts/{name}/check", "POST")[0], 404)

    def test_page_has_the_accounts_section_and_labelled_key(self):
        html = (Path(publish.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="nav-accounts"', html)
        self.assertIn('<label for="key">', html)
        js = (Path(publish.__file__).parent / "web" / "studio.js").read_text(encoding="utf-8")
        self.assertIn('button.className = "review-card"', js)  # la tarjeta es un botón (teclado)
        self.assertNotRegex(js, r"\.innerHTML\s*=")  # todo con textContent
        for state in accounts.STATES:
            self.assertIn(f"{state}:", js)


if __name__ == "__main__":
    unittest.main()
