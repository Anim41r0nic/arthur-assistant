"""La voz de un aparato que habla con el ARTHUR del servidor (fase S4, paso 4).

Todo falso: el micrófono es un objeto que devuelve un tono, el servidor es un RemoteCore de mentira y el
«altavoz» es un programa que guarda lo que le llega en un fichero. Nada debe sonar ni abrir el micro de verdad.
"""

import http.client
import io
import json
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import wave
from pathlib import Path

from arthur.interfaces.core import RemoteError, run_remote_client
from arthur.interfaces.voz_cliente import RemoteVoice, WavPlayer, wav_bytes
from arthur.voice.recorder import SAMPLE_RATE, RecorderError


def tone(seconds, amplitude=12000):
    n = int(seconds * SAMPLE_RATE)
    return struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2)))


def reply_wav(frames=10, rate=22050):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x07\x00" * frames)
    return out.getvalue()


class FakeRecorder:
    def __init__(self, audio=None, fail=False):
        self.audio = tone(1) if audio is None else audio
        self.fail = fail
        self.recording = False
        self.truncated = False

    def start(self, **_):
        if self.fail:
            raise RecorderError("No encuentro el micrófono")
        self.recording = True

    def stop(self):
        self.recording = False
        return self.audio


class FakeCore:
    url = "http://100.64.1.2:8770"

    def __init__(self, heard="¿qué hora es?", reply="Son las cinco.", fail=None):
        self.heard, self.reply, self.fail = heard, reply, fail
        self.sent: list[bytes] = []
        self.spoken: list[str] = []

    def whoami(self):
        return {"device": "cachyos", "name": "ARTHUR"}

    def voice(self, wav):
        if self.fail:
            raise RemoteError(self.fail)
        self.sent.append(wav)
        return {"heard": self.heard, "reply": self.reply if self.heard else None}

    def speech(self, text):
        self.spoken.append(text)
        return reply_wav()


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "sonado.raw"

    def tearDown(self):
        self.dir.cleanup()

    def fake_player(self, delay=0.0):
        code = (f"import sys, time; data = sys.stdin.buffer.read(); time.sleep({delay}); "
                f"open({str(self.out)!r}, 'wb').write(data)")
        return WavPlayer(volume=0.5, command=lambda rate, volume: [sys.executable, "-I", "-c", code, str(rate),
                                                                   str(volume)])

    def test_plays_the_audio_of_the_wav(self):
        self.fake_player().play(reply_wav(frames=4))
        self.assertEqual(self.out.read_bytes(), b"\x07\x00" * 4)

    def test_can_be_cut_in_the_middle(self):
        player = self.fake_player(delay=10)
        thread = threading.Thread(target=player.play, args=(reply_wav(),))
        thread.start()
        deadline = time.monotonic() + 5
        while not player.playing and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        player.stop()
        thread.join(5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(player.playing)
        self.assertFalse(self.out.exists())

    def test_rejects_unexpected_formats(self):
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00" * 8)
        with self.assertRaises(ValueError):
            self.fake_player().play(out.getvalue())


class FakePlayer:
    playing = False

    def __init__(self):
        self.played: list[bytes] = []
        self.stopped = 0

    def play(self, audio):
        self.played.append(audio)

    def stop(self):
        self.stopped += 1


class RemoteVoiceTest(unittest.TestCase):
    def make(self, core=None, recorder=None):
        self.lines: list[str] = []
        self.player = FakePlayer()
        self.core = core or FakeCore()
        return RemoteVoice(self.core, recorder=recorder or FakeRecorder(), player=self.player,
                           say=self.lines.append, max_seconds=300)

    def test_a_voice_turn_sends_wav_and_plays_the_reply(self):
        voice = self.make()
        self.assertEqual(voice.max_seconds, 60)  # nunca más de lo que acepta el servidor
        voice.push_to_talk(lambda _: "")
        with wave.open(io.BytesIO(self.core.sent[0]), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (SAMPLE_RATE, 1, 2))
        self.assertEqual(self.core.spoken, ["Son las cinco."])
        self.assertEqual(self.player.played, [reply_wav()])
        self.assertIn("tú (voz) › ¿qué hora es?", self.lines)
        self.assertIn("arthur › Son las cinco.", self.lines)
        self.assertGreaterEqual(self.player.stopped, 1)  # al abrir el micro, ARTHUR se calla

    def test_silence_is_not_sent(self):
        voice = self.make(recorder=FakeRecorder(audio=b"\x00\x00" * SAMPLE_RATE))
        voice.push_to_talk(lambda _: "")
        self.assertEqual((self.core.sent, self.core.spoken), ([], []))

    def test_nothing_understood(self):
        voice = self.make(core=FakeCore(heard=""))
        self.assertTrue(voice.start())
        self.assertIsNone(voice.finish())
        self.assertEqual(self.core.spoken, [])
        self.assertTrue(any("No te he entendido" in line for line in self.lines))

    def test_server_errors_and_missing_microphone_are_explained(self):
        voice = self.make(core=FakeCore(fail="El servidor respondió 503: la voz no está activada en este servidor"))
        voice.push_to_talk(lambda _: "")
        self.assertTrue(any("no está activada" in line for line in self.lines))
        voice = self.make(recorder=FakeRecorder(fail=True))
        voice.push_to_talk(lambda _: "")
        self.assertTrue(any("micrófono" in line for line in self.lines))
        self.assertEqual(self.core.sent, [])

    def test_ctrl_c_while_recording_cancels(self):
        recorder = FakeRecorder()
        voice = self.make(recorder=recorder)

        def interrupt(_):
            raise KeyboardInterrupt

        voice.push_to_talk(interrupt)
        self.assertFalse(recorder.recording)
        self.assertEqual(self.core.sent, [])

    def test_no_voice_for_the_reply_is_not_fatal(self):
        core = FakeCore()

        def no_speech(text):
            raise RemoteError("El servidor respondió 502")

        core.speech = no_speech
        voice = self.make(core=core)
        self.assertTrue(voice.start())
        self.assertEqual(voice.finish(), "Son las cinco.")
        self.assertTrue(any("sin voz" in line for line in self.lines))


class ChatWithVoiceTest(unittest.TestCase):
    def test_empty_line_talks_and_text_still_works(self):
        core = FakeCore()
        core.ask = lambda text: f"eco: {text}"
        voice = RemoteVoice(core, recorder=FakeRecorder(), player=FakePlayer(), say=lambda _: None)
        lines = iter(["", "", "hola", "/salir"])  # Enter (abre micro), Enter (cierra), texto, salir
        out = io.StringIO()
        code = run_remote_client(core, input_fn=lambda _: next(lines), output=out, listen_events=False, voice=voice)
        self.assertEqual(code, 0)
        self.assertEqual(len(core.sent), 1)
        self.assertEqual(core.spoken, ["Son las cinco."])  # lo escrito no se dice en voz alta
        self.assertIn("arthur › eco: hola", out.getvalue())
        self.assertIn("Voz activada", out.getvalue())

    def test_without_voice_an_empty_line_does_nothing(self):
        core = FakeCore()
        core.ask = lambda text: f"eco: {text}"
        lines = iter(["", "/salir"])
        self.assertEqual(run_remote_client(core, input_fn=lambda _: next(lines), output=io.StringIO(),
                                           listen_events=False), 0)
        self.assertEqual(core.sent, [])


class HandsFreeRecorder(FakeRecorder):
    """Como el micro tras «Hey Arthur»: wait_for_end dice si llegaste a hablar."""

    def __init__(self, spoke=True, **kw):
        super().__init__(**kw)
        self.spoke = spoke
        self.options = None
        self.ignored = []

    def start(self, **options):
        super().start()
        self.options = options

    def ignore_for(self, seconds):
        self.ignored.append(seconds)

    def wait_for_end(self, **_):
        return self.spoke


class HandsFreeAndToggleTest(unittest.TestCase):
    def make(self, recorder):
        self.core = FakeCore()
        self.player = FakePlayer()
        return RemoteVoice(self.core, recorder=recorder, player=self.player, say=lambda _: None)

    def test_hands_free_uses_preroll_and_ignores_the_chime(self):
        recorder = HandsFreeRecorder()
        voice = self.make(recorder)
        dings = []
        voice.hands_free(preroll=b"xx", noise_floor=0.01, chime=lambda: dings.append(1))
        self.assertEqual(recorder.options["initial"], b"xx")
        self.assertGreater(recorder.options["ignore_seconds"], 0)
        self.assertEqual(len(self.core.sent), 1)
        deadline = time.monotonic() + 2
        while not recorder.ignored and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(dings, [1])
        self.assertFalse(voice.busy)

    def test_hands_free_without_speech_sends_nothing(self):
        voice = self.make(HandsFreeRecorder(spoke=False))
        voice.hands_free()
        self.assertEqual(self.core.sent, [])
        self.assertFalse(voice.recording)

    def test_toggle_opens_then_sends(self):
        voice = self.make(FakeRecorder())
        notes = []
        self.assertEqual(voice.toggle(notes.append), "escuchando")
        self.assertTrue(voice.busy)
        self.assertEqual(voice.hands_free(), None)  # «Hey Arthur» mientras tanto: no abre otro turno
        self.assertEqual(voice.toggle(notes.append), "procesando")
        deadline = time.monotonic() + 2
        while voice.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.core.sent), 1)
        self.assertEqual(self.core.spoken, ["Son las cinco."])

    def test_toggle_never_closes_a_hands_free_turn(self):
        recorder = HandsFreeRecorder()
        release = threading.Event()
        recorder.wait_for_end = lambda **_: release.wait(5)
        voice = self.make(recorder)
        thread = threading.Thread(target=voice.hands_free)
        thread.start()
        deadline = time.monotonic() + 2
        while not recorder.recording and time.monotonic() < deadline:
            time.sleep(0.01)
        notes = []
        self.assertEqual(voice.toggle(notes.append), "ocupado")
        release.set()
        thread.join(5)
        self.assertEqual(len(self.core.sent), 1)  # lo terminó «Hey Arthur», una sola vez
        self.assertFalse(voice.busy)


