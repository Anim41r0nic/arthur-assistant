"""arthur-voz: el oído y la voz del ARTHUR del servidor (fase S4, docs/servidor.md).

Corre en su propio contenedor (Dockerfile.voz), al lado de arthur-core. Solo le habla arthur-core, por la red
interna de Docker: no publica ningún puerto hacia fuera.

API:
  GET  /health → {"ok": true}                               (sin llave: solo dice que está vivo)
  POST /stt    cuerpo audio/wav (16 kHz, mono, 16 bits)       → {"text": "lo que has dicho"}
  POST /tts    {"text": "…", "speed": 1.0}                   → audio/wav (lo que dirá ARTHUR)

Por qué un contenedor aparte: arthur-core sigue siendo solo biblioteca estándar, y la voz (whisper.cpp + Piper
con onnxruntime) se puede cambiar o quitar sin tocarlo. Este fichero tampoco importa Piper: lo usa a través de
piper_worker.py (GPL), igual que en el portátil.

Seguridad:
  - Llave compartida con arthur-core (fichero de secretos 600, `ARTHUR_VOZ_TOKEN_FILE`): aunque la red interna
    ya lo aísla, otro contenedor de esa red no puede usarlo sin ella.
  - Tamaños limitados (60 s de audio, 4.000 caracteres), solo el formato de audio esperado, una transcripción a
    la vez (whisper usa mucha memoria y el servidor tiene 8 GB): si ya hay una, responde «ocupado» al momento.
  - Los modelos se comprueban con su huella sha256 al arrancar: si no coinciden, no arranca.
  - El registro dice qué se pidió, cuánto duró y cuánto tardó; nunca lo que dijiste ni lo que dirá.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .stt import SAMPLE_RATE, SttError, WhisperTranscriber, build_prompt
from .tts import WORKER, TtsError, speakable

log = logging.getLogger(__name__)

MAX_AUDIO_SECONDS = 60
MAX_AUDIO_BYTES = MAX_AUDIO_SECONDS * SAMPLE_RATE * 2 + 1024  # + la cabecera WAV
MAX_JSON = 16 * 1024
MAX_TEXT = 4_000
REQUEST_TIMEOUT = 30   # para ENVIAR la petición (60 s de audio por la red de casa tardan mucho menos)
BAD_KEY_DELAY = 0.5

# Los modelos de siempre (los mismos del portátil). Otro modelo → otra huella en el entorno del contenedor.
DEFAULT_STT_MODEL = "/models/whisper/ggml-small-q5_1.bin"
DEFAULT_STT_SHA256 = "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb"
DEFAULT_TTS_MODEL = "/models/voices/es_ES-davefx-medium.onnx"
DEFAULT_TTS_SHA256 = "6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917"


class VoiceServerError(Exception):
    pass


# --- piezas -------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_model(path: Path, expected: str) -> None:
    if not path.is_file():
        raise VoiceServerError(f"No encuentro el modelo {path}")
    if expected and sha256_of(path) != expected.lower():
        raise VoiceServerError(f"La huella de {path} no coincide: no lo uso (¿descarga corrupta o cambiada?)")


def pcm_from_wav(data: bytes) -> bytes:
    """Saca el audio de un WAV y comprueba que es justo lo que graba el cliente: 16 kHz, mono, 16 bits."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
                raise VoiceServerError(f"El audio debe ser WAV de {SAMPLE_RATE} Hz, mono y 16 bits")
            if wav.getnframes() > MAX_AUDIO_SECONDS * SAMPLE_RATE:
                raise VoiceServerError(f"El audio dura más de {MAX_AUDIO_SECONDS} s")
            return wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as e:
        raise VoiceServerError("No es un WAV válido") from e


