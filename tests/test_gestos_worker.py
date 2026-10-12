"""Tests del proceso de la cámara y su tubería (paso 3 de docs/gestos.md).

NINGÚN test toca la cámara: el «proceso de la cámara» es un script FALSO de Python que solo escribe líneas (válidas,
basura, imágenes y puntos de la mano simulados). bwrap tampoco se ejecuta: de la burbuja se comprueba la orden.
"""

import base64
import hashlib
import json
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from arthur.core.config import ConfigError, config_from_dict
from arthur.interfaces.gestos import Gesture, GestureReading, HandLandmarks, ListFrameSource
from arthur.interfaces.gestos import camera_worker
from arthur.interfaces.gestos.protocol import (
    MAX_LINE_BYTES,
    GestureLine,
    StatusLine,
    decode,
    encode_reading,
    encode_status,
)
from arthur.interfaces.gestos.source import Frame, FrameSourceError
from arthur.interfaces.gestos.worker import CAMERA_DEVICE, CHILD, GestureWorker, WorkerUnavailable, sandbox_argv

FAKE_HAND_POINTS = [[0.1 * i, 0.2 * i] for i in range(21)]


class ProtocolTest(unittest.TestCase):
    def test_valid_lines(self):
        self.assertEqual(decode(b'{"gesture": "palma", "score": 0.93}'), GestureLine("palma", 0.93))
        self.assertEqual(decode(b'{"score": 1, "gesture": "pulgar_abajo"}'), GestureLine("pulgar_abajo", 1.0))
        self.assertEqual(decode(b'{"gesture": null, "score": 0.0}'), GestureLine(None, 0.0))
        self.assertEqual(decode(b'{"status": "no-camera"}'), StatusLine("no-camera"))

    def test_everything_else_is_ignored(self):
        bad = [
            b"", b"hola", b"null", b"[]", b'"palma"', b"{}",
            b'{"gesture": "pinza", "score": 0.99}',             # nivel 3: la pinza aún no viaja
            b'{"gesture": "pulgar_menique", "score": 0.99}',    # retirado en 0.37.2
            b'{"gesture": "mover_lateral", "score": 0.99}',     # retirado en 0.38.1
            b'{"gesture": "puno ", "score": 0.9}',
            b'{"gesture": "PALMA", "score": 0.9}',
            b'{"gesture": "palma"}',
            b'{"gesture": "palma", "score": 1.5}',
            b'{"gesture": "palma", "score": -0.1}',
            b'{"gesture": "palma", "score": NaN}',
            b'{"gesture": "palma", "score": Infinity}',
            b'{"gesture": "palma", "score": true}',
            b'{"gesture": "palma", "score": "0.9"}',
            b'{"gesture": ["palma"], "score": 0.9}',
            b'{"gesture": "palma", "score": 0.9, "points": []}',  # clave de más: ni siquiera vacía
            b'{"gesture": "palma", "gesture": "pulgar_abajo", "score": 0.9}',
            b'{"status": "ready", "detail": "x"}',
            b'{"status": "camara-rota"}',
            b'{"status": "ready", "status": "error"}',
            '{"gesture": "palma", "score": 0.9, "x": "ñ"}'.encode(),
            b'{"gesture": "palma", "score": 0.9}' + b" " * MAX_LINE_BYTES,
            json.dumps({"landmarks": FAKE_HAND_POINTS}).encode(),
            json.dumps({"frame": base64.b64encode(bytes(64)).decode()}).encode(),
            b"[" * 5000 + b"]" * 5000,
        ]
        for line in bad:
            with self.subTest(line=line[:60]):
                self.assertIsNone(decode(line))

    def test_the_camera_side_never_sends_the_pinch(self):
        self.assertEqual(json.loads(encode_reading(GestureReading(Gesture.PINCH, 0.99))),
                         {"gesture": None, "score": 0.0})
        self.assertEqual(decode(encode_reading(GestureReading(Gesture.THUMB_UP, 0.99)).encode()),
                         GestureLine("pulgar_arriba", 0.99))
        self.assertEqual(json.loads(encode_reading(None)), {"gesture": None, "score": 0.0})
        line = encode_reading(GestureReading(Gesture.PALM, 0.876543))
        self.assertEqual(decode(line.encode()), GestureLine("palma", 0.877))
        self.assertEqual(decode(encode_status("ready").encode()), StatusLine("ready"))
        with self.assertRaises(ValueError):
            encode_status("cualquier cosa")


