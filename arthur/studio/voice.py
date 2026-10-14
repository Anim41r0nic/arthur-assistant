"""Voz y subtítulos de un vídeo (paso 3b): Piper lee el guion frase a frase.

Por qué frase a frase y no la escena entera: así se sabe cuánto dura EXACTAMENTE cada frase, y los subtítulos
salen sincronizados sin tener que transcribir nada (el texto ya lo tenemos: es el guion).

  guion.toml ──► trozos de ≤ 84 caracteres ──► piper (uno por trozo) ──► voz/<huella>.wav
                                                                           │
                    voz/narracion.wav (todo seguido, con pausas) ◄─────────┤
                    voz/linea_tiempo.json (cuándo empieza cada escena) ◄───┤
                    salida/subtitulos.srt ◄────────────────────────────────┘

Cada trozo se guarda con un nombre sacado de su texto y de los ajustes de la voz: si cambias una escena,
solo se vuelve a generar lo que cambió.

Voces (campo `voz` del guion; si falta, la del idioma):
  kokoro:ef_dora (español) y kokoro:af_heart (inglés) → elegidas por Marc; modelo Apache 2.0, en su propio
      entorno (<datos>/studio/kokoro, requirements-kokoro.txt) y un proceso que lo carga una vez.
  piper:es_ES-davefx-medium → CC0, la de ARTHUR (.venv del proyecto, requirements-voz.txt). Suena más a máquina.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .project import Script
from .store import studio_dir

PROJECT_DIR = Path(__file__).resolve().parents[2]
PIPER = PROJECT_DIR / ".venv" / "bin" / "piper"
LENGTH_SCALE = 1.1       # Piper: algo más despacio que ARTHUR hablando; una historia se cuenta con calma
KOKORO_SPEED = 0.95      # Kokoro: lo mismo (la velocidad de las muestras que Marc eligió)
# Huellas del modelo (GitHub thewh1teagle/kokoro-onnx, model-files-v1.0): si el fichero no es ese, no se usa.
KOKORO_FILES = {"kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
                "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"}
# Voces elegidas por Marc (2026-10-12) tras escuchar muestras: Dora (español) y Heart (inglés), de Kokoro.
DEFAULT_VOICES = {"es": "kokoro:ef_dora", "en": "kokoro:af_heart"}
CHUNK_GAP = 0.15         # segundos de silencio entre frases de la misma escena
SCENE_GAP = 0.6          # y entre escenas (coincide con el cambio de imagen)
MAX_CHARS = 84           # dos líneas de subtítulo de 42 caracteres, lo cómodo de leer
LINE_CHARS = 42


class VoiceError(Exception):
    pass


@dataclass(frozen=True)
class Chunk:
    scene: int
    text: str
    path: Path
    start: float
    end: float


@dataclass(frozen=True)
class Narration:
    path: Path
    duration: float
    chunks: list[Chunk]
    scenes: list[tuple[int, float, float]]  # (escena, empieza, acaba) en segundos


# --- trocear el texto ---------------------------------------------------------

def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Frases de como mucho `limit` caracteres: corta por puntos, luego por comas, y si no queda otra, por palabras."""
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?…:;])\s+", text.strip()):
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        current = ""
        for part in re.split(r"(?<=,)\s+", sentence):
            for word in part.split():
                candidate = f"{current} {word}".strip()
                if len(candidate) > limit and current:
                    pieces.append(current)
                    current = word
                else:
                    current = candidate
            if current.endswith(",") and len(current) > limit // 2:
                pieces.append(current)  # una coma es buen sitio para cortar si ya hay bastante
                current = ""
        if current:
            pieces.append(current)
    return [p for p in pieces if p]


def wrap(text: str, width: int = LINE_CHARS) -> str:
    """Parte un subtítulo en dos líneas lo más iguales posible (sin cortar palabras)."""
    if len(text) <= width:
        return text
    words = text.split()
    best, best_diff = text, len(text)
    for i in range(1, len(words)):
        first, second = " ".join(words[:i]), " ".join(words[i:])
        diff = abs(len(first) - len(second))
        if len(first) <= width + 6 and len(second) <= width + 6 and diff < best_diff:
            best, best_diff = f"{first}\n{second}", diff
    return best


# --- motores de voz -------------------------------------------------------------