class NotifyConfirmTest(unittest.TestCase):
    def fake(self, out, code=0, sleep=0.0):
        return [sys.executable, "-I", "-c", f"import sys, time; time.sleep({sleep}); print({out!r}); sys.exit({code})"]

    def test_only_the_yes_button_confirms(self):
        from arthur.interfaces.voz_cliente import notify_confirm

        self.assertTrue(notify_confirm("borrar el evento", command=self.fake("si")))
        for out, code in (("no", 0), ("", 0), ("default", 0), ("si", 1)):
            self.assertFalse(notify_confirm("x", command=self.fake(out, code)), (out, code))
        with self.assertLogs("arthur.interfaces.voz_cliente", "WARNING"):
            self.assertFalse(notify_confirm("x", command=["no-existe-xyz"]))
        self.assertFalse(notify_confirm("x", timeout=0.3, command=self.fake("si", sleep=3)))  # sin respuesta: NO


class CoreEventLinkTest(unittest.TestCase):
    def test_confirmations_and_announcements(self):
        from arthur.interfaces.core import CoreEventLink

        class Core(FakeCore):
            def __init__(self):
                super().__init__()
                self.answers = []

            def confirm(self, pending_id, answer):
                self.answers.append((pending_id, answer))
                return True

        core, announced = Core(), []
        link = CoreEventLink(core, executor=None, specs=None, ask_confirm=lambda summary: summary == "abrir firefox",
                             announce=announced.append, say=lambda _: None)
        link.handle({"type": "confirmation_pending", "data": {"id": "c1", "summary": "abrir firefox"}})
        link.handle({"type": "confirmation_pending", "data": {"id": "c2", "summary": "otra cosa"}})
        link.handle({"type": "assistant_message", "data": {"text": "Llama al dentista", "announcement": True}})
        link.handle({"type": "assistant_message", "data": {"text": "respuesta normal"}})
        deadline = time.monotonic() + 2
        while len(core.answers) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(sorted(core.answers), [("c1", True), ("c2", False)])
        self.assertEqual(announced, ["Llama al dentista"])