def wav_from_pcm(pcm: bytes, sample_rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue()


class PiperSynth:
    """Texto → audio completo (sin reproducirlo: eso lo hará el aparato). Usa el mismo piper_worker.py que el
    portátil, cargado UNA vez; una frase cada vez."""

    def __init__(self, voice_model: Path, *, python: str = "/opt/piper/bin/python",
                 worker_cmd: list[str] | None = None) -> None:
        """`worker_cmd` solo se cambia en los tests (un auxiliar falso, sin Piper)."""
        self.worker_cmd = worker_cmd or [python, str(WORKER), str(voice_model)]
        self._worker: subprocess.Popen | None = None
        self.sample_rate = 22050
        self._lock = threading.Lock()

    def _ensure_worker(self) -> subprocess.Popen:
        if self._worker is not None and self._worker.poll() is None:
            return self._worker
        self._discard()  # el anterior murió: cerrar sus tuberías antes de arrancar otro
        self._worker = subprocess.Popen(self.worker_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        kind, payload = self._read()
        if kind != b"R":
            raise TtsError("El proceso de voz no ha arrancado bien")
        self.sample_rate = json.loads(payload)["sample_rate"]
        return self._worker

    def _read(self) -> tuple[bytes, bytes]:
        header = self._worker.stdout.read(5)
        if len(header) < 5:
            self._discard()  # la próxima frase lo vuelve a arrancar
            raise TtsError("El proceso de voz se ha cerrado inesperadamente")
        return header[:1], self._worker.stdout.read(struct.unpack(">I", header[1:])[0])

    def warm_up(self) -> None:
        with self._lock:
            self._ensure_worker()

    def synthesize(self, text: str, speed: float = 1.0) -> tuple[bytes, int]:
        """Devuelve (PCM de 16 bits mono, frecuencia)."""
        with self._lock:
            worker = self._ensure_worker()
            try:
                worker.stdin.write((json.dumps({"text": text, "length_scale": speed}) + "\n").encode())
                worker.stdin.flush()
            except OSError as e:  # murió justo ahora: error limpio, y la próxima frase lo rearranca
                self._discard()
                raise TtsError("El proceso de voz se ha cerrado inesperadamente") from e
            pcm = bytearray()
            while True:
                kind, payload = self._read()
                if kind == b"E":
                    return bytes(pcm), self.sample_rate
                if kind == b"X":
                    raise TtsError(payload.decode(errors="replace"))
                pcm += payload

    def close(self) -> None:
        self._discard()

    def _discard(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        for pipe in (worker.stdin, worker.stdout):
            try:
                pipe.close()  # al cerrar su entrada, el proceso de voz termina solo
            except OSError:
                pass
        try:
            worker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()


# --- servidor -----------------------------------------------------------------

class VoiceServer:
    def __init__(self, *, token: str, transcribe: Callable[[bytes], str],
                 synthesize: Callable[[str, float], tuple[bytes, int]], host: str = "127.0.0.1",
                 port: int = 8771) -> None:
        """`transcribe(pcm) -> texto` (WhisperTranscriber.transcribe); `synthesize(texto, velocidad) -> (pcm, Hz)`."""
        if len(token) < 32:
            raise VoiceServerError("La llave de arthur-voz es demasiado corta (mínimo 32 caracteres)")
        self._token = token.encode()
        self.transcribe = transcribe
        self.synthesize = synthesize
        self._stt_lock = threading.Lock()
        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="voz-api", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("arthur-voz escuchando en el puerto %d", self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT
            server_version = "arthur-voz"
            sys_version = ""

            def log_message(self, fmt, *args):
                pass  # sin el registro por defecto; se anota a mano y sin contenido

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                given = header[7:].strip().encode() if header.startswith("Bearer ") else b""
                if hmac.compare_digest(given, server._token):  # tiempo constante: no da pistas de la llave
                    return True
                log.warning("arthur-voz: llave no válida desde %s", self.client_address[0])
                time.sleep(BAD_KEY_DELAY)
                self._reply(401, {"error": "llave no válida"})
                return False

            def _body(self, content_type: str, limit: int) -> bytes | None:
                if self.headers.get("Content-Type", "").split(";")[0].strip() != content_type:
                    self._reply(415, {"error": f"se espera {content_type}"})
                    return None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= limit:
                    self._reply(413, {"error": "cuerpo vacío o demasiado grande"})
                    return None
                return self.rfile.read(length)

            def do_GET(self):
                if self.path == "/health":
                    return self._reply(200, {"ok": True})
                if self._authorized():
                    self._reply(404, {"error": "no existe"})

            def do_POST(self):
                if not self._authorized():
                    return
                if self.path == "/stt":
                    return self._stt()
                if self.path == "/tts":
                    return self._tts()
                self._reply(404, {"error": "no existe"})

            def _stt(self):
                data = self._body("audio/wav", MAX_AUDIO_BYTES)
                if data is None:
                    return
                try:
                    pcm = pcm_from_wav(data)
                except VoiceServerError as e:
                    return self._reply(400, {"error": str(e)})
                # Una a la vez: dos whisper juntos no caben bien en 8 GB junto a Ollama.
                if not server._stt_lock.acquire(blocking=False):
                    return self._reply(503, {"error": "ocupado transcribiendo otra petición"})
                started = time.monotonic()
                try:
                    text = server.transcribe(pcm)
                except SttError as e:
                    log.warning("arthur-voz: la transcripción ha fallado: %s", e)
                    return self._reply(500, {"error": "la transcripción ha fallado"})
                finally:
                    server._stt_lock.release()
                log.info("arthur-voz: transcrito %.1f s de audio en %.1f s",
                         len(pcm) / 2 / SAMPLE_RATE, time.monotonic() - started)
                self._reply(200, {"text": text})

            def _tts(self):
                raw = self._body("application/json", MAX_JSON)
                if raw is None:
                    return
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict):
                    return self._reply(400, {"error": "JSON no válido"})
                text, speed = body.get("text"), body.get("speed", 1.0)
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                    return self._reply(400, {"error": f"«text» debe ser un texto de 1 a {MAX_TEXT} caracteres"})
                if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.5 <= speed <= 2.0:
                    return self._reply(400, {"error": "«speed» debe ser un número entre 0.5 y 2.0"})
                text = speakable(text)  # fuera markdown, emojis y enlaces, como en el portátil
                if not text:
                    return self._reply(400, {"error": "no hay nada que decir en voz alta"})
                started = time.monotonic()
                try:
                    pcm, rate = server.synthesize(text, float(speed))
                except TtsError as e:
                    log.warning("arthur-voz: la síntesis ha fallado: %s", e)
                    return self._reply(500, {"error": "la síntesis de voz ha fallado"})
                log.info("arthur-voz: %d caracteres → %.1f s de voz en %.1f s",
                         len(text), len(pcm) / 2 / rate, time.monotonic() - started)
                self._send(200, "audio/wav", wav_from_pcm(pcm, rate))

            def _reply(self, code: int, payload: dict):
                self._send(code, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode())

            def _send(self, code: int, content_type: str, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        return Handler


# --- arranque (lo usa el contenedor) ------------------------------------------

def read_token(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise VoiceServerError(f"No puedo leer la llave de arthur-voz ({path}): {e}") from e


def main(env: dict[str, str] | None = None, stop: threading.Event | None = None) -> int:
    env = os.environ if env is None else env
    synth = None
    try:
        token = read_token(env.get("ARTHUR_VOZ_TOKEN_FILE", "/run/secrets/voz_token"))
        stt_model = Path(env.get("ARTHUR_VOZ_STT_MODEL", DEFAULT_STT_MODEL))
        tts_model = Path(env.get("ARTHUR_VOZ_TTS_MODEL", DEFAULT_TTS_MODEL))
        check_model(stt_model, env.get("ARTHUR_VOZ_STT_SHA256", DEFAULT_STT_SHA256))
        check_model(tts_model, env.get("ARTHUR_VOZ_TTS_SHA256", DEFAULT_TTS_SHA256))
        transcriber = WhisperTranscriber(
            stt_model, language=env.get("ARTHUR_VOZ_LANGUAGE", "es"),
            binary=env.get("ARTHUR_VOZ_WHISPER", "/usr/local/bin/whisper-cli"),
            prompt=build_prompt(env.get("ARTHUR_VOZ_NAME", "ARTHUR"), []),
            audio_ctx=int(env.get("ARTHUR_VOZ_AUDIO_CTX", "768")),  # frases cortas en la mitad de tiempo (stt.py)
        )
        if warning := transcriber.check():
            raise VoiceServerError(warning)
        synth = PiperSynth(tts_model, python=env.get("ARTHUR_VOZ_PIPER_PYTHON", "/opt/piper/bin/python"))
        synth.warm_up()  # cargar la voz ya, no en la primera frase
        server = VoiceServer(token=token, transcribe=transcriber.transcribe, synthesize=synth.synthesize,
                             host=env.get("ARTHUR_VOZ_HOST", "0.0.0.0"), port=int(env.get("ARTHUR_VOZ_PORT", "8771")))
    except (VoiceServerError, TtsError, ValueError, OSError) as e:
        log.error("arthur-voz no arranca: %s", e)
        if synth is not None:
            synth.close()
        return 1
    server.start()
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        import signal

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())  # docker stop / Ctrl+C
    try:
        stop.wait()
    finally:
        server.stop()
        synth.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