class PiperEngine:
    """Piper (.venv/bin/piper): una llamada por frase. Lista fija de argumentos, sin shell; el texto va por stdin."""

    def __init__(self, voice: str = "es_ES-davefx-medium", voices_dir: Path | None = None) -> None:
        self.model = (voices_dir or Path("~/.local/share/arthur/voices").expanduser()) / f"{voice}.onnx"
        self.id = f"piper:{voice}|{LENGTH_SCALE}"

    def check(self) -> None:
        if not PIPER.exists():
            raise VoiceError(f"No encuentro Piper en {PIPER} (se instala con requirements-voz.txt)")
        if not self.model.exists():
            raise VoiceError(f"No encuentro la voz {self.model}")

    def speak(self, text: str, output: Path) -> None:
        try:
            subprocess.run([str(PIPER), "-m", str(self.model), "-f", str(output), "--length-scale", str(LENGTH_SCALE)],
                           input=text, text=True, check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError as e:
            raise VoiceError(f"Piper ha fallado: {(e.stderr or '').strip()[-300:]}") from None
        except subprocess.TimeoutExpired:
            raise VoiceError("Piper ha tardado demasiado") from None

    def close(self) -> None:
        pass


class KokoroEngine:
    """Kokoro (Apache 2.0) en su propio entorno: un proceso que carga el modelo UNA vez y lee frase a frase."""

    LANGS = {"es": "es", "en": "en-us"}

    def __init__(self, voice: str, language: str, base: Path | None = None, speed: float = KOKORO_SPEED) -> None:
        self.voice, self.speed = voice, speed
        self.lang = self.LANGS.get(language)
        # En el contenedor del servidor el modelo se monta en otra carpeta y el Python de Kokoro es otro:
        # ARTHUR_KOKORO_MODELS y ARTHUR_KOKORO_PYTHON (los pone Dockerfile.studio).
        self.base = base or Path(os.environ.get("ARTHUR_KOKORO_MODELS") or studio_dir() / "kokoro")
        default_python = self.base / "venv" / "bin" / "python"
        self.python = Path(os.environ.get("ARTHUR_KOKORO_PYTHON") or default_python) if base is None else default_python
        self.id = f"kokoro:{voice}|{self.lang}|{speed}"
        self.worker = Path(__file__).with_name("kokoro_worker.py")
        self.verify = base is None  # el modelo real se comprueba siempre; los tests usan ficheros falsos
        self._proc: subprocess.Popen | None = None

    def check(self) -> None:
        if self.lang is None:
            raise VoiceError(f"Kokoro no tiene configurado el idioma de este guion (hay: {', '.join(self.LANGS)})")
        missing = [p for p in (self.python, self.base / "kokoro-v1.0.onnx",
                               self.base / "voices-v1.0.bin") if not p.exists()]
        if missing:
            raise VoiceError(f"Falta Kokoro ({missing[0]}): mira «Voz Kokoro» en docs/studio.md")
        if self.verify:
            for name, expected in KOKORO_FILES.items():
                digest = hashlib.sha256()
                with (self.base / name).open("rb") as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        digest.update(block)
                if digest.hexdigest() != expected:
                    raise VoiceError(f"{name} no es el modelo esperado (huella distinta): vuelve a descargarlo")

    def _start(self) -> subprocess.Popen:
        if self._proc is None:
            # -I: el Python de Kokoro no mira la carpeta actual ni variables de entorno (no carga nada ajeno).
            env = dict(os.environ)
            if os.environ.get("ARTHUR_KOKORO_TMP"):
                # phonemizer COPIA la librería eSpeak a una carpeta temporal y la carga desde ahí. En el contenedor,
                # /tmp no deja ejecutar nada (noexec): Kokoro usa su propia carpetita, solo para eso.
                env["TMPDIR"] = os.environ["ARTHUR_KOKORO_TMP"]
            self._proc = subprocess.Popen(
                [str(self.python), "-I", str(self.worker),
                 str(self.base)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, env=env)
            self._read()  # espera a que el modelo esté cargado
        return self._proc

    def _read(self) -> dict:
        line = self._proc.stdout.readline() if self._proc and self._proc.stdout else ""
        if not line:
            self.close()
            raise VoiceError("El proceso de Kokoro se ha cerrado (¿falta memoria?)")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise VoiceError(f"Kokoro ha fallado: {reply.get('error')}")
        return reply

    def speak(self, text: str, output: Path) -> None:
        proc = self._start()
        proc.stdin.write(json.dumps({"text": text, "out": str(output), "voice": self.voice, "lang": self.lang,
                                     "speed": self.speed}, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        self._read()

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
            self._proc = None


def engine_for(choice: str, language: str):
    """`choice` = campo `voz` del guion («kokoro:ef_dora», «piper:es_ES-davefx-medium») o vacío (la del idioma)."""
    choice = choice or DEFAULT_VOICES.get(language, "")
    kind, _, name = choice.partition(":")
    if kind == "kokoro" and name:
        return KokoroEngine(name, language)
    if kind == "piper" and name:
        return PiperEngine(name)
    raise VoiceError(f"No hay voz para el idioma «{language}»: pon `voz = \"kokoro:…\"` en el guion")


def narrate(script: Script, folder: Path, engine) -> Narration:
    engine.check()
    voice_dir = folder / "voz"
    voice_dir.mkdir(parents=True, exist_ok=True)
    parts: list[tuple[int, str, Path]] = []
    try:
        for scene in script.escenas:
            for text in split_text(scene.texto):
                name = hashlib.sha256(f"{engine.id}|{text}".encode()).hexdigest()[:16]
                path = voice_dir / f"{name}.wav"
                if not path.exists():  # ya hecho antes con el mismo texto y la misma voz: no se repite
                    tmp = path.with_suffix(".part.wav")
                    engine.speak(text, tmp)
                    tmp.replace(path)
                parts.append((scene.number, text, path))
    finally:
        engine.close()
    return _join(parts, voice_dir / "narracion.wav")


def _join(parts: list[tuple[int, str, Path]], output: Path) -> Narration:
    """Une los trozos en un solo WAV con silencios entre frases y entre escenas, y apunta los tiempos."""
    params = None
    frame_count = 0  # se cuenta en muestras (no en segundos) para que los tiempos no se desvíen
    chunks: list[Chunk] = []
    scenes: dict[int, list[float]] = {}
    tmp = output.with_suffix(".part.wav")
    with wave.open(str(tmp), "wb") as out:
        previous_scene = None
        for scene, text, path in parts:
            with wave.open(str(path), "rb") as piece:
                shape = (piece.getnchannels(), piece.getsampwidth(), piece.getframerate())
                if params is None:
                    params = shape
                    out.setnchannels(shape[0])
                    out.setsampwidth(shape[1])
                    out.setframerate(shape[2])
                elif shape != params:
                    raise VoiceError(f"El trozo {path.name} tiene otro formato de audio")
                frames = piece.readframes(piece.getnframes())
            channels, width, rate = params
            if previous_scene is not None:
                gap = int((SCENE_GAP if scene != previous_scene else CHUNK_GAP) * rate)
                out.writeframes(b"\x00" * gap * width * channels)
                frame_count += gap
            length = len(frames) // (width * channels)
            start, end = frame_count / rate, (frame_count + length) / rate
            chunks.append(Chunk(scene, text, path, round(start, 3), round(end, 3)))
            scenes.setdefault(scene, [start, end])[1] = end
            out.writeframes(frames)
            frame_count += length
            previous_scene = scene
    if params is None:
        tmp.unlink(missing_ok=True)
        raise VoiceError("El guion no tiene texto")
    tmp.replace(output)
    duration = round(frame_count / params[2], 3)
    scene_times = [(n, round(a, 3), round(b, 3)) for n, (a, b) in scenes.items()]
    (output.parent / "linea_tiempo.json").write_text(json.dumps(
        {"duracion": duration, "escenas": scene_times,
         "frases": [{"escena": c.scene, "texto": c.text, "empieza": c.start, "acaba": c.end} for c in chunks]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return Narration(output, duration, chunks, scene_times)


# --- subtítulos -----------------------------------------------------------------

def _stamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(narration: Narration, path: Path) -> Path:
    """Subtítulos SRT (los lee YouTube y también ffmpeg para quemarlos en los cortos verticales)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [f"{n}\n{_stamp(c.start)} --> {_stamp(c.end)}\n{wrap(c.text)}\n"
              for n, c in enumerate(narration.chunks, start=1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path
