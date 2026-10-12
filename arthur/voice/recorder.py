"""Grabación del micrófono para hablar con ARTHUR (pulsar para hablar).

El micrófono SOLO se abre entre start() y stop(). El audio se guarda en memoria
(nunca en disco) y se descarta en cuanto se ha transcrito.

Usamos pw-record (PipeWire) en modo "raw": envía muestras de 16 bits a 16 kHz
en mono, que es exactamente el formato que necesita Whisper.
"""

import logging
import subprocess
import threading
import time
from collections.abc import Callable

from .tts import rms_level

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHUNK_BYTES = SAMPLE_RATE // 20 * 2  # 50 ms de audio
SPEECH_LEVEL = 0.05                  # umbral mínimo de voz (en una habitación silenciosa)
NOISE_CHUNKS = 6                     # los primeros 300 ms sirven para medir el ruido de fondo
NOISE_FACTOR = 2.5                   # hay voz si el nivel supera el ruido de fondo × este factor
MAX_SPEECH_LEVEL = 0.2               # tope si el ruido se mide aquí (puede colarse tu voz o el ding)
MAX_KNOWN_LEVEL = 0.5                # tope si el ruido ya viene medido por el detector (medida fiable)
VOICE_CHUNKS = 2                     # voz = 2 trozos seguidos (100 ms) por encima: un pico suelto no cuenta


class RecorderError(Exception):
    pass


class Recorder:
    def __init__(
        self,
        *,
        max_seconds: int = 60,
        on_level: Callable[[float], None] | None = None,
        command: list[str] | None = None,
    ) -> None:
        """`command` solo se cambia en los tests (para no abrir el micrófono de verdad)."""
        self.max_bytes = max_seconds * SAMPLE_RATE * 2
        self.on_level = on_level or (lambda level: None)
        self.command = command or [
            "pw-record", "--raw", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "-",
        ]
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._buffer = bytearray()
        self.truncated = False
        # Detección de voz (para "manos libres"): cuándo empezaste a hablar y cuándo se oyó voz por última vez.
        self._started_at = 0.0
        self._first_voice: float | None = None
        self._last_voice = 0.0

    @property
    def recording(self) -> bool:
        return self._proc is not None

    def start(self, *, initial: bytes = b"", noise_floor: float | None = None, ignore_seconds: float = 0.0) -> None:
        """`initial`: audio previo (p. ej. el último segundo antes de «Hey Arthur»).
        `noise_floor`: ruido de fondo ya medido (si no, se mide en los primeros 300 ms).
        `ignore_seconds`: no contar como voz el principio (p. ej. el ding)."""
        if self._proc:
            raise RecorderError("Ya estoy grabando")
        self._buffer = bytearray(initial)
        self.truncated = False
        self._started_at, self._first_voice, self._last_voice = time.monotonic(), None, 0.0
        self._ignore_until = self._started_at + ignore_seconds
        self._noise_levels: list[float] = []
        self._noise_known = noise_floor is not None
        self.noise_floor = noise_floor
        self.speech_level = self._threshold(noise_floor, known=True) if self._noise_known else SPEECH_LEVEL
        self.peak = 0.0
        self._loud_run = 0
        try:
            self._proc = subprocess.Popen(self.command, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise RecorderError(f"No puedo abrir el micrófono: {e}") from e
        self._thread = threading.Thread(target=self._read, name="recorder", daemon=True)
        self._thread.start()
        log.info("Micrófono abierto")

    def _read(self) -> None:
        stdout = self._proc.stdout
        while chunk := stdout.read(CHUNK_BYTES):
            if len(self._buffer) >= self.max_bytes:
                self.truncated = True  # límite de seguridad: seguimos vaciando pero no guardamos
                continue
            self._buffer += chunk
            level = rms_level(chunk)
            # Umbral adaptativo: el ruido de fondo de TU habitación (ventilador, calle…) no cuenta como voz.
            # Tomamos el mínimo de los primeros trozos: aunque empieces a hablar enseguida, el mínimo es el fondo.
            if not self._noise_known and len(self._noise_levels) < NOISE_CHUNKS:
                self._noise_levels.append(level)
                self.noise_floor = min(self._noise_levels)
                self.speech_level = self._threshold(self.noise_floor)
            if time.monotonic() < self._ignore_until:
                self._loud_run = 0
            elif level >= self.speech_level:
                self._loud_run += 1
                self.peak = max(self.peak, level)
                if self._loud_run >= VOICE_CHUNKS:  # un golpe o pico de ruido suelto no es voz
                    now = time.monotonic()
                    self._first_voice = self._first_voice or now
                    self._last_voice = now
            else:
                self._loud_run = 0
            self.on_level(level)

    def ignore_for(self, seconds: float) -> None:
        """A partir de ahora, ignorar solo `seconds` más (p. ej. el eco del ding, que ya ha terminado)."""
        self._ignore_until = time.monotonic() + seconds

    @staticmethod
    def _threshold(noise: float, *, known: bool = False) -> float:
        return min(MAX_KNOWN_LEVEL if known else MAX_SPEECH_LEVEL, max(SPEECH_LEVEL, noise * NOISE_FACTOR))

    def wait_for_end(self, *, silence: float = 1.0, no_speech: float = 5.0, max_seconds: float = 15.0) -> bool:
        """Espera a que termines de hablar: `silence` s callado tras haber hablado.

        Devuelve False si no llegaste a hablar en `no_speech` s. Corta como mucho en `max_seconds`.
        """
        while self._proc is not None:
            now = time.monotonic()
            if self._first_voice is None:
                if now - self._started_at > no_speech:
                    return False
            elif now - self._last_voice > silence or now - self._started_at > max_seconds:
                return True
            time.sleep(0.05)
        return self._first_voice is not None

    def stop(self) -> bytes:
        """Cierra el micrófono y devuelve el audio grabado (PCM 16 bits, 16 kHz, mono)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return b""
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self._thread.join(timeout=3)
        proc.stdout.close()
        self.on_level(0.0)
        log.info("Micrófono cerrado (%.1f s grabados; ruido %.3f, umbral de voz %.3f, pico %.3f)",
                 len(self._buffer) / 2 / SAMPLE_RATE, self.noise_floor or 0.0, self.speech_level, self.peak)
        audio, self._buffer = bytes(self._buffer), bytearray()
        return audio