class ClientServiceTest(unittest.TestCase):
    def test_runs_wake_and_hotkey_against_the_server(self):
        from arthur.api.control import send_command
        from arthur.core.config import Config, WakeConfig
        from arthur.interfaces.voz_cliente import run_client_service

        class FakeListener:
            made = []

            def __init__(self, on_wake, **kw):
                self.on_wake, self.kw, self.stopped = on_wake, kw, False
                FakeListener.made.append(self)

            def start(self):
                pass

            def stop(self):
                self.stopped = True

            def preroll(self):
                return b""

            noise_floor = None

        core = FakeCore()
        core.ask = lambda text: f"eco: {text}"
        core.events = lambda: iter(())
        player = FakePlayer()
        voice = RemoteVoice(core, recorder=HandsFreeRecorder(), player=player, say=lambda _: None)
        config = Config()
        import dataclasses
        config = dataclasses.replace(config, wake=dataclasses.replace(WakeConfig(), enabled=True, chime=False))
        from arthur.core.config import OrbConfig

        # El Orb en un puerto libre cualquiera y una ventana falsa: la prueba nunca toca tu escritorio.
        config = dataclasses.replace(config, orb=dataclasses.replace(OrbConfig(), port=0))
        notes, stop = [], threading.Event()
        windows = []

        class FakeWindow:
            def __init__(self, url):
                self.url, self.calls = url, []
                windows.append(self)

            def show(self):
                self.calls.append("show")

            def hide(self):
                self.calls.append("hide")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("arthur.interfaces.voz_cliente.ORB_HIDE_DELAY", 0):
            path = Path(tmp) / "control.sock"
            result = []
            thread = threading.Thread(target=lambda: result.append(run_client_service(
                core, config, control_path=path, stop=stop, notify=notes.append, voice=voice,
                ask_confirm=lambda s: False, wake_listener_factory=FakeListener, orb_window_factory=FakeWindow)))
            thread.start()
            deadline = time.monotonic() + 3
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(send_command("status", path).startswith("ok ARTHUR cliente"))
            self.assertEqual(send_command("ask hola", path), "eco: hola")
            url = send_command("orb-url", path)
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/\?t=")
            self.assertEqual(windows[0].url(), url)
            FakeListener.made[-1].on_wake()  # «Hey Arthur»
            self.assertEqual(len(core.sent), 1)
            deadline = time.monotonic() + 2
            while windows[0].calls != ["show", "hide"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(windows[0].calls, ["show", "hide"])  # a la vista mientras hablas; luego se quita
            self.assertEqual(send_command("toggle-listen", path), "escuchando")
            self.assertEqual(send_command("toggle-listen", path), "procesando")
            deadline = time.monotonic() + 2
            while len(core.sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(core.sent), 2)
            stop.set()
            thread.join(5)
        self.assertEqual(result, [0])
        self.assertTrue(FakeListener.made[-1].stopped)


class ClientOrbTest(unittest.TestCase):
    """El Orb del aparato en modo cliente (fase S7): ve lo que pasa aquí y contesta las confirmaciones."""

    def test_a_voice_turn_drives_the_orb(self):
        events = []
        voice = RemoteVoice(FakeCore(), recorder=FakeRecorder(), player=FakePlayer(), say=lambda _: None,
                            events=lambda kind, data: events.append((kind, data)))
        voice.start()
        voice.finish()
        self.assertEqual([d["to"] for k, d in events if k == "state"], ["listening", "thinking", "speaking", "idle",
                                                                        "idle"])
        self.assertIn(("user_message", {"text": "¿qué hora es?"}), events)
        self.assertIn(("assistant_message", {"text": "Son las cinco."}), events)

    def test_the_player_reports_the_level_while_it_sounds(self):
        levels = []
        player = WavPlayer(on_level=levels.append,
                           command=lambda rate, volume: [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"])
        player.play(reply_wav(frames=4410))  # 0,2 s
        self.assertGreater(len(levels), 2)
        self.assertEqual(levels[-1], 0.0)  # al acabar, el Orb deja de latir

    def test_confirmation_is_answered_in_the_open_orb(self):
        from arthur.api.control import send_command
        from arthur.core.config import Config, OrbConfig, WakeConfig
        from arthur.interfaces.voz_cliente import run_client_service
        import dataclasses

        asked = threading.Event()

        class Core(FakeCore):
            answers = []

            def events(self):
                asked.wait(5)  # en cuanto el Orb está abierto, el servidor pide permiso
                yield {"type": "confirmation_pending",
                       "data": {"id": "c1", "tool": "create_event", "risk": "write", "summary": "Crear «dentista»"}}
                time.sleep(5)

            def confirm(self, pending_id, answer):
                self.answers.append((pending_id, answer))
                return True

        core, notified = Core(), []
        config = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), enabled=False),
                                     orb=dataclasses.replace(OrbConfig(), port=0, fullscreen_on_wake=False))
        stop = threading.Event()
        voice = RemoteVoice(core, recorder=FakeRecorder(), player=FakePlayer(), say=lambda _: None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.sock"
            thread = threading.Thread(target=run_client_service, args=(core, config), kwargs=dict(
                control_path=path, stop=stop, notify=lambda _: None, voice=voice,
                ask_confirm=lambda s: notified.append(s) or False))
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                url = send_command("orb-url", path)
                port, token = int(url.split(":")[2].split("/")[0]), url.split("?t=")[1]
                sse = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                sse.request("GET", f"/events?t={token}")
                stream = sse.getresponse()
                self.assertEqual(stream.status, 200)
                asked.set()
                pending = None
                while pending is None:
                    line = stream.readline().decode()
                    if line.startswith("data: ") and '"confirmation_pending"' in line:
                        pending = json.loads(line[6:])["data"]
                self.assertEqual((pending["risk"], pending["summary"]), ("write", "Crear «dentista»"))
                post = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                post.request("POST", "/api/confirm", json.dumps({"id": pending["id"], "answer": True}), headers={
                    "Cookie": f"arthur_token={token}", "Origin": f"http://127.0.0.1:{port}", "X-Arthur": "1",
                    "Content-Type": "application/json"})
                self.assertEqual(post.getresponse().status, 200)
                deadline = time.monotonic() + 3
                while not core.answers and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(core.answers, [("c1", True)])  # el id del SERVIDOR, no el del Orb
                self.assertEqual(notified, [])  # con el Orb abierto no sale la notificación
                sse.close()
            finally:
                stop.set()
                thread.join(5)


class ClientGesturesTest(unittest.TestCase):
    """Los gestos en modo cliente (fase S7, paso 2). La cámara NUNCA se enciende: los gestos se inyectan en el bus
    como si los hubiera visto la burbuja."""

    def test_gestures_act_on_this_device(self):
        from arthur.api.control import send_command
        from arthur.core.config import Config, GesturesConfig, OrbConfig, WakeConfig
        from arthur.core.events import Event
        from arthur.interfaces.gestos.watcher import GestureSwitch
        from arthur.interfaces.voz_cliente import run_client_service
        import dataclasses

        class Executor:
            audit = None

            def __init__(self):
                self.calls = []

            def run(self, name, arguments):
                self.calls.append((name, arguments))
                return "ok", "hecho"

        core, executor, switch, player = FakeCore(), Executor(), GestureSwitch(), FakePlayer()
        core.events = lambda: iter(())
        config = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), enabled=False),
                                     orb=dataclasses.replace(OrbConfig(), port=0, fullscreen_on_wake=False),
                                     gestures=dataclasses.replace(GesturesConfig(), enabled=True))
        voice = RemoteVoice(core, recorder=FakeRecorder(), player=player, say=lambda _: None)
        stop, notes = threading.Event(), []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.sock"
            thread = threading.Thread(target=run_client_service, args=(core, config), kwargs=dict(
                control_path=path, stop=stop, notify=lambda _: None, voice=voice, executor=executor,
                gestures=switch, camera_notify=notes.append))
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                watcher = switch.watcher
                self.assertIsNotNone(watcher)
                self.assertFalse(watcher.active)  # nace con la cámara APAGADA
                stopped_before = player.stopped
                watcher.bus.publish(Event("gesture", {"name": "palma"}))      # ✋ calla
                watcher.bus.publish(Event("gesture", {"name": "dos_dedos"}))  # ✌️ pausa/play
                self.assertGreater(player.stopped, stopped_before)
                self.assertEqual(executor.calls, [("media", {"action": "pause"}), ("media", {"action": "toggle"})])
                # El botón del Orb existe (los gestos están permitidos) y la cámara sale apagada
                url = send_command("orb-url", path)
                port, token = int(url.split(":")[2].split("/")[0]), url.split("?t=")[1]
                sse = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                sse.request("GET", f"/events?t={token}")
                stream = sse.getresponse()
                line = ""
                while not line.startswith("data: "):
                    line = stream.readline().decode()
                self.assertEqual(json.loads(line[6:])["data"]["camera"], {"available": True, "on": False})
                sse.close()
            finally:
                stop.set()
                thread.join(5)
        self.assertEqual(notes, [])  # nunca se encendió la cámara


