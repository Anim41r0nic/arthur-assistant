"""El aparato Windows (fase A, docs/windows.md), probado en Linux y SIN tocar nada real.

En la nube no hay Windows: winmm (micro), winsound (altavoz), PowerShell (notificaciones), SendInput (teclas
multimedia) y el menú Inicio son todos falsos. Lo que se comprueba es NUESTRA lógica: que los búferes del micro se
lean en orden, que la voz se pueda cortar, que el texto de una confirmación nunca entre en el código de PowerShell,
que solo se abran accesos directos de la lista, que el buzón exija la llave…
"""

import base64
import ctypes
import dataclasses
import io
import json
import os
import re
import socket
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

from arthur.api.control import (
    ControlServer,
    ControlUnavailable,
    KeyedControlServer,
    make_control_server,
    send_command,
)
from arthur.integrations import win_escritorio as win
from arthur.tools.base import Risk, ToolDenied, ToolError
from arthur.voice import win_audio
from arthur.voice.recorder import CHUNK_BYTES, SAMPLE_RATE, Recorder, RecorderError

ROOT = Path(__file__).resolve().parents[1]
IS_64 = ctypes.sizeof(ctypes.c_void_p) == 8


def tone(seconds, amplitude=12000):
    n = int(seconds * SAMPLE_RATE)
    return struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2)))


