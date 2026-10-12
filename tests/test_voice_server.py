"""arthur-voz (fase S4): el oído y la voz del servidor.

Todo con falsos: el «whisper» es una función, y el «Piper» es un auxiliar de mentira que habla el mismo protocolo
que piper_worker.py. El servidor escucha en 127.0.0.1 en un puerto libre. Nada de micrófono ni altavoces.
"""

import io
import json
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

from arthur.voice.stt import SttError
from arthur.voice.tts import TtsError
from arthur.voice.voice_server import (MAX_AUDIO_SECONDS, PiperSynth, VoiceServer, VoiceServerError, check_model,
                                       main, pcm_from_wav, sha256_of, wav_from_pcm)

TOKEN = "k" * 40

FAKE_WORKER = textwrap.dedent("""
    import json, struct, sys
    def send(kind, payload=b""):
        sys.stdout.buffer.write(kind + struct.pack(">I", len(payload)) + payload)
        sys.stdout.buffer.flush()
    send(b"R", json.dumps({"sample_rate": 22050}).encode())
    for line in sys.stdin:
        request = json.loads(line)
        if request["text"] == "rompe":
            send(b"X", b"frase rota")
            continue
        # Un «audio» que dice qué texto y velocidad le llegaron: así el test lo puede comprobar.
        send(b"A", ("%s|%s" % (request["text"], request["length_scale"])).encode())
        send(b"A", b"!!")
        send(b"E")
""")


