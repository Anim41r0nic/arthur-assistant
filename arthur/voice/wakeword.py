"""Palabra de activación: «hey arthur» (y, si se activa, la doble palmada).

Vosk (repositorios oficiales, Apache-2.0) escucha el micrófono de forma continua,
EN LOCAL, con una "gramática" que solo admite la frase de activación (todo lo demás
lo descarta como desconocido). No transcribe tus conversaciones ni guarda audio:
solo detecta la frase. Al oírla, avisa (`on_wake`) y ARTHUR pasa a la escucha normal.

No reacciona mientras ARTHUR está ocupado o hablando (`is_idle`), para que no se
active con su propia voz al decir su nombre. Se puede pausar y reanudar.
"""

import json
import logging
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .clap import ClapDetector

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHUNK_BYTES = SAMPLE_RATE // 10 * 2  # 100 ms
COOLDOWN = 3.0                      # segundos mínimos entre dos activaciones
PREROLL_CHUNKS = 10                 # último segundo de audio: por si dices la petición de un tirón
MIN_CONFIDENCE = 0.7                # seguridad mínima de Vosk en CADA palabra de la frase (0-1)
MIN_PHRASE_SECONDS = 0.35           # «hey arthur» dura más que esto: un golpe o un clic, no


def vosk_recognizer(model_path: Path, phrases: list[str] | None):
    """Reconocedor limitado a las frases de activación (o libre, con `phrases=None`, para calibrar)."""
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)  # Vosk es muy hablador: silencio
    if phrases is None:
        return KaldiRecognizer(Model(str(model_path)), SAMPLE_RATE)
    recognizer = KaldiRecognizer(Model(str(model_path)), SAMPLE_RATE, json.dumps([*phrases, "[unk]"]))
    recognizer.SetWords(True)  # seguridad y tiempos de cada palabra, para descartar falsas alarmas
    return recognizer


def phrase_score(result: dict, phrases: list[str]) -> tuple[str, float, float] | None:
    """Si el resultado contiene una frase de activación: (frase, seguridad mínima, duración en s).

    Con la gramática limitada, Vosk intenta encajar CUALQUIER sonido en la frase; la seguridad
    por palabra y la duración permiten distinguir tu voz de un ruido.
    """
    words = result.get("result")
    text = result.get("text", "")
    for phrase in phrases:
        target = phrase.split()
        if not words:  # sin detalle por palabra: solo el texto
            if phrase in text:
                return phrase, 1.0, 1.0
            continue
        said = [w.get("word") for w in words]
        for i in range(len(said) - len(target) + 1):
            if said[i:i + len(target)] == target:
                window = words[i:i + len(target)]
                return phrase, min(w.get("conf", 0.0) for w in window), window[-1]["end"] - window[0]["start"]
    return None