def wav(seconds=0.2, rate=22050, amplitude=8000, channels=1):
    out = io.BytesIO()
    n = int(seconds * rate)
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        samples = ([amplitude, -amplitude] * (n * channels // 2 + 1))[:n * channels]
        w.writeframes(struct.pack(f"<{n * channels}h", *samples))
    return out.getvalue()


# --- micrófono (waveIn) ------------------------------------------------------------------------------------------

class FakeWinmm:
    """Un winmm de mentira: guarda los búferes que le dan y los «llena» cuando el test se lo pide."""

    def __init__(self, open_code=0, start_code=0):
        self.open_code, self.start_code = open_code, start_code
        self.queue: list = []   # cabeceras esperando audio, en el orden en que se las dieron
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def waveInOpen(self, handle, device, fmt, callback, instance, flags):
        self.calls.append("open")
        f = fmt._obj
        self.format = (f.wFormatTag, f.nChannels, f.nSamplesPerSec, f.wBitsPerSample, f.nBlockAlign)
        self.device = device
        handle._obj.value = 1234
        return self.open_code

    def waveInPrepareHeader(self, handle, header, size):
        self.calls.append("prepare")
        return 0

    def waveInAddBuffer(self, handle, header, size):
        with self.lock:
            self.queue.append(header._obj)
        return 0

    def waveInStart(self, handle):
        self.calls.append("start")
        return self.start_code

    def waveInReset(self, handle):
        # Como el de verdad: tras el reset ya no escribe en ningún búfer (después se liberan).
        self.calls.append("reset")
        with self.lock:
            self.queue.clear()
        return 0

    def waveInUnprepareHeader(self, handle, header, size):
        self.calls.append("unprepare")
        return 0

    def waveInClose(self, handle):
        self.calls.append("close")
        return 0

    def waveInStop(self, handle):
        return 0

    def fill(self, pcm: bytes) -> None:
        """Como el driver: llena el siguiente búfer de la cola y lo marca como hecho."""
        while pcm:
            with self.lock:
                if not self.queue:
                    return
                header = self.queue.pop(0)
            piece, pcm = pcm[:header.dwBufferLength], pcm[header.dwBufferLength:]
            ctypes.memmove(header.lpData, piece, len(piece))
            header.dwBytesRecorded = len(piece)
            header.dwFlags |= win_audio.WHDR_DONE


class StructSizesTest(unittest.TestCase):
    """Si estas estructuras no miden lo mismo que en Windows, winmm o SendInput escribirían fuera de sitio."""

    def test_waveformatex_is_packed(self):
        self.assertEqual(ctypes.sizeof(win_audio.WAVEFORMATEX), 18)

    @unittest.skipUnless(IS_64, "los tamaños de Windows de 64 bits")
    def test_sizes_match_64_bit_windows(self):
        self.assertEqual(ctypes.sizeof(win_audio.WAVEHDR), 48)
        self.assertEqual(ctypes.sizeof(win.INPUT), 40)
        self.assertEqual(ctypes.sizeof(win.KEYBDINPUT), 24)


class WaveInSourceTest(unittest.TestCase):
    def test_asks_for_16_khz_mono_16_bit_from_the_default_mic(self):
        fake = FakeWinmm()
        source = win_audio.WaveInSource(winmm=fake, poll=0.001)
        self.assertEqual(fake.format, (win_audio.WAVE_FORMAT_PCM, 1, 16000, 16, 2))
        self.assertEqual(fake.device, win_audio.WAVE_MAPPER)
        self.assertEqual(len(fake.queue), win_audio.BUFFERS)  # todos los búferes, ya en manos de winmm
        self.assertIn("start", fake.calls)
        source.close()

    def test_reads_the_audio_in_order_and_gives_the_buffers_back(self):
        fake = FakeWinmm()
        source = win_audio.WaveInSource(winmm=fake, poll=0.001)
        pcm = bytes(range(256)) * 100  # 25 600 bytes: más que todos los búferes juntos

        def feed():
            for i in range(0, len(pcm), 3200):
                fake.fill(pcm[i:i + 3200])
                time.sleep(0.005)
        feeder = threading.Thread(target=feed)
        feeder.start()
        got = b""
        while len(got) < len(pcm):
            got += source.read(1600)
        feeder.join()
        self.assertEqual(got, pcm)
        source.close()

    def test_close_releases_the_mic_and_ends_reading(self):
        fake = FakeWinmm()
        source = win_audio.WaveInSource(winmm=fake, poll=0.001)
        result = []
        reader = threading.Thread(target=lambda: result.append(source.read(CHUNK_BYTES)))
        reader.start()
        time.sleep(0.02)
        source.close()
        reader.join(2)
        self.assertEqual(result, [b""])  # como pw-record al terminar: fin de los datos
        self.assertEqual(fake.calls[-1], "close")
        self.assertEqual(fake.calls.count("unprepare"), win_audio.BUFFERS)
        source.close()  # cerrar dos veces no hace nada
        self.assertEqual(fake.calls.count("close"), 1)

    def test_without_a_mic_it_is_an_oserror_and_nothing_stays_open(self):
        fake = FakeWinmm(open_code=2)
        with self.assertRaises(OSError) as e:
            win_audio.WaveInSource(winmm=fake)
        self.assertIn("Privacidad", str(e.exception))
        self.assertNotIn("close", fake.calls)  # no llegó a abrirse

    def test_if_it_cannot_start_it_closes_what_it_opened(self):
        fake = FakeWinmm(start_code=4)
        with self.assertRaises(OSError):
            win_audio.WaveInSource(winmm=fake)
        self.assertEqual(fake.calls[-1], "close")


class RecorderOnWindowsTest(unittest.TestCase):
    def test_the_same_voice_and_silence_logic_works_with_wavein(self):
        fake = FakeWinmm()
        levels = []
        recorder = Recorder(source=lambda: win_audio.WaveInSource(winmm=fake, poll=0.001), on_level=levels.append)
        recorder.start()
        audio = b"\x00\x00" * int(SAMPLE_RATE * 0.4) + tone(0.6) + b"\x00\x00" * int(SAMPLE_RATE * 1.6)

        def feed():
            for i in range(0, len(audio), 3200):
                fake.fill(audio[i:i + 3200])
                time.sleep(0.1)  # a ritmo real: 100 ms por búfer
        threading.Thread(target=feed, daemon=True).start()
        self.assertTrue(recorder.wait_for_end(silence=0.5, no_speech=3, max_seconds=5))
        pcm = recorder.stop()
        self.assertGreater(len(pcm), SAMPLE_RATE)  # más de medio segundo grabado
        self.assertEqual(levels[-1], 0.0)          # el Orb deja de latir al cerrar
        self.assertIn("close", fake.calls)
        self.assertFalse(recorder.recording)

    def test_a_mic_error_is_a_recorder_error(self):
        recorder = Recorder(source=lambda: win_audio.WaveInSource(winmm=FakeWinmm(open_code=2)))
        with self.assertRaises(RecorderError):
            recorder.start()

    def test_the_system_picks_the_source(self):
        from arthur.voice import recorder as module

        with mock.patch.object(module.sys, "platform", "win32"):
            self.assertIsNotNone(module.default_source_factory())  # waveIn (no se llega a abrir)
        with mock.patch.object(module.sys, "platform", "linux"):
            self.assertIsNone(module.default_source_factory())     # pw-record de siempre
            self.assertEqual(Recorder().command[0], "pw-record")


# --- altavoz (winsound) ------------------------------------------------------------------------------------------

class FakeSpeaker:
    """Un winsound de mentira: «suena» lo que dura el WAV, salvo que lo corten."""

    def __init__(self, fail=False):
        self.played: list[bytes] = []
        self.stops = 0
        self.cut = threading.Event()
        self.fail = fail

    def play(self, data):
        if self.fail:
            raise RuntimeError("Failed to play sound")
        self.played.append(data)
        with wave.open(io.BytesIO(data), "rb") as w:
            seconds = w.getnframes() / w.getframerate()
        self.cut.clear()
        self.cut.wait(seconds)

    def stop(self):
        self.stops += 1
        self.cut.set()


class WinSoundPlayerTest(unittest.TestCase):
    def test_plays_from_memory_and_reports_the_level(self):
        speaker, levels = FakeSpeaker(), []
        player = win_audio.WinSoundPlayer(backend=speaker, on_level=levels.append)
        started = time.monotonic()
        player.play(wav(0.3))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)  # bloquea mientras suena, como pw-play
        self.assertEqual(len(speaker.played), 1)
        self.assertGreater(max(levels), 0.1)
        self.assertEqual(levels[-1], 0.0)
        self.assertFalse(player.playing)

    def test_can_be_cut_in_the_middle(self):
        speaker = FakeSpeaker()
        player = win_audio.WinSoundPlayer(backend=speaker)
        thread = threading.Thread(target=player.play, args=(wav(10),))
        thread.start()
        deadline = time.monotonic() + 2
        while not player.playing and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(player.playing)
        started = time.monotonic()
        player.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        self.assertGreaterEqual(speaker.stops, 1)
        self.assertFalse(player.playing)

    def test_the_volume_is_applied_to_the_samples(self):
        speaker = FakeSpeaker()
        win_audio.WinSoundPlayer(backend=speaker, volume=0.5).play(wav(0.05, amplitude=8000))
        with wave.open(io.BytesIO(speaker.played[0]), "rb") as w:
            first = struct.unpack("<h", w.readframes(1))[0]
        self.assertEqual(first, 4000)

    def test_rejects_unexpected_formats(self):
        with self.assertRaises(ValueError):
            win_audio.WinSoundPlayer(backend=FakeSpeaker()).play(wav(0.05, channels=2))

    def test_a_speaker_failure_does_not_break_the_turn(self):
        player = win_audio.WinSoundPlayer(backend=FakeSpeaker(fail=True))
        with self.assertLogs("arthur.voice.win_audio", "WARNING"):
            player.play(wav(0.05))
        self.assertFalse(player.playing)

    def test_remote_voice_can_use_it(self):
        from arthur.interfaces.voz_cliente import make_player

        self.assertIsInstance(make_player(windows=True), win_audio.WinSoundPlayer)
        self.assertNotIsInstance(make_player(windows=False), win_audio.WinSoundPlayer)


# --- confirmaciones y avisos (PowerShell) ---------------------------------------------------------------------------

class FakePowerShell:
    def __init__(self, out="no", code=0, error=None):
        self.out, self.code, self.error = out, code, error
        self.calls = []

    def __call__(self, args, stdin, timeout):
        self.calls.append((args, stdin, timeout))
        if self.error:
            raise self.error
        return mock.Mock(returncode=self.code, stdout=self.out + "\r\n")

    def script(self, n=0) -> str:
        args = self.calls[n][0]
        return base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")

    def texts(self, n=0) -> list[str]:
        return [base64.b64decode(line).decode("utf-8") for line in self.calls[n][1].splitlines()]


class WindowsConfirmTest(unittest.TestCase):
    NASTY = "Abrir «x»'); Remove-Item C:\\ -Recurse; ('<script>&\"\n$env:USERNAME"

    def test_only_the_yes_button_confirms(self):
        self.assertTrue(win.windows_confirm("abrir firefox", run=FakePowerShell("si")))
        for out, code in (("no", 0), ("", 0), ("si", 1), ("sí", 0), ("SI de verdad", 0)):
            self.assertFalse(win.windows_confirm("x", run=FakePowerShell(out, code)), (out, code))

    def test_any_failure_counts_as_no(self):
        import subprocess

        self.assertFalse(win.windows_confirm("x", run=FakePowerShell(error=FileNotFoundError("powershell.exe"))))
        self.assertFalse(win.windows_confirm("x", run=FakePowerShell(error=subprocess.TimeoutExpired("ps", 1))))

    def test_the_text_never_goes_into_the_code(self):
        ps = FakePowerShell("si")
        win.windows_confirm(self.NASTY, timeout=30, run=ps)
        script = ps.script()
        self.assertEqual(script, win.confirm_script(30))  # siempre la plantilla fija, sea cual sea el texto
        for piece in ("Remove-Item", "<script>", "USERNAME"):
            self.assertNotIn(piece, script)
        title, text = ps.texts()
        self.assertEqual(title, "ARTHUR · ¿Confirmas?")
        self.assertIn("Remove-Item", text)  # llega como DATOS, por la entrada estándar
        self.assertNotIn("\n", text)        # sin saltos de línea que partan la entrada
        self.assertTrue(ps.calls[0][1].isascii())
        args = ps.calls[0][0]
        self.assertEqual(args[0], "powershell.exe")
        self.assertTrue(all(self.NASTY not in a for a in args))

    def test_the_script_puts_the_text_in_as_text_nodes(self):
        for script in (win.NOTIFY_SCRIPT, win.CONFIRM_SCRIPT):
            self.assertIn("CreateTextNode($texto)", script)
            self.assertNotIn("$texto +", script)
            self.assertNotIn("Invoke-Expression", script)
            self.assertTrue(script.isascii())
        self.assertIn("-Timeout $Segundos", win.CONFIRM_SCRIPT)
        self.assertIn('arguments="si"', win.CONFIRM_SCRIPT)

    def test_the_summary_is_cut(self):
        ps = FakePowerShell()
        win.windows_confirm("a" * 1000, run=ps)
        self.assertEqual(len(ps.texts()[1]), win.MAX_SUMMARY)

    def test_notifications_use_the_fixed_script_too(self):
        ps = FakePowerShell()
        win.windows_notify(self.NASTY, run=ps)
        self.assertEqual(ps.script(), win.NOTIFY_SCRIPT)
        self.assertIn("Remove-Item", ps.texts()[1])
        with self.assertLogs("arthur.integrations.win_escritorio", "WARNING"):
            win.windows_notify("x", run=FakePowerShell(error=OSError("no hay PowerShell")))

    def test_the_client_service_picks_them_on_windows(self):
        from arthur.interfaces import voz_cliente

        with mock.patch.object(win, "_run_powershell", FakePowerShell("si")):
            self.assertTrue(voz_cliente.default_confirm(windows=True)("abrir firefox"))
        self.assertIs(voz_cliente.default_confirm(windows=False), voz_cliente.notify_confirm)
        self.assertIs(voz_cliente.default_notify(windows=True), win.windows_notify)


# --- teclas multimedia ---------------------------------------------------------------------------------------------

class FakeKeyboard:
    def __init__(self, accept=True):
        self.accept = accept
        self.keys: list[tuple[int, bool]] = []

    def __call__(self, events):
        for e in events:
            assert e.type == win.INPUT_KEYBOARD and e.u.ki.dwFlags & win.KEYEVENTF_EXTENDEDKEY
            self.keys.append((e.u.ki.wVk, bool(e.u.ki.dwFlags & win.KEYEVENTF_KEYUP)))
        return len(events) if self.accept else 0

    def presses(self) -> list[int]:
        return [vk for vk, up in self.keys if not up]


class MediaKeysTest(unittest.TestCase):
    def test_press_and_release(self):
        kb = FakeKeyboard()
        win.MediaKeys(kb).press(win.VK_MEDIA_PLAY_PAUSE)
        self.assertEqual(kb.keys, [(win.VK_MEDIA_PLAY_PAUSE, False), (win.VK_MEDIA_PLAY_PAUSE, True)])

    def test_only_media_keys(self):
        for vk in (0x41, 0x5B, 0x0D, 0x2E):  # A, Windows, Enter, Supr
            with self.assertRaises(win.MediaKeysError):
                win.MediaKeys(FakeKeyboard()).press(vk)

    def test_blocked_by_windows_is_an_error(self):
        with self.assertRaises(win.MediaKeysError):
            win.MediaKeys(FakeKeyboard(accept=False)).press(win.VK_VOLUME_UP)

    def test_volume_goes_to_zero_and_up_in_steps_of_2(self):
        kb = FakeKeyboard()
        win.MediaKeys(kb).set_volume(30)
        presses = kb.presses()
        self.assertEqual(presses.count(win.VK_VOLUME_DOWN), 50)
        self.assertEqual(presses.count(win.VK_VOLUME_UP), 15)
        self.assertEqual(presses[:50], [win.VK_VOLUME_DOWN] * 50)  # primero a 0


# --- apps del menú Inicio y herramientas ----------------------------------------------------------------------------

class StartMenuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.user, self.common = base / "usuario", base / "todos"
        for path in ("usuario/Firefox.lnk", "usuario/Spotify.lnk", "todos/Google Chrome.lnk",
                     "todos/Firefox.lnk", "todos/Accesorios/Bloc de notas.lnk", "todos/Accesorios/leeme.txt",
                     "todos/VLC/Uninstall VLC.lnk", "todos/VLC/VLC media player.lnk",
                     "todos/Herramientas de Windows/Editor del Registro.lnk", "todos/Código.lnk"):
            (base / path).parent.mkdir(parents=True, exist_ok=True)
            (base / path).write_bytes(b"L\x00\x00\x00")  # el contenido da igual: nunca se lee
        (self.user / "Fuera.lnk").symlink_to(base / "todos" / "Firefox.lnk")
        self.catalog = win.StartMenuCatalog([self.user, self.common])
        self.kb, self.opened = FakeKeyboard(), []

    def tearDown(self):
        self.tmp.cleanup()

    def tools(self, trusted=("firefox",), keys=None, launch=None):
        from arthur.tools.win_aparato import build_windows_tools

        return {t.name: t for t in build_windows_tools(self.catalog, keys or win.MediaKeys(self.kb), trusted,
                                                         launch=launch or self.opened.append)}

    def test_the_list_is_closed_and_safe(self):
        ids = [a.id for a in self.catalog.apps()]
        self.assertEqual(ids, ["bloc-de-notas", "codigo", "firefox", "google-chrome", "spotify", "vlc-media-player"])
        self.assertEqual(self.catalog.find("Firefox").path, self.user / "Firefox.lnk")  # la del usuario manda

    def test_blocked_folders_count_at_any_depth_and_startup_is_out(self):
        base = Path(self.tmp.name)
        for path in ("todos/Administrative Tools/Avanzado/Registry Editor.lnk",
                     "usuario/Startup/ARTHUR (aparato).lnk", "usuario/Startup/Programa al arrancar.lnk"):
            (base / path).parent.mkdir(parents=True, exist_ok=True)
            (base / path).write_bytes(b"L\x00\x00\x00")
        ids = [a.id for a in self.catalog.apps()]
        self.assertNotIn("registry-editor", ids)  # dentro de una carpeta bloqueada, aunque sea en una subcarpeta
        self.assertNotIn("arthur-aparato", ids)   # la carpeta Inicio: lo que arranca solo no se abre por voz
        self.assertNotIn("programa-al-arrancar", ids)

    def test_find(self):
        self.assertEqual(self.catalog.find("chrome").id, "google-chrome")  # una sola que lo contiene
        self.assertEqual(self.catalog.find("Código").id, "codigo")
        self.assertIsNone(self.catalog.find("o"))                          # varias: no se adivina
        self.assertIsNone(self.catalog.find("Uninstall VLC"))
        self.assertIsNone(self.catalog.find("..\\..\\Windows\\System32\\cmd.exe"))
        self.assertIsNone(self.catalog.find(""))

    def test_open_only_launches_shortcuts_from_the_list(self):
        tool = self.tools()["open_application"]
        args = tool.validate({"name": "spotify"})
        tool.check(args)
        self.assertEqual(tool.run(args), "He abierto Spotify")
        self.assertEqual(self.opened, [self.user / "Spotify.lnk"])
        for name in ("cmd.exe", "C:\\Windows\\System32\\cmd.exe", "spotify & calc", "Uninstall VLC"):
            with self.assertRaises(ToolDenied):
                tool.check(tool.validate({"name": name}))

    def test_trusted_apps_are_low_risk(self):
        tool = self.tools()["open_application"]
        self.assertIs(tool.effective_risk({"name": "firefox"}), Risk.LOW)
        self.assertIs(tool.effective_risk({"name": "spotify"}), Risk.WRITE)
        self.assertIs(tool.effective_risk({"name": "no-existe"}), Risk.WRITE)

    def test_a_trusted_name_never_opens_a_different_app_without_asking(self):
        """El servidor se salta la confirmación si el NOMBRE pedido está en trusted_apps (low_if, comparación
        exacta). Aquí, la búsqueda aproximada podría convertir ese nombre en OTRA app: eso no puede pasar."""
        tool = self.tools(trusted=("chrome",))["open_application"]  # solo existe «google-chrome»
        with self.assertRaises(ToolDenied):
            tool.check(tool.validate({"name": "chrome"}))
        self.assertEqual(self.opened, [])
        tool = self.tools(trusted=("google-chrome",))["open_application"]
        tool.check(tool.validate({"name": "google-chrome"}))  # el nombre exacto de la lista, sí
        tool.check(tool.validate({"name": "chrome"}))         # sin confianza: el servidor pregunta antes

    def test_a_launch_failure_is_explained(self):
        def boom(path):
            raise OSError("no se encuentra")
        with self.assertRaises(ToolError):
            self.tools(launch=boom)["open_application"].run({"name": "spotify"})

    def test_list_applications(self):
        tool = self.tools()["list_applications"]
        self.assertIn("Google Chrome [google-chrome]", tool.run(tool.validate({})))
        self.assertEqual(tool.run(tool.validate({"query": "chrome"})), "Google Chrome [google-chrome]")

    def test_media_and_volume_press_the_right_keys(self):
        tools = self.tools()
        media = tools["media"]
        for action, vk in (("pause", win.VK_MEDIA_PLAY_PAUSE), ("next", win.VK_MEDIA_NEXT_TRACK),
                           ("previous", win.VK_MEDIA_PREV_TRACK)):
            self.kb.keys.clear()
            args = media.validate({"action": action})
            media.check(args)
            media.run(args)
            self.assertEqual(self.kb.presses(), [vk])
        with self.assertRaises(ToolDenied):
            media.check(media.validate({"action": "shutdown"}))
        volume = tools["volume"]
        self.kb.keys.clear()
        self.assertIn("40 %", volume.run(volume.validate({"percent": 40})))
        self.assertEqual(self.kb.presses().count(win.VK_VOLUME_UP), 20)
        self.kb.keys.clear()
        volume.run(volume.validate({"muted": True}))
        self.assertEqual(self.kb.presses(), [win.VK_VOLUME_MUTE])
        self.kb.keys.clear()
        self.assertIn("no puedo leer", volume.run(volume.validate({})))  # honesto: no se inventa el volumen
        self.assertEqual(self.kb.keys, [])

    def test_blocked_keys_are_a_tool_error(self):
        tools = self.tools(keys=win.MediaKeys(FakeKeyboard(accept=False)))
        with self.assertRaises(ToolError):
            tools["media"].run({"action": "next"})

    def registry(self):
        from arthur.core.config import SecurityConfig
        from arthur.tools import build_registry

        return build_registry(SecurityConfig(), profile="windows", windows_catalog=self.catalog,
                              media_keys=win.MediaKeys(self.kb))

    def test_the_windows_profile(self):
        from arthur.interfaces.core import capability_specs

        registry = self.registry()
        self.assertEqual(sorted(t.name for t in registry.all()),
                         ["list_applications", "media", "open_application", "volume"])
        self.assertFalse(any(t.risk is Risk.DANGEROUS for t in registry.all()))
        specs = {s["name"]: s for s in capability_specs(registry, ("firefox",))}
        self.assertEqual(specs["open_application"]["low_if"], {"param": "name", "values": ["firefox"]})

    def test_the_server_accepts_them(self):
        """El servidor da de alta las herramientas del aparato con sus propias reglas (core/remote_tools.py)."""
        from arthur.core.remote_tools import build_remote_tool
        from arthur.interfaces.core import capability_specs

        for spec in capability_specs(self.registry()):
            build_remote_tool(spec, lambda args: "ok")


# --- el buzón de control con llave ------------------------------------------------------------------------------------

class KeyedControlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "arthur" / "control.key"

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self):
        server = make_control_server({"status": lambda _: "ok ARTHUR", "ask": lambda t: f"eco: {t}"}, self.path)
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_the_path_picks_the_mailbox(self):
        self.assertIsInstance(self.serve(), KeyedControlServer)
        unix = make_control_server({}, Path(self.tmp.name) / "control.sock")
        self.addCleanup(unix.stop)
        self.assertIsInstance(unix, ControlServer)

    def test_orders_with_the_key(self):
        self.serve()
        self.assertEqual(send_command("status", self.path), "ok ARTHUR")
        self.assertEqual(send_command("ask hola qué tal", self.path), "eco: hola qué tal")
        self.assertEqual(send_command("borrar todo", self.path), "error: orden desconocida")

    def test_only_on_localhost_and_without_the_key_nothing(self):
        server = self.serve()
        info = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(info["port"], server.port)
        self.assertGreaterEqual(len(info["key"]), 40)
        self.assertEqual(server._sock.getsockname()[0], "127.0.0.1")
        for attempt in (b"status", b"\nstatus", b"una-llave-falsa\nstatus", info["key"][:-1].encode() + b"\nstatus"):
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as s:
                s.sendall(attempt)
                s.shutdown(socket.SHUT_WR)
                self.assertEqual(s.recv(1000).strip(), b"error: no autorizado")

    def test_a_new_key_each_start(self):
        first = self.serve()
        key = json.loads(self.path.read_text())["key"]
        first.stop()
        self.assertFalse(self.path.exists())
        self.serve()
        self.assertNotEqual(json.loads(self.path.read_text())["key"], key)

    def test_only_one_arthur(self):
        self.serve()
        with self.assertRaises(ControlUnavailable):
            make_control_server({"status": lambda _: "ok"}, self.path)

    def test_leftovers_of_a_crash_do_not_block(self):
        self.path.parent.mkdir(parents=True)
        with socket.socket() as s:  # un puerto en el que ya no escucha nadie
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        self.path.write_text(json.dumps({"port": dead, "key": "vieja"}))
        self.serve()
        self.assertEqual(send_command("status", self.path), "ok ARTHUR")

    def test_without_service_it_says_so(self):
        with self.assertRaises(ControlUnavailable):
            send_command("status", self.path)
        self.path.parent.mkdir(parents=True)
        for bad in ("no es json", json.dumps({"port": "80", "key": "x"}), json.dumps({"port": 0, "key": "x"})):
            self.path.write_text(bad)
            with self.assertRaises(ControlUnavailable):
                send_command("status", self.path)

    def test_default_path_on_windows(self):
        from arthur.api import control

        with mock.patch.object(control.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name}):
            self.assertEqual(control.default_socket_path(), Path(self.tmp.name) / "arthur" / "control.key")