class CameraWorkerTest(unittest.TestCase):
    """El proceso de la cámara, con fuentes de MENTIRA (sin cámara)."""

    def test_without_detector_it_says_so_and_does_not_open_anything(self):
        out = StringIO()
        self.assertIsNone(camera_worker.open_camera(8, Path("/no/existe")))
        self.assertEqual(camera_worker.run(None, out), 3)
        self.assertEqual(out.getvalue(), '{"status": "no-detector"}\n')

    def test_models_with_another_fingerprint_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            self.assertIsNone(camera_worker.verified_models(models))  # no están
            for name in camera_worker.MODEL_SHA256:
                (models / name).write_bytes(b"no soy un modelo")
            self.assertIsNone(camera_worker.verified_models(models))  # están, pero cambiados
            self.assertIsNone(camera_worker.open_camera(8, models))
            fake = {name: hashlib.sha256(b"no soy un modelo").hexdigest() for name in camera_worker.MODEL_SHA256}
            with mock.patch.dict(camera_worker.MODEL_SHA256, fake):
                self.assertEqual(camera_worker.verified_models(models),
                                 (models / camera_worker.PALM_MODEL, models / camera_worker.HAND_MODEL))
                # huella «buena» pero cv2 no puede cargarlo: tampoco hay detector, y no se lanza nada
                self.assertIsNone(camera_worker.open_camera(8, models))

    @unittest.skipUnless((Path.home() / ".local/share/arthur/gestos").is_dir(), "sin los modelos instalados")
    def test_with_the_real_models_there_is_a_detector_but_the_camera_stays_closed(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("sin cv2")
        source = camera_worker.open_camera(8, Path.home() / ".local/share/arthur/gestos")
        self.assertIsNotNone(source)
        self.assertIsNone(source._capture)  # solo `with` / iter_readings la abre

    def test_it_only_writes_whitelisted_lines(self):
        palm = HandLandmarks([(0.0, 0.0)] * 21)  # puntos amontonados: no es ningún gesto
        source = ListFrameSource([Frame(0.0, None), Frame(0.1, palm)])
        out = StringIO()
        self.assertEqual(camera_worker.run(source, out), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], '{"status": "ready"}')
        self.assertEqual([decode(line.encode()) for line in lines[1:]], [GestureLine(None, 0.0)] * 2)
        self.assertFalse(source.is_open)  # la «cámara» queda cerrada

    def test_a_camera_that_fails_is_reported_without_details(self):
        class Broken(ListFrameSource):
            def open(self):
                raise FrameSourceError("/dev/video0: secreto")

        out = StringIO()
        self.assertEqual(camera_worker.run(Broken([]), out), 2)
        self.assertEqual(out.getvalue(), '{"status": "no-camera"}\n')

    def test_the_real_script_runs_without_camera(self):
        """El script tal cual (con `python -I`, como dentro de bwrap): sin detector, avisa y termina."""
        import subprocess

        proc = subprocess.run([sys.executable, "-I", "-B", str(CHILD), "--models", "/no/existe"],
                              input=b"", capture_output=True, timeout=20, env={})
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(proc.stdout, b'{"status": "no-detector"}\n')


