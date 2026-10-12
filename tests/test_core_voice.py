"""La voz del ARTHUR del servidor (fase S4, paso 2): aparato → arthur-core → arthur-voz y vuelta.

Todo en este proceso y en 127.0.0.1: el aparato es RemoteCore, el core es CoreServer con un «cerebro» de eco, y
arthur-voz es VoiceServer con whisper y Piper falsos (funciones). Nada de micrófono, altavoces ni Docker.
"""

import io
import tempfile
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

from arthur.api.core_api import CoreServer
from arthur.core.events import EventBus
from arthur.integrations.voz import VoiceError, VoiceLink, check_voice_url
from arthur.interfaces.core import RemoteCore, RemoteError
from arthur.security.devices import DeviceRegistry
from arthur.voice.voice_server import VoiceServer

VOZ_TOKEN = "v" * 40


def wav(seconds: float = 1.0, rate: int = 16_000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds))
    return out.getvalue()


class VoiceUrlTest(unittest.TestCase):
    def test_only_inside_the_internal_network(self):
        for good in ("http://voz:8771", "http://127.0.0.1:8771/", "http://172.18.0.3:8771", "http://localhost:8771"):
            self.assertTrue(check_voice_url(good).startswith("http://"), good)
        for bad in ("https://voz:8771", "http://voz.example.com:8771", "http://8.8.8.8:8771", "http://voz:8771/stt",
                    "ftp://voz", "voz:8771", ""):
            with self.assertRaises(ValueError, msg=bad):
                check_voice_url(bad)

    def test_from_env(self):
        self.assertIsNone(VoiceLink.from_env({}))
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "llave"
            key.write_text(VOZ_TOKEN + "\n")
            link = VoiceLink.from_env({"ARTHUR_VOZ_URL": "http://voz:8771", "ARTHUR_VOZ_TOKEN_FILE": str(key)},
                                      speed=1.2)
            self.assertEqual((link.url, link.speed), ("http://voz:8771", 1.2))
            with self.assertRaises(ValueError):
                VoiceLink.from_env({"ARTHUR_VOZ_URL": "http://voz:8771", "ARTHUR_VOZ_TOKEN_FILE": str(key) + "x"})
            key.write_text("corta")
            with self.assertRaises(ValueError):
                VoiceLink.from_env({"ARTHUR_VOZ_URL": "http://voz:8771", "ARTHUR_VOZ_TOKEN_FILE": str(key)})


class CoreVoiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.devices = DeviceRegistry(Path(self.dir.name) / "devices.json")
        self.token = self.devices.add("cachyos")
        self.asked: list[str] = []
        self.said: list[tuple[str, float]] = []
        self.transcript = "Hey Arthur, ¿qué hora es?"

        def synthesize(text, speed):
            self.said.append((text, speed))
            return b"\x00\x00" * 5, 22050

        self.voz = VoiceServer(token=VOZ_TOKEN, transcribe=lambda pcm: self.transcript, synthesize=synthesize, port=0)
        self.voz.start()
        link = VoiceLink(f"http://127.0.0.1:{self.voz.port}", VOZ_TOKEN, speed=1.1)

        def handle_text(text):
            self.asked.append(text)
            return f"eco: {text}"

        self.server = CoreServer(bus=EventBus(), devices=self.devices, handle_text=handle_text, voice=link,
                                 host="127.0.0.1", port=0)
        self.server.start()
        self.core = RemoteCore(f"http://127.0.0.1:{self.server.port}", self.token)

    def tearDown(self):
        self.server.stop()
        self.voz.stop()
        self.dir.cleanup()

    def test_voice_goes_through_the_assistant_without_the_wake_phrase(self):
        self.assertEqual(self.core.voice(wav()), {"heard": "¿qué hora es?", "reply": "eco: ¿qué hora es?"})
        self.assertEqual(self.asked, ["¿qué hora es?"])

    def test_nothing_heard_does_not_bother_the_assistant(self):
        for transcript in ("", "Hey Arthur."):
            self.transcript = transcript
            self.assertEqual(self.core.voice(wav()), {"heard": "", "reply": None})
        self.assertEqual(self.asked, [])

    def test_speech_returns_the_voice_of_arthur(self):
        audio = self.core.speech("Son las **cinco**.")
        with wave.open(io.BytesIO(audio), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnframes()), (22050, 5))
        self.assertEqual(self.said, [("Son las cinco.", 1.1)])

    def test_bad_audio_is_explained_and_never_reaches_the_assistant(self):
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            with self.assertRaises(RemoteError) as ctx:
                self.core.voice(wav(rate=44_100))
        self.assertIn("16000 Hz", str(ctx.exception))
        self.assertEqual(self.asked, [])

    def test_voice_needs_the_device_key(self):
        import arthur.api.core_api as api
        delay, api.BAD_KEY_DELAY = api.BAD_KEY_DELAY, 0
        try:
            with self.assertLogs("arthur.api.core_api", "WARNING"):
                with self.assertRaises(RemoteError):
                    RemoteCore(self.core.url, "arthur_falsa").voice(wav())
        finally:
            api.BAD_KEY_DELAY = delay
        self.assertEqual(self.asked, [])

    def test_too_much_audio_is_refused_by_the_core(self):
        req = urllib.request.Request(self.core.url + "/api/v1/voice", data=b"x", method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "audio/wav")
        req.add_header("Content-Length", str(10 * 1024 * 1024))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 413)

    def test_voice_server_down_is_a_clean_error(self):
        self.voz.stop()
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            with self.assertRaises(RemoteError) as ctx:
                self.core.speech("hola")
        self.assertIn("502", str(ctx.exception))


