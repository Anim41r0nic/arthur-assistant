"""Órdenes del cerebro (cerebro/cli.py), sus ajustes (cerebro/settings.py) y el montaje desde el entorno. Sin red."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arthur import __main__ as main_mod
from arthur.cerebro import cli
from arthur.cerebro.settings import CerebroError, load_settings
from arthur.security.devices import DeviceRegistry

ACCOUNT = "0123456789abcdef0123456789abcdef"
TOKEN = "t" * 40


class DeviceTest(unittest.TestCase):
    def test_cli_dispositivo_nuevo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "devices.json"
            shown = []
            self.assertEqual(cli.new_device("panel", DeviceRegistry(path), out=shown.append), 0)
            key = shown[0].split("\n\n")[1].strip()
            self.assertNotIn(key, path.read_text(encoding="utf-8"))  # solo queda la huella
            self.assertEqual(DeviceRegistry(path).check(key), "panel")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.new_device("panel", DeviceRegistry(path), out=shown.append), 1)
            self.assertEqual(len(shown), 1)  # la llave se enseña una sola vez

    def test_aviso_si_no_es_de_los_del_cerebro(self):
        with tempfile.TemporaryDirectory() as tmp:
            shown = []
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.new_device("studio", DeviceRegistry(Path(tmp) / "d.json"), out=shown.append), 1)
            self.assertEqual(shown, [])
            self.assertIn("core", err.getvalue())

    def test_orden_conectada(self):
        args = mock.Mock(cerebro_web=False, cerebro_dispositivo_nuevo="core")
        with mock.patch.object(cli, "new_device", return_value=0) as nd:
            self.assertEqual(cli.cerebro_command(args, {}), 0)
        nd.assert_called_once_with("core")

    def test_main_reconoce_las_opciones(self):
        with mock.patch("arthur.cerebro.cli.cerebro_command", return_value=0) as cmd:
            self.assertEqual(main_mod.main(["--cerebro-dispositivo-nuevo", "core"]), 0)
            self.assertEqual(main_mod.main(["--cerebro-web"]), 0)
        self.assertEqual(cmd.call_count, 2)
        self.assertEqual(cmd.call_args_list[0].args[0].cerebro_dispositivo_nuevo, "core")
        self.assertTrue(cmd.call_args_list[1].args[0].cerebro_web)


class SettingsTest(unittest.TestCase):
    def test_por_defecto(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cerebro" / "ajustes.toml"
            s = load_settings(path)
            self.assertTrue(path.exists())
            self.assertEqual(s.budget, 3000)
            self.assertEqual(s.vault_cerebro, Path("/vaults/cerebro"))
            self.assertEqual(s.vault_business, Path("/vaults/business-lab"))
            self.assertEqual(s.pdf, Path("/vaults/cerebro/PDF"))
            self.assertEqual(str(s.zone), "Europe/Madrid")

    def test_valores_malos(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ajustes.toml"
            for text in ('presupuesto = "mucho"\n', "presupuesto = -1\n", "presupuesto = 20000\n",
                         'zona = "America/Lima"\n', 'boveda_cerebro = ""\n', "esto no es toml ==\n"):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(CerebroError, msg=text):
                    load_settings(path)

    def test_propios(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ajustes.toml"
            path.write_text('presupuesto = 1500\nboveda_cerebro = "/b/c"\npdf = "/b/c/Docs"\n', encoding="utf-8")
            s = load_settings(path)
            self.assertEqual((s.budget, s.vault_cerebro, s.pdf), (1500, Path("/b/c"), Path("/b/c/Docs")))


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = load_settings(self.root / "ajustes.toml")

    def tearDown(self):
        self.tmp.cleanup()

    def secret(self, name, value):
        path = self.root / name
        path.write_text(value + "\n", encoding="utf-8")
        return str(path)

    def test_sin_nada_arranca(self):
        rt = cli.build_runtime({}, self.settings, data=self.root)
        self.assertEqual((rt.host, rt.port), ("127.0.0.1", 8778))
        self.assertIsNone(rt.cloud)
        self.assertIsNone(rt.embedder)
        self.assertIsNone(rt.apuntes)
        self.assertIsNotNone(rt.local)
        names = [s.name for s in rt.sources()]
        self.assertEqual(names, ["obsidian:cerebro", "obsidian:business-lab", "pdf"])  # sin Apuntes configurado

    def test_con_todo(self):
        env = {"ARTHUR_CEREBRO_HOST": "0.0.0.0", "ARTHUR_CEREBRO_PORT": "9000",
               "ARTHUR_APUNTES_URL": "http://apuntes-web:8774",
               "ARTHUR_CEREBRO_APUNTES_KEY_FILE": self.secret("apuntes", "arthur_llave"),
               "ARTHUR_WEBCHAT_CF_ACCOUNT": ACCOUNT,
               "ARTHUR_CEREBRO_CF_TOKEN_FILE": self.secret("cf", TOKEN),
               "ARTHUR_APUNTES_PUBLIC_URL": "http://100.1.2.3:8774"}
        rt = cli.build_runtime(env, self.settings, data=self.root)
        self.assertEqual((rt.host, rt.port), ("0.0.0.0", 9000))
        self.assertIsNotNone(rt.cloud)
        self.assertIsNotNone(rt.embedder)
        self.assertNotIn(TOKEN, repr(rt.cloud) + repr(rt.embedder))
        self.assertEqual(rt.apuntes.url, "http://apuntes-web:8774")
        self.assertEqual(rt.links.apuntes_url, "http://100.1.2.3:8774/")
        self.assertEqual([s.name for s in rt.sources()][-1], "apuntes")

    def test_secreto_ilegible_no_tumba(self):
        env = {"ARTHUR_WEBCHAT_CF_ACCOUNT": ACCOUNT, "ARTHUR_CEREBRO_CF_TOKEN_FILE": str(self.root / "no-existe"),
               "ARTHUR_APUNTES_URL": "http://apuntes-web:8774",
               "ARTHUR_CEREBRO_APUNTES_KEY_FILE": str(self.root / "tampoco")}
        with self.assertLogs("arthur.cerebro", "WARNING"):
            rt = cli.build_runtime(env, self.settings, data=self.root)
        self.assertIsNone(rt.cloud)
        self.assertIsNone(rt.apuntes)

    def test_fuentes_leen_las_carpetas(self):
        vault = self.root / "boveda"
        (vault / "Uni").mkdir(parents=True)
        (vault / "Uni" / "nota.md").write_text("# Nota\nHola", encoding="utf-8")
        self.settings = load_settings(self.root / "otros.toml")
        (self.root / "otros.toml").write_text(f'boveda_cerebro = "{vault}"\npdf = "{vault}/PDF"\n', encoding="utf-8")
        rt = cli.build_runtime({}, load_settings(self.root / "otros.toml"), data=self.root)
        docs, alive = rt.sources()[0].read(None)
        self.assertEqual([d.ref for d in docs], ["Uni/nota.md"])
        self.assertIsNone(alive)


class ApunteLinkTest(unittest.TestCase):
    def test_enlace_y_trozo_que_ya_no_existe(self):
        from types import SimpleNamespace

        from arthur.cerebro.chunks import Doc
        from arthur.cerebro.index import Index

        with tempfile.TemporaryDirectory() as tmp:
            index = Index(Path(tmp) / "indice.sqlite3")
            self.addCleanup(index.close)
            index.sync([Doc(source="apuntes", ref="apunte:12", title="T", subject="Cálculo",
                            changed_at="2026-10-17T20:05:00+02:00", text="Uno.")],
                       source="apuntes", alive_refs={"apunte:12"})
            [cid] = index.chunk_ids("apuntes", {"Cálculo"})
            links = SimpleNamespace(apuntes_url="https://apuntes.example")
            self.assertEqual(cli.apunte_link(index, links, cid), "https://apuntes.example#n12")
            # Un trozo que se rehízo o se borró: sin enlace (la página lo esconde), nunca un error
            self.assertEqual(cli.apunte_link(index, links, cid + 99), "")


if __name__ == "__main__":
    unittest.main()
