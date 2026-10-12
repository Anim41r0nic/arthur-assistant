"""Voz a texto (STT) con whisper.cpp, en local.

Se usa el programa whisper-cli (paquete oficial whisper-cpp). Para cada frase:
  1. el audio grabado se escribe como WAV en una carpeta temporal privada;
  2. whisper-cli lo transcribe;
  3. la carpeta se borra al momento.

Whisper tiene un fallo conocido: ante silencio o ruido a veces "se inventa"
frases típicas de subtítulos (p. ej. "Subtítulos realizados por la comunidad
de Amara.org"). Por eso descartamos el audio casi silencioso y esas frases.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from .recorder import SAMPLE_RATE
from .tts import rms_level

log = logging.getLogger(__name__)

MIN_SECONDS = 0.4
FULL_AUDIO_CTX = 1500    # las posiciones de whisper para sus 30 s de audio
AUDIO_CTX_MARGIN = 1.5   # segundos de margen al usar un contexto menor
SILENCE_LEVEL = 0.02
# Solo frases típicas de subtítulos de vídeos, que nadie le diría a ARTHUR.
# (Nada genérico como "música": "pon música" es una petición válida.)
HALLUCINATIONS = (
    "subtítulos realizados por", "subtitulado por", "amara.org",
    "suscríbete al canal", "gracias por ver el vídeo", "gracias por ver el video",
)


# Pista inicial para Whisper: las palabras que esperamos oír. Mejora mucho el
# reconocimiento de nombres propios y términos técnicos ("Firefox", "RAM"…).
BASE_PROMPT = (
    "Conversación con {name}, un asistente personal en Linux. Vocabulario: "
    "Firefox, Code::Blocks, Alacritty, Hyprland, RAM, CPU, Docker, PostgreSQL, Python, C++, "
    "terminal, carpeta, archivo, Proyectos, Documentos, portapapeles, volumen, notificación"
)


def build_prompt(assistant_name: str, extra_words: list[str]) -> str:
    words = [w for w in extra_words if w.lower() not in BASE_PROMPT.lower()]
    return BASE_PROMPT.format(name=assistant_name) + "".join(f", {w}" for w in words) + "."


class SttError(Exception):
    pass


def clean_transcript(text: str) -> str:
    # Quita marcas como [Música], (risas) o *aplausos* y espacios sobrantes.
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if any(h in text.lower() for h in HALLUCINATIONS) and len(text) < 80:
        return ""
    return text


def is_silent(pcm: bytes) -> bool:
    if len(pcm) < MIN_SECONDS * SAMPLE_RATE * 2:
        return True
    # Nivel del trozo más fuerte: si nada supera el umbral, no ha hablado nadie.
    step = SAMPLE_RATE // 10 * 2
    return max(rms_level(pcm[i:i + step]) for i in range(0, len(pcm), step)) < SILENCE_LEVEL


class WhisperTranscriber:
    def __init__(
        self,
        model: Path,
        *,
        language: str = "es",
        threads: int | None = None,
        binary: str = "whisper-cli",
        timeout: int = 120,
        prompt: str = "",
        beam_size: int = 1,
        audio_ctx: int = 0,
    ) -> None:
        """`audio_ctx` (0 = desactivado): whisper codifica siempre 30 s de audio (1500 posiciones) aunque hables 3 s,
        y eso es casi todo su tiempo. Con un contexto menor solo codifica el principio: en el servidor (i7 a
        1,3 GHz), 768 lo deja en la mitad sin perder precisión (medido 2026-10-12). Solo se usa si la frase cabe
        con margen; si no, whisper la cortaría, así que va el modo completo."""
        self.audio_ctx = audio_ctx
        self.prompt = prompt
        self.beam_size = beam_size
        self.model = model
        self.language = language
        self.threads = threads or max(1, (os.cpu_count() or 2) // 2)  # núcleos físicos, no hilos
        self.binary = binary
        self.timeout = timeout

    def check(self) -> str | None:
        if shutil.which(self.binary) is None:
            return f"Falta {self.binary} (instálalo con: sudo pacman -S whisper-cpp)"
        if not self.model.exists():
            return f"No encuentro el modelo de reconocimiento {self.model}"
        return None

    def _short_enough(self, pcm: bytes) -> bool:
        if not self.audio_ctx:
            return False
        seconds = len(pcm) / 2 / SAMPLE_RATE
        return seconds <= self.audio_ctx / FULL_AUDIO_CTX * 30 - AUDIO_CTX_MARGIN

    def transcribe(self, pcm: bytes) -> str:
        if is_silent(pcm):
            return ""
        with tempfile.TemporaryDirectory(prefix="arthur-stt-") as tmp:  # carpeta privada (700)
            wav_path = Path(tmp) / "voz.wav"
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm)
            try:
                proc = subprocess.run(
                    [self.binary, "--model", str(self.model), "--file", str(wav_path),
                     "--language", self.language, "--threads", str(self.threads),
                     "--beam-size", str(self.beam_size), "--best-of", str(self.beam_size),
                     "--no-timestamps", "--no-prints",
                     *(["--audio-ctx", str(self.audio_ctx)] if self._short_enough(pcm) else []),
                     *(["--prompt", self.prompt] if self.prompt else [])],
                    capture_output=True, text=True, timeout=self.timeout,
                    env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},  # sin nuestras variables de entorno
                )
            except subprocess.TimeoutExpired as e:
                raise SttError(f"El reconocimiento de voz ha tardado más de {self.timeout} s") from e
            except OSError as e:
                raise SttError(f"No puedo ejecutar {self.binary}: {e}") from e
        if proc.returncode != 0:
            raise SttError(f"whisper-cli ha fallado: {proc.stderr.strip()[-300:]}")
        return clean_transcript(proc.stdout)