class CoreWithoutVoiceTest(unittest.TestCase):
    def test_says_voice_is_not_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            devices = DeviceRegistry(Path(tmp) / "devices.json")
            token = devices.add("cachyos")
            server = CoreServer(bus=EventBus(), devices=devices, handle_text=lambda t: t, host="127.0.0.1", port=0)
            server.start()
            try:
                with self.assertRaises(RemoteError) as ctx:
                    RemoteCore(f"http://127.0.0.1:{server.port}", token).speech("hola")
                self.assertIn("no está activada", str(ctx.exception))
            finally:
                server.stop()


class VoiceLinkErrorsTest(unittest.TestCase):
    def test_busy_rejected_and_wrong_key_keep_their_meaning(self):
        server = VoiceServer(token=VOZ_TOKEN, transcribe=lambda pcm: "", synthesize=lambda t, s: (b"", 22050), port=0)
        server.start()
        try:
            link = VoiceLink(f"http://127.0.0.1:{server.port}", VOZ_TOKEN)
            with self.assertRaises(VoiceError) as ctx:
                link.transcribe(b"no es un wav")
            self.assertEqual(ctx.exception.status, 400)
            server._stt_lock.acquire()  # como si otra transcripción estuviera en marcha
            try:
                with self.assertRaises(VoiceError) as ctx:
                    link.transcribe(wav())
                self.assertEqual(ctx.exception.status, 503)
            finally:
                server._stt_lock.release()
            import arthur.voice.voice_server as vs
            delay, vs.BAD_KEY_DELAY = vs.BAD_KEY_DELAY, 0
            try:
                with self.assertLogs("arthur.voice.voice_server", "WARNING"):
                    with self.assertRaises(VoiceError) as ctx:
                        VoiceLink(f"http://127.0.0.1:{server.port}", "w" * 40).speak("hola")
            finally:
                vs.BAD_KEY_DELAY = delay
            self.assertEqual(ctx.exception.status, 502)
        finally:
            server.stop()



class CoreVoiceStreamTest(unittest.TestCase):
    """La voz a trozos (0.56.0): la primera frase llega con su audio antes de que acabe la respuesta."""

    def test_sentences_arrive_with_audio_before_the_reply(self):
        import base64
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            devices = DeviceRegistry(Path(tmp) / "devices.json")
            token = devices.add("cachyos")
            said = []
            voz = VoiceServer(token=VOZ_TOKEN, transcribe=lambda pcm: "Hey Arthur, ¿qué tiempo hace?",
                              synthesize=lambda text, speed: said.append(text) or (b"\x00\x00" * 5, 22050), port=0)
            voz.start()
            self.addCleanup(voz.stop)
            first_heard = threading.Event()

            def handle_text(text, images=(), on_speech=None):
                on_speech("Hoy hace sol.")
                first_heard.wait(5)  # el aparato ya tiene la primera frase mientras «sigue escribiendo»
                on_speech("Mañana, lluvia.")
                return "Hoy hace sol. Mañana, lluvia."

            server = CoreServer(bus=EventBus(), devices=devices, handle_text=handle_text, port=0,
                                voice=VoiceLink(f"http://127.0.0.1:{voz.port}", VOZ_TOKEN))
            server.start()
            self.addCleanup(server.stop)
            lines = []
            for item in RemoteCore(f"http://127.0.0.1:{server.port}", token).voice_stream(wav()):
                lines.append(item)
                if "say" in item:
                    first_heard.set()
            self.assertEqual(lines[0], {"heard": "¿qué tiempo hace?"})
            self.assertEqual([l["say"] for l in lines if "say" in l], ["Hoy hace sol.", "Mañana, lluvia."])
            self.assertTrue(all(base64.b64decode(l["audio"]).startswith(b"RIFF") for l in lines if "say" in l))
            self.assertEqual(lines[-1], {"reply": "Hoy hace sol. Mañana, lluvia."})
            self.assertEqual(said, ["Hoy hace sol.", "Mañana, lluvia."])

    def test_nothing_heard(self):
        with tempfile.TemporaryDirectory() as tmp:
            devices = DeviceRegistry(Path(tmp) / "devices.json")
            token = devices.add("cachyos")
            voz = VoiceServer(token=VOZ_TOKEN, transcribe=lambda pcm: "", synthesize=lambda t, s: (b"", 22050), port=0)
            voz.start()
            self.addCleanup(voz.stop)
            server = CoreServer(bus=EventBus(), devices=devices, handle_text=lambda *a, **k: "x", port=0,
                                voice=VoiceLink(f"http://127.0.0.1:{voz.port}", VOZ_TOKEN))
            server.start()
            self.addCleanup(server.stop)
            lines = list(RemoteCore(f"http://127.0.0.1:{server.port}", token).voice_stream(wav()))
            self.assertEqual(lines, [{"heard": ""}, {"reply": None}])


if __name__ == "__main__":
    unittest.main()
