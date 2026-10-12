"""Texto a voz (TTS): ARTHUR responde hablando.

Piezas:
  - piper_worker.py, un proceso aparte (en el .venv) que tiene la voz cargada;
  - pw-play (PipeWire), que reproduce el audio por tus altavoces o auriculares.

El audio se envía al reproductor en trocitos de ~50 ms a ritmo real. Así:
  - sabemos el volumen de lo que suena en cada momento → el Orb late con la voz;
  - se puede cortar en cualquier momento (stop) sin esperar a que acabe.
"""

import array
import json
import logging
import math
import re
import shutil
import struct
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("piper_worker.py")
SLICE_SECONDS = 0.05
LEAD_SECONDS = 0.15   # cuánto audio adelantamos al reproductor para que no se entrecorte
MAX_SPOKEN_CHARS = 1_200


class TtsError(Exception):
    pass


def speakable(text: str) -> str:
    """Prepara el texto para leerlo en voz alta: fuera markdown, emojis y enlaces."""
    text = re.sub(r"```.*?```", " (te dejo el código en pantalla) ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "el enlace", text)
    text = re.sub(r"[*_`#>|~]+", "", text)
    # Quitamos emojis y símbolos que la voz leería de forma extraña.
    text = "".join(c for c in text if unicodedata.category(c)[0] in "LNPZ" or c in "\n%€$")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SPOKEN_CHARS:
        cut = text[:MAX_SPOKEN_CHARS].rsplit(". ", 1)[0]
        text = cut + ". El resto te lo dejo en pantalla."
    return text


def rms_level(pcm: bytes) -> float:
    """Volumen de un trozo de audio, de 0 (silencio) a ~1 (muy fuerte)."""
    samples = array.array("h", pcm)
    if not samples:
        return 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return min(1.0, rms * 2.5)  # la voz normal ronda 0,05–0,3 de RMS: lo ampliamos para el Orb


class PiperSpeaker:
    def __init__(
        self,
        voice_model: Path,
        *,
        length_scale: float = 1.0,
        volume: float = 1.0,
        python: Path = PROJECT_DIR / ".venv" / "bin" / "python",
        on_level: Callable[[float], None] | None = None,
        worker_cmd: list[str] | None = None,
        player_cmd: Callable[[int, float], list[str]] | None = None,
    ) -> None:
        """`worker_cmd` y `player_cmd` solo se cambian en los tests (para que no suene nada)."""
        self.voice_model = voice_model
        self.length_scale = length_scale
        self.volume = volume
        self.python = python
        self.on_level = on_level or (lambda level: None)
        self.worker_cmd = worker_cmd or [str(python), str(WORKER), str(voice_model)]
        self.player_cmd = player_cmd or (lambda rate, volume: [
            "pw-play", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16",
            "--volume", f"{volume:.2f}", "-",
        ])
        self._worker: subprocess.Popen | None = None
        self._sample_rate = 22050
        self._player: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def check(self) -> str | None:
        """Devuelve un aviso si falta algo para poder hablar; None si todo está bien."""
        if not self.python.exists():
            return f"No encuentro el entorno de voz ({self.python}). Instálalo con requirements-voz.txt"
        if not self.voice_model.exists():
            return f"No encuentro la voz {self.voice_model}"
        if shutil.which("pw-play") is None:
            return "Falta pw-play (PipeWire) para reproducir audio"
        return None

    # --- proceso auxiliar -------------------------------------------------

    def _ensure_worker(self) -> subprocess.Popen:
        if self._worker and self._worker.poll() is None:
            return self._worker
        if self._worker:  # el anterior murió: cerramos sus tuberías antes de arrancar otro
            _close_pipes(self._worker)
        log.info("Cargando la voz %s", self.voice_model.name)
        self._worker = subprocess.Popen(
            self.worker_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},  # sin nuestras variables
        )
        kind, payload = self._read_message()
        if kind != b"R":
            raise TtsError("El proceso de voz no ha arrancado bien")
        self._sample_rate = json.loads(payload)["sample_rate"]
        return self._worker

    def _read_message(self) -> tuple[bytes, bytes]:
        stdout = self._worker.stdout
        header = stdout.read(5)
        if len(header) < 5:
            raise TtsError("El proceso de voz se ha cerrado inesperadamente")
        length = struct.unpack(">I", header[1:])[0]
        return header[:1], stdout.read(length)

    # --- hablar -----------------------------------------------------------

    def speak(self, text: str) -> None:
        """Lee el texto en voz alta. Bloquea hasta que termina de sonar (o hasta stop())."""
        text = speakable(text)
        if not text:
            return
        with self._lock:
            self._stop.clear()
            worker = self._ensure_worker()
            worker.stdin.write((json.dumps({"text": text, "length_scale": self.length_scale}) + "\n").encode())
            worker.stdin.flush()
            self._player = subprocess.Popen(
                self.player_cmd(self._sample_rate, self.volume),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                self._stream_to_player()
            finally:
                self._finish_player()
                self.on_level(0.0)

    def _stream_to_player(self) -> None:
        bytes_per_slice = int(self._sample_rate * SLICE_SECONDS) * 2
        start = time.monotonic()
        sent_seconds = 0.0
        while True:
            kind, payload = self._read_message()
            if kind == b"E":
                break
            if kind == b"X":
                raise TtsError(payload.decode(errors="replace"))
            if self._stop.is_set():
                continue  # seguimos leyendo para vaciar la frase, pero sin reproducir
            for i in range(0, len(payload), bytes_per_slice):
                if self._stop.is_set():
                    break
                piece = payload[i:i + bytes_per_slice]
                # Ritmo real: no adelantarnos más de LEAD_SECONDS a lo que está sonando.
                wait = start + sent_seconds - LEAD_SECONDS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._player.stdin.write(piece)
                self.on_level(rms_level(piece))
                sent_seconds += len(piece) / 2 / self._sample_rate
        # Esperar a que termine de sonar lo último que enviamos.
        remaining = start + sent_seconds - time.monotonic()
        while remaining > 0 and not self._stop.is_set():
            time.sleep(min(0.05, remaining))
            remaining = start + sent_seconds - time.monotonic()

    def _finish_player(self) -> None:
        player, self._player = self._player, None
        if player is None:
            return
        if self._stop.is_set():
            player.kill()  # cortar ya, sin esperar a lo que quede en el búfer
        try:
            player.stdin.close()
        except BrokenPipeError:
            pass
        try:
            player.wait(timeout=3)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait()

    def stop(self) -> None:
        """Corta la voz al momento (p. ej. si el usuario empieza a hablar)."""
        self._stop.set()

    def close(self) -> None:
        self.stop()
        if self._worker is None:
            return
        _close_pipes(self._worker)  # al cerrar su entrada, el proceso de voz termina solo
        try:
            self._worker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._worker.kill()
            self._worker.wait()


def _close_pipes(proc: subprocess.Popen) -> None:
    for pipe in (proc.stdin, proc.stdout):
        try:
            pipe.close()
        except (BrokenPipeError, OSError):
            pass


def chime(volume: float = 0.4) -> None:
    """Un "ding" corto (dos tonos) para avisar de que ARTHUR te ha oído. Se genera al momento."""
    rate, samples = 22050, bytearray()
    for freq, duration in ((880, 0.07), (1320, 0.09)):
        n = int(rate * duration)
        for i in range(n):
            fade = min(1.0, i / 200, (n - i) / 200)  # sin chasquidos al empezar y acabar
            samples += int(12000 * volume * fade * math.sin(2 * math.pi * freq * i / rate)).to_bytes(2, "little", signed=True)
    try:
        subprocess.run(["pw-play", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16", "-"],
                       input=bytes(samples), timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
