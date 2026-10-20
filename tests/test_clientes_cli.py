"""Órdenes de clientes (clientes/cli.py y __main__): alta de aparatos, llave de webchat y montaje. Sin red."""

import contextlib
import io
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from arthur import __main__ as entry
from arthur.clientes import cli
from arthur.clientes.store import ClientStore
from arthur.clientes.web import WebchatKey
from arthur.security.devices import DeviceRegistry


class DirTests(unittest.TestCase):
    def test_data_dir_from_env(self):
        self.assertEqual(cli.clientes_dir({"ARTHUR_CLIENTES_DATA": "/data"}), Path("/data"))
        self.assertEqual(cli.clientes_dir({"XDG_DATA_HOME": "/x"}), Path("/x/arthur/clientes"))


class DeviceTests(unittest.TestCase):
    def test_dispositivo_nuevo_prints_key_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"ARTHUR_CLIENTES_DATA": tmp}
            shown = []
            self.assertEqual(cli.new_device("movil", env, out=shown.append), 0)
            self.assertEqual(len(shown), 1)
            key = shown[0].split("\n\n")[1].strip()
            path = Path(tmp) / "devices.json"
            self.assertNotIn(key, path.read_text(encoding="utf-8"))  # solo queda la huella
            self.assertEqual(DeviceRegistry(path).check(key), "movil")
            # el mismo nombre otra vez no da otra llave
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.new_device("movil", env, out=shown.append), 1)
            self.assertEqual(len(shown), 1)

    def test_webchat_is_a_reserved_device_name(self):
        # «webchat» es la llave especial de la ruta interna: un aparato con ese nombre confundiría los registros.
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.new_device("webchat", {"ARTHUR_CLIENTES_DATA": tmp}, out=print), 1)
            self.assertFalse((Path(tmp) / "devices.json").exists())


class WebchatKeyCommandTests(unittest.TestCase):
    def test_llave_webchat_prints_only_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"ARTHUR_CLIENTES_DATA": tmp}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.webchat_key_command(env), 0)
            text = out.getvalue()
            self.assertEqual(text.count("\n"), 1)  # una línea: sirve tal cual para «> secrets/clientes_webchat»
            key = text.strip()
            self.assertTrue(key.startswith("arthur_"))
            key_file = WebchatKey(Path(tmp) / "webchat.key")
            self.assertTrue(key_file.check(key))
            self.assertNotIn(key, key_file.path.read_text(encoding="utf-8"))
            # volver a pedirla la rota: la anterior deja de valer
            with contextlib.redirect_stdout(io.StringIO()):
                cli.webchat_key_command(env)
            self.assertFalse(key_file.check(key))


class CommandWiringTests(unittest.TestCase):
    def args(self, **kw):
        base = dict(clientes_web=False, clientes_dispositivo_nuevo=None, clientes_llave_webchat=False)
        return mock.Mock(**(base | kw))

    def test_command_dispatch(self):
        with mock.patch.object(cli, "new_device", return_value=0) as nd:
            self.assertEqual(cli.clientes_command(self.args(clientes_dispositivo_nuevo="movil"), {}), 0)
        nd.assert_called_once_with("movil", {})
        with mock.patch.object(cli, "webchat_key_command", return_value=0) as wk:
            self.assertEqual(cli.clientes_command(self.args(clientes_llave_webchat=True), {}), 0)
        wk.assert_called_once_with({})
        with mock.patch.object(cli, "_serve", return_value=0) as serve:
            self.assertEqual(cli.clientes_command(self.args(clientes_web=True), {}), 0)
        serve.assert_called_once_with({})

    def test_main_flags(self):
        with mock.patch.object(cli, "clientes_command", return_value=0) as cc:
            self.assertEqual(entry.main(["--clientes-dispositivo-nuevo", "movil"]), 0)
            self.assertEqual(entry.main(["--clientes-llave-webchat"]), 0)
            self.assertEqual(entry.main(["--clientes-web"]), 0)
        flags = [(a.clientes_dispositivo_nuevo, a.clientes_llave_webchat, a.clientes_web)
                 for a, in (c.args for c in cc.call_args_list)]
        self.assertEqual(flags, [("movil", False, False), (None, True, False), (None, False, True)])

    def test_runtime_from_env(self):
        rt = cli.runtime_settings({"ARTHUR_CLIENTES_HOST": "0.0.0.0", "ARTHUR_CLIENTES_PORT": "9000",
                                   "ARTHUR_CLIENTES_DATA": "/d"})
        self.assertEqual((rt.host, rt.port, rt.data), ("0.0.0.0", 9000, Path("/d")))
        rt = cli.runtime_settings({})
        self.assertEqual((rt.host, rt.port), ("127.0.0.1", cli.DEFAULT_PORT))
        self.assertEqual(cli.DEFAULT_PORT, 8777)


class PurgeLoopTests(unittest.TestCase):
    def test_purges_at_start_and_survives_errors(self):
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        reloj = {"t": now}
        store = ClientStore(":memory:", lambda: reloj["t"])
        store.record_contact("Ana", "ana@example.com", "", "hola", "es")
        reloj["t"] = now + timedelta(days=400)
        stop = threading.Event()
        stop.set()  # una sola vuelta: purga al arrancar y sale
        with self.assertLogs("arthur.clientes", "INFO"):
            cli._purge_loop(store, stop)
        self.assertEqual(store.list(), [])
        broken = mock.Mock(purge=mock.Mock(side_effect=RuntimeError("roto")))
        with self.assertLogs("arthur.clientes", "ERROR"):
            cli._purge_loop(broken, stop)  # no revienta
        store.close()


if __name__ == "__main__":
    unittest.main()