class WakeWordListener:
    def __init__(
        self,
        on_wake: Callable[[], None],
        *,
        is_idle: Callable[[], bool],
        phrase: str = "hey arthur",
        variants: tuple[str, ...] = (),
        min_confidence: float = MIN_CONFIDENCE,
        recognizer_factory: Callable[[], object] | None = None,
        model_path: Path | None = None,
        command: list[str] | None = None,
        clap: "ClapDetector | None" = None,
    ) -> None:
        """`recognizer_factory` y `command` solo se cambian en los tests."""
        self.on_wake = on_wake
        self.is_idle = is_idle
        self.phrase = phrase.lower()
        self.phrases = [self.phrase, *(v.lower() for v in variants)]
        self.min_confidence = min_confidence
        self.recognizer_factory = recognizer_factory or (lambda: vosk_recognizer(model_path, self.phrases))
        self.command = command or ["pw-record", "--raw", "--rate", str(SAMPLE_RATE), "--channels", "1",
                                   "--format", "s16", "-"]
        self.clap = clap  # doble palmada como otra forma de despertar (None = desactivada)
        self.paused = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._last_wake = 0.0
        self.wake_count = 0  # cuántas veces se ha activado (para saber si hubo otra llamada)
        self._levels: deque[float] = deque(maxlen=50)          # últimos 5 s, para medir el ruido de fondo
        self._recent: deque[bytes] = deque(maxlen=PREROLL_CHUNKS)

    @property
    def noise_floor(self) -> float | None:
        """Nivel típico del ruido de fondo de tu habitación en los últimos segundos."""
        if len(self._levels) < 10:
            return None
        ordered = sorted(self._levels)
        return ordered[len(ordered) // 2]  # mediana: el ruido de siempre; «hey arthur» es solo ~1 s de 5

    def preroll(self) -> bytes:
        """El último segundo de audio antes de detectar la frase (se descarta si no se usa)."""
        return b"".join(self._recent)

    @property
    def active(self) -> bool:
        return self._proc is not None and not self.paused.is_set()

    def start(self) -> None:
        recognizer = self.recognizer_factory()  # si falla (sin Vosk, sin modelo), falla aquí, al arrancar
        self._proc = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        threading.Thread(target=self._loop, args=(recognizer,), name="wakeword", daemon=True).start()
        log.info("Palabra de activación activa: «%s»", self.phrase)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc.stdout.close()

    def pause(self) -> None:
        self.paused.set()
        log.info("Palabra de activación en pausa")

    def resume(self) -> None:
        self.paused.clear()
        log.info("Palabra de activación reanudada")

    def _loop(self, recognizer) -> None:
        from .tts import rms_level

        stdout = self._proc.stdout
        while chunk := self._read_chunk(stdout):
            self._recent.append(chunk)
            if self.paused.is_set() or not self.is_idle():
                if self.clap:
                    self.clap.reset()  # sus propios sonidos no cuentan, ni una palmada a medias de antes
                continue  # en pausa, o ARTHUR ocupado/hablando: no escuchamos su propia voz
            self._levels.append(rms_level(chunk))
            if self.clap and self.clap.feed(chunk):
                self._wake("doble palmada")
                continue
            # Solo resultados FINALES (Vosk da la frase por terminada): los parciales son meras suposiciones
            # y con cualquier ruido «adivinaba» la frase.
            if not recognizer.AcceptWaveform(chunk):
                continue
            score = phrase_score(json.loads(recognizer.Result()), self.phrases)
            if score is None:
                continue
            phrase, confidence, duration = score
            if confidence < self.min_confidence or duration < MIN_PHRASE_SECONDS:
                log.info("Descartado «%s» (seguridad %.2f, %.2f s): parece ruido", phrase, confidence, duration)
                continue
            self._wake(f"{phrase}, seguridad {confidence:.2f}, {duration:.2f} s")

    @staticmethod
    def _read_chunk(stdout) -> bytes:
        try:
            return stdout.read(CHUNK_BYTES)
        except (ValueError, OSError):  # stop() ha cerrado el micro: terminamos sin error
            return b""

    def _wake(self, heard: str = "") -> None:
        now = time.monotonic()
        if now - self._last_wake < COOLDOWN:
            return
        self._last_wake = now
        self.wake_count += 1
        log.info("He oído la palabra de activación («%s»)", heard)  # solo puede ser una frase de activación
        threading.Thread(target=self.on_wake, name="wake-turn", daemon=True).start()


class WakeSwitch:
    """Interruptor de la palabra de activación que usan las herramientas.

    Se crea antes que el detector (que solo existe en modo servicio) y se le conecta después.
    """

    def __init__(self) -> None:
        self.listener: WakeWordListener | None = None

    def set(self, enabled: bool) -> str:
        if self.listener is None:
            return "La palabra de activación no está configurada (solo funciona con ARTHUR como servicio)."
        if enabled:
            self.listener.resume()
            return f"Vuelvo a escuchar «{self.listener.phrase}»."
        self.listener.pause()
        return "Dejo de escuchar la palabra de activación. Para reactivarla: Super+Shift+A y «vuelve a escuchar»."