class StreamingCore(FakeCore):
    """Un servidor 0.56.0: manda lo oído, cada frase con su voz y al final la respuesta."""

    def __init__(self, sentences=("Hoy hace sol.", "Mañana, lluvia."), heard="¿qué tiempo hace?", fail=None):
        super().__init__(heard=heard, reply=" ".join(sentences))
        self.sentences, self.stream_fail = sentences, fail
        self.closed = False

    def voice_stream(self, wav):
        import base64

        if self.stream_fail:
            raise RemoteError(self.stream_fail)
        self.sent.append(wav)
        try:
            yield {"heard": self.heard}
            if not self.heard:
                yield {"reply": None}
                return
            for i, sentence in enumerate(self.sentences):
                yield {"say": sentence, "audio": base64.b64encode(reply_wav(frames=10 + i)).decode()}
            yield {"reply": self.reply}
        finally:
            self.closed = True


class StreamedVoiceTest(unittest.TestCase):
    def make(self, core):
        self.player, events = FakePlayer(), []
        voice = RemoteVoice(core, recorder=FakeRecorder(), player=self.player, say=lambda _: None,
                            events=lambda kind, data: events.append((kind, data)))
        return voice, events

    def test_sentences_are_played_in_order_as_they_arrive(self):
        core = StreamingCore()
        voice, events = self.make(core)
        voice.start()
        self.assertEqual(voice.finish(), "Hoy hace sol. Mañana, lluvia.")
        self.assertEqual(self.player.played, [reply_wav(frames=10), reply_wav(frames=11)])
        self.assertEqual(core.spoken, [])  # ni se pide la voz aparte: ya venía con cada frase
        self.assertIn(("user_message", {"text": "¿qué tiempo hace?"}), events)
        self.assertIn(("assistant_message", {"text": "Hoy hace sol. Mañana, lluvia."}), events)
        self.assertEqual([d["to"] for k, d in events if k == "state"][-2:], ["speaking", "idle"])

    def test_being_cut_drops_the_sentences_still_waiting(self):
        core = StreamingCore(sentences=("Uno, dos y tres.", "Cuatro y cinco.", "Seis y siete."))
        voice, _ = self.make(core)
        voice.start()
        original = self.player.play

        def play_then_cut(audio):
            original(audio)
            voice.stop_speaking()  # ✋ mientras suena la primera
        self.player.play = play_then_cut
        voice.finish()
        self.assertEqual(len(self.player.played), 1)
        self.assertTrue(core.closed)

    def test_an_older_server_still_works(self):
        core = StreamingCore(fail="El servidor respondió 404: no existe")
        voice, _ = self.make(core)
        voice.start()
        self.assertEqual(voice.finish(), core.reply)  # por /voice + /speech, como antes
        self.assertEqual(core.spoken, [core.reply])

    def test_nothing_understood(self):
        lines = []
        voice = RemoteVoice(StreamingCore(heard=""), recorder=FakeRecorder(), player=FakePlayer(), say=lines.append)
        voice.start()
        self.assertIsNone(voice.finish())
        self.assertIn("  No te he entendido. Vuelve a intentarlo.", lines)