def wav(seconds: float = 1.0, rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01" * width * channels * int(rate * seconds))
    return out.getvalue()


class WavTest(unittest.TestCase):
    def test_accepts_only_what_the_client_records(self):
        self.assertEqual(len(pcm_from_wav(wav(0.5))), 16_000)
        for bad in (wav(rate=44_100), wav(channels=2), wav(width=1), b"esto no es un wav", b""):
            with self.assertRaises(VoiceServerError):
                pcm_from_wav(bad)

    def test_rejects_audio_longer_than_the_limit(self):
        with self.assertRaises(VoiceServerError):
            pcm_from_wav(wav(MAX_AUDIO_SECONDS + 1))

    def test_round_trip(self):
        pcm = b"\x10\x00" * 100
        with wave.open(io.BytesIO(wav_from_pcm(pcm, 22050)), "rb") as w:
            self.assertEqual((w.getframerate(), w.readframes(100)), (22050, pcm))


class ModelCheckTest(unittest.TestCase):
    def test_a_model_with_another_fingerprint_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "modelo.bin"
            model.write_bytes(b"modelo de verdad")
            check_model(model, sha256_of(model))  # la buena: no protesta
            model.write_bytes(b"modelo cambiado")
            with self.assertRaises(VoiceServerError):
                check_model(model, "0" * 64)
            with self.assertRaises(VoiceServerError):
                check_model(Path(tmp) / "no-existe.bin", "")


class PiperSynthTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        worker = Path(self.dir.name) / "worker.py"
        worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.synth = PiperSynth(Path("voz.onnx"), worker_cmd=[sys.executable, "-I", str(worker)])

    def tearDown(self):
        self.synth.close()
        self.dir.cleanup()

    def test_joins_all_the_audio_of_a_sentence(self):
        pcm, rate = self.synth.synthesize("hola", 1.2)
        self.assertEqual((pcm, rate), (b"hola|1.2!!", 22050))

    def test_a_broken_sentence_does_not_kill_the_worker(self):
        with self.assertRaises(TtsError):
            self.synth.synthesize("rompe")
        self.assertEqual(self.synth.synthesize("sigo")[0], b"sigo|1.0!!")

    def test_restarts_the_worker_if_it_died(self):
        self.synth.synthesize("uno")
        self.synth._worker.kill()
        self.synth._worker.wait()
        self.assertEqual(self.synth.synthesize("dos")[0], b"dos|1.0!!")


class VoiceServerTest(unittest.TestCase):
    def setUp(self):
        self.heard: list[bytes] = []
        self.said: list[tuple[str, float]] = []
        self.transcribe_hook = lambda pcm: "pon música"
        self.synth_hook = lambda text, speed: (b"\x00\x00" * 10, 22050)

        def transcribe(pcm):
            self.heard.append(pcm)
            return self.transcribe_hook(pcm)

        def synthesize(text, speed):
            self.said.append((text, speed))
            return self.synth_hook(text, speed)

        self.server = VoiceServer(token=TOKEN, transcribe=transcribe, synthesize=synthesize, port=0)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.stop()

    def request(self, method, path, body=None, content_type=None, token=TOKEN):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type"), e.read()

    def tts(self, payload):
        return self.request("POST", "/tts", json.dumps(payload).encode(), "application/json")

    def test_health_needs_no_key_but_everything_else_does(self):
        self.assertEqual(self.request("GET", "/health", token=None)[0], 200)
        with self.assertLogs("arthur.voice.voice_server", "WARNING") as logs:  # queda en el registro
            self.assertEqual(self.request("POST", "/stt", wav(), "audio/wav", token=None)[0], 401)
            self.assertEqual(self.request("POST", "/stt", wav(), "audio/wav", token="x" * 40)[0], 401)
            self.assertEqual(self.request("GET", "/otra", token="x" * 40)[0], 401)
        self.assertEqual(len(logs.output), 3)
        self.assertEqual(self.heard, [])

    def test_a_short_key_is_refused_at_start(self):
        with self.assertRaises(VoiceServerError):
            VoiceServer(token="corta", transcribe=lambda p: "", synthesize=lambda t, s: (b"", 1), port=0)

    def test_stt_returns_the_text(self):
        status, _, body = self.request("POST", "/stt", wav(1.0), "audio/wav")
        self.assertEqual((status, json.loads(body)), (200, {"text": "pon música"}))
        self.assertEqual(len(self.heard[0]), 32_000)  # le llega el audio sin cabecera

    def test_stt_rejects_wrong_formats_and_sizes(self):
        self.assertEqual(self.request("POST", "/stt", wav(rate=44_100), "audio/wav")[0], 400)
        self.assertEqual(self.request("POST", "/stt", wav(), "application/json")[0], 415)
        self.assertEqual(self.request("POST", "/stt", b"", "audio/wav")[0], 413)
        self.assertEqual(self.heard, [])

    def test_stt_failure_does_not_leak_details(self):
        def fail(pcm):
            raise SttError("whisper-cli ha fallado: /models/secreto")
        self.transcribe_hook = fail
        with self.assertLogs("arthur.voice.voice_server", "WARNING"):
            status, _, body = self.request("POST", "/stt", wav(), "audio/wav")
        self.assertEqual(status, 500)
        self.assertNotIn("secreto", body.decode())

    def test_only_one_transcription_at_a_time(self):
        release = threading.Event()
        self.transcribe_hook = lambda pcm: (release.wait(5), "lento")[1]
        first: list = []
        thread = threading.Thread(target=lambda: first.append(self.request("POST", "/stt", wav(), "audio/wav")))
        thread.start()
        deadline = time.monotonic() + 5
        while not self.heard and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.request("POST", "/stt", wav(), "audio/wav")[0], 503)
        release.set()
        thread.join(5)
        self.assertEqual(first[0][0], 200)
        self.assertEqual(self.request("POST", "/stt", wav(), "audio/wav")[0], 200)  # libre otra vez

    def test_tts_returns_a_wav_of_the_clean_text(self):
        status, content_type, body = self.tts({"text": "**Hola** 👋 mira https://ejemplo.com", "speed": 1.1})
        self.assertEqual((status, content_type), (200, "audio/wav"))
        with wave.open(io.BytesIO(body), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate(), w.getnframes()), (1, 22050, 10))
        self.assertEqual(self.said, [("Hola mira el enlace", 1.1)])

    def test_tts_validates_its_input(self):
        for bad in ({}, {"text": ""}, {"text": "x" * 4001}, {"text": "hola", "speed": 5},
                    {"text": "hola", "speed": True}, {"text": "👋👋"}, ["hola"]):
            self.assertEqual(self.tts(bad)[0], 400, bad)
        self.assertEqual(self.request("POST", "/tts", b"{no json", "application/json")[0], 400)
        self.assertEqual(self.said, [])

    def test_tts_failure_is_a_clean_error(self):
        def fail(text, speed):
            raise TtsError("onnx explotó")
        self.synth_hook = fail
        with self.assertLogs("arthur.voice.voice_server", "WARNING"):
            status, _, body = self.tts({"text": "hola"})
        self.assertEqual(status, 500)
        self.assertNotIn("onnx", body.decode())


class MainTest(unittest.TestCase):
    def test_does_not_start_without_a_key_or_with_a_tampered_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            stt = Path(tmp) / "stt.bin"
            stt.write_bytes(b"modelo")
            env = {"ARTHUR_VOZ_TOKEN_FILE": str(Path(tmp) / "no-hay-llave"), "ARTHUR_VOZ_STT_MODEL": str(stt)}
            with self.assertLogs("arthur.voice.voice_server", "ERROR"):
                self.assertEqual(main(env), 1)
            key = Path(tmp) / "llave"
            key.write_text(TOKEN)
            env.update(ARTHUR_VOZ_TOKEN_FILE=str(key), ARTHUR_VOZ_STT_SHA256="0" * 64)
            with self.assertLogs("arthur.voice.voice_server", "ERROR") as logs:
                self.assertEqual(main(env), 1)
            self.assertIn("huella", logs.output[0])


if __name__ == "__main__":
    unittest.main()