class WindowsFoldersTest(unittest.TestCase):
    def test_appdata_and_localappdata(self):
        from arthur.core import config

        roaming, local = "C:/Usuarios/u/AppData/Roaming", "C:/Usuarios/u/AppData/Local"
        with mock.patch.object(config.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"APPDATA": roaming, "LOCALAPPDATA": local}):
            for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
                os.environ.pop(var, None)  # patch.dict lo deja todo como estaba al salir
            self.assertEqual(config.default_config_path(), Path(roaming) / "arthur" / "config.toml")
            self.assertEqual(config.state_dir(), Path(local) / "arthur")
            self.assertEqual(config.data_dir(), Path(local) / "arthur" / "datos")


# --- el servicio entero, en modo Windows -----------------------------------------------------------------------------

class WindowsClientServiceTest(unittest.TestCase):
    def test_runs_with_the_keyed_mailbox_and_without_hyprland(self):
        from arthur.core.config import Config, OrbConfig, WakeConfig
        from arthur.interfaces.voz_cliente import RemoteVoice, run_client_service

        class Core:
            url = "http://servidor:8770"

            def __init__(self):
                self.sent = []

            def whoami(self):
                return {"device": "pc-windows", "name": "ARTHUR"}

            def ask(self, text):
                return f"eco: {text}"

            def events(self):
                return iter(())

            def voice(self, wav_bytes):
                self.sent.append(wav_bytes)
                return {"heard": "hola", "reply": "Hola."}

            def speech(self, text):
                return wav(0.05)

        class Mic:
            recording = truncated = False

            def start(self, **_):
                self.recording = True

            def stop(self):
                self.recording = False
                return tone(1)

        core, speaker = Core(), FakeSpeaker()
        voice = RemoteVoice(core, recorder=Mic(), player=win_audio.WinSoundPlayer(backend=speaker),
                            say=lambda _: None)
        config = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), enabled=True),
                                     orb=dataclasses.replace(OrbConfig(), port=0, fullscreen_on_wake=True))

        def no_window(url):
            raise AssertionError("En Windows no hay Orb a pantalla completa (es de Hyprland)")

        stop, notes, result = threading.Event(), [], []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.key"
            thread = threading.Thread(target=lambda: result.append(run_client_service(
                core, config, control_path=path, stop=stop, notify=notes.append, voice=voice,
                ask_confirm=lambda s: False, orb_window_factory=no_window, windows=True)))
            thread.start()
            deadline = time.monotonic() + 3
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(send_command("status", path).startswith("ok ARTHUR cliente"))
            self.assertEqual(send_command("ask hola", path), "eco: hola")
            self.assertRegex(send_command("orb-url", path), r"^http://127\.0\.0\.1:\d+/\?t=")
            self.assertEqual(send_command("toggle-listen", path), "escuchando")
            self.assertEqual(send_command("toggle-listen", path), "procesando")
            deadline = time.monotonic() + 3
            while not speaker.played and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(core.sent), 1)
            self.assertEqual(len(speaker.played), 1)  # la respuesta ha «sonado» por el winsound falso
            stop.set()
            thread.join(5)
        self.assertEqual(result, [0])