class ClientAnnouncementsTest(unittest.TestCase):
    """Los avisos de WhatsApp en modo cliente (fase S7, paso 3): la escucha va aquí y ARTHUR los dice aquí."""

    def test_a_whatsapp_message_is_announced_when_free(self):
        from arthur.core.config import Config, OrbConfig, WakeConfig
        from arthur.integrations.notifications import Notification
        from arthur.interfaces.voz_cliente import run_client_service
        import dataclasses

        class Watcher:
            on_notification = None

        core, watcher, stop = FakeCore(), Watcher(), threading.Event()
        core.events = lambda: iter(())
        config = dataclasses.replace(Config(), wake=dataclasses.replace(WakeConfig(), enabled=False),
                                     orb=dataclasses.replace(OrbConfig(), port=0, fullscreen_on_wake=False))
        voice = RemoteVoice(core, recorder=FakeRecorder(), player=FakePlayer(), say=lambda _: None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.sock"
            thread = threading.Thread(target=run_client_service, args=(core, config), kwargs=dict(
                control_path=path, stop=stop, notify=lambda _: None, voice=voice, notifications=watcher))
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while watcher.on_notification is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                watcher.on_notification(Notification("Firefox", "Laura", "¿Vienes a cenar?", "whatsapp"))
                deadline = time.monotonic() + 5
                while not core.spoken and time.monotonic() < deadline:
                    time.sleep(0.05)
                # Sin announce_text (por defecto), solo quién escribe: el texto no se lee
                self.assertEqual(core.spoken, ["Te ha escrito Laura por WhatsApp."])
            finally:
                stop.set()
                thread.join(5)


class WavBytesTest(unittest.TestCase):
    def test_is_what_the_server_expects(self):
        from arthur.voice.voice_server import pcm_from_wav

        self.assertEqual(pcm_from_wav(wav_bytes(tone(0.5))), tone(0.5))


if __name__ == "__main__":
    unittest.main()