class SandboxArgvTest(unittest.TestCase):
    def setUp(self):
        self.argv = sandbox_argv(8, bwrap="/usr/bin/bwrap", home=Path("/home/usuario"), models=Path("/m/gestos"))

    def test_no_network_and_dies_with_arthur(self):
        for flag in ("--unshare-net", "--die-with-parent", "--clearenv", "--new-session", "--unshare-pid"):
            self.assertIn(flag, self.argv)
        self.assertNotIn("--share-net", self.argv)

    def test_home_is_empty_and_only_the_camera_is_a_device(self):
        a = self.argv
        pairs = [a[i:i + 2] for i in range(len(a))]
        self.assertIn(["--tmpfs", "/home/usuario"], pairs)
        self.assertIn(["--dev", "/dev"], pairs)
        devices = [a[i + 1] for i, x in enumerate(a) if x in ("--dev-bind", "--dev-bind-try")]
        self.assertEqual(devices, [CAMERA_DEVICE])
        writable = [a[i + 1] for i, x in enumerate(a) if x in ("--bind", "--bind-try")]
        self.assertEqual(writable, [])  # nada con escritura

    def test_runs_the_camera_script_isolated(self):
        cmd = self.argv[self.argv.index("--") + 1:]
        self.assertEqual(cmd[:4], ["/usr/bin/python3", "-I", "-B", str(CHILD)])
        self.assertEqual(cmd[4:], ["--fps", "8", "--models", "/m/gestos"])

    def test_without_bwrap_nothing_is_launched(self):
        from unittest import mock

        with mock.patch("arthur.interfaces.gestos.worker.shutil.which", return_value=None):
            with self.assertRaises(WorkerUnavailable):
                sandbox_argv(8)
            worker = GestureWorker(lambda m: None)
            with self.assertRaises(WorkerUnavailable):
                worker.start()
            self.assertFalse(worker.running)


FAKE_CAMERA = textwrap.dedent(r'''
    import base64, json, sys, time
    out = sys.stdout
    def send(text):
        out.write(text + "\n"); out.flush()
    send('{"status": "ready"}')
    send("esto no es json")
    send(json.dumps({"frame": base64.b64encode(bytes(range(256)) * 400).decode()}))  # «imagen» de 100 KB
    send(json.dumps({"landmarks": [[0.1 * i, 0.2 * i, 0.0] for i in range(21)]}))  # «puntos de la mano»
    send('{"gesture": "palma", "score": 0.9, "points": [[1, 2]]}')
    send('{"gesture": "pinza", "score": 0.99}')
    send('{"gesture": "palma", "score": 0.91}')
    out.write('{"gesture": "pulgar_abajo",'); out.flush()  # una línea que llega a trozos
    time.sleep(0.05)
    send(' "score": 0.85}')
    sys.stdout.buffer.write(b"\xff\xfe binario\n"); sys.stdout.flush()
    send('{"gesture": null, "score": 0.0}')
    sys.stdin.read()  # se queda «con la cámara encendida» hasta que ARTHUR cierra la tubería
''')


class GestureWorkerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.script = Path(tmp.name) / "camara_falsa.py"
        self.script.write_text(FAKE_CAMERA, encoding="utf-8")
        self.messages = []
        self.exits = []
        self.got_all = threading.Event()

    def on_message(self, message):
        self.messages.append(message)
        if message == GestureLine(None, 0.0):
            self.got_all.set()

    def make(self, script=None):
        worker = GestureWorker(self.on_message, on_exit=self.exits.append,
                               command=[sys.executable, "-I", str(script or self.script)])
        self.addCleanup(worker.stop)
        return worker

    def wait_for(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def test_only_whitelisted_lines_get_through(self):
        worker = self.make()
        worker.start()
        self.assertTrue(self.got_all.wait(15))
        self.assertEqual(self.messages, [
            StatusLine("ready"), GestureLine("palma", 0.91), GestureLine("pulgar_abajo", 0.85),
            GestureLine(None, 0.0),
        ])
        worker.stop()
        self.assertFalse(worker.running)
        self.assertEqual(worker.ignored, 6)  # texto, imagen, puntos, clave de más, pulgar arriba, binario
        self.assertEqual(self.exits, [])  # se paró a propósito: no es una salida inesperada

    def test_stop_ends_the_process_and_start_again_works(self):
        worker = self.make()
        worker.start()
        self.assertTrue(self.got_all.wait(15))
        proc = worker._proc
        worker.stop()
        self.assertIsNotNone(proc.poll())
        self.got_all.clear()
        worker.start()
        self.assertTrue(self.got_all.wait(15))
        worker.stop()
        self.assertEqual(self.exits, [])

    def test_a_process_that_ignores_stdin_and_sigterm_is_killed(self):
        stubborn = self.script.with_name("terca.py")
        stubborn.write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                            "print('{\"status\": \"ready\"}', flush=True)\ntime.sleep(60)\n")
        worker = self.make(stubborn)
        worker.start()
        self.assertTrue(self.wait_for(lambda: self.messages))
        proc = worker._proc
        worker.stop()
        self.assertIsNotNone(proc.poll())

    @unittest.skipUnless(shutil.which("bwrap"), "sin bwrap")
    def test_started_from_a_thread_that_ends_the_process_survives(self):
        """Como el botón del Orb: lo enciende el hilo de la petición web, que termina enseguida.

        `bwrap --die-with-parent` mata al hijo cuando muere el HILO que lo lanzó (PR_SET_PDEATHSIG), no el proceso:
        así la cámara se apagaba a los 7 ms (código -9) cada vez que se encendía desde el Orb.
        """
        worker = GestureWorker(self.on_message, on_exit=self.exits.append, command=[
            shutil.which("bwrap"), "--ro-bind", "/", "/", "--dev", "/dev", "--die-with-parent", "--",
            sys.executable, "-I", "-c", "import sys, time\ntime.sleep(0.5)\n"
            "print('{\"status\": \"ready\"}', flush=True)\nsys.stdin.read()\n"])
        self.addCleanup(worker.stop)
        def like_the_orb_request():
            worker.start()
            time.sleep(0.3)  # contestar la petición: el hilo muere cuando bwrap ya ha activado --die-with-parent

        starter = threading.Thread(target=like_the_orb_request)
        starter.start()
        starter.join()
        self.assertTrue(self.wait_for(lambda: self.messages or self.exits, timeout=10))
        self.assertEqual(self.exits, [])
        self.assertEqual(self.messages, [StatusLine("ready")])

    def test_an_unexpected_exit_is_reported(self):
        quits = self.script.with_name("se_va.py")
        quits.write_text('print(\'{"status": "no-detector"}\', flush=True)\nraise SystemExit(3)\n')
        worker = self.make(quits)
        worker.start()
        self.assertTrue(self.wait_for(lambda: self.exits))
        self.assertEqual(self.messages, [StatusLine("no-detector")])
        self.assertEqual(self.exits, [3])


class GesturesConfigTest(unittest.TestCase):
    def test_defaults_keep_the_camera_disabled(self):
        cfg = config_from_dict({}).gestures
        self.assertFalse(cfg.enabled)
        self.assertEqual((cfg.idle_minutes, cfg.hold_seconds, cfg.fps, cfg.min_score), (5, 1.0, 8, 0.8))

    def test_strict_values(self):
        ok = config_from_dict({"gestures": {"enabled": True, "idle_minutes": 10, "hold_seconds": 2,
                                            "fps": 5, "min_score": 0.9}}).gestures
        self.assertEqual((ok.enabled, ok.hold_seconds), (True, 2.0))
        for bad in ({"idle_minutes": 0}, {"idle_minutes": 61}, {"hold_seconds": 0.1}, {"fps": 30},
                    {"min_score": 0.2}, {"min_score": 1.1}, {"enabled": "sí"}, {"enabled": 1},
                    {"camera": "/dev/video1"}, {"model_path": "/tmp/x"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                config_from_dict({"gestures": bad})


if __name__ == "__main__":
    unittest.main()