# --- los instaladores de PowerShell ----------------------------------------------------------------------------------

class PowerShellScriptsTest(unittest.TestCase):
    SCRIPTS = (ROOT / "scripts" / "instalar-servidor.ps1", ROOT / "scripts" / "windows" / "instalar-aparato.ps1")

    def test_ascii_utf8_without_bom(self):
        """Windows PowerShell 5.1 lee un .ps1 sin BOM como ANSI: con solo ASCII da igual, y sin BOM no molesta."""
        for script in self.SCRIPTS:
            data = script.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), script.name)
            self.assertTrue(data.isascii(), script.name)

    def test_the_device_installer_never_asks_for_admin(self):
        text = (ROOT / "scripts" / "windows" / "instalar-aparato.ps1").read_text()
        for needed in ("-RunLevel Limited", "-LogonType Interactive", "-AtLogOn", "(recomendado)",
                       "-ForegroundColor Green", "--servicio-cliente"):
            self.assertIn(needed, text)
        for forbidden in ("Highest", "RunAs", "Invoke-Expression", "iex ", "Invoke-WebRequest", "DownloadString",
                          "Set-ExecutionPolicy"):
            self.assertNotIn(forbidden, text)
        self.assertTrue((ROOT / "scripts" / "windows" / "arthur-hablar.pyw").is_file())

    def test_no_assignment_to_automatic_variables(self):
        """Lo que PSScriptAnalyzer llama PSAvoidAssignmentToAutomaticVariable: $args, $input… son de PowerShell."""
        for script in self.SCRIPTS:
            text = script.read_text()
            for name in ("args", "input", "host", "error", "matches", "home", "pid", "profile", "event"):
                self.assertIsNone(re.search(rf"(?im)^\s*\${name}\s*=", text), (script.name, name))


if __name__ == "__main__":
    unittest.main()
