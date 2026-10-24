"""Importar Shorts ya terminados fuera de Studio (Frutinovelas, 2026-10-17) a la página de revisión.

Los episodios de Frutinovelas se montan aparte (Kokoro + ffmpeg en el portátil). Para verlos en la página, aprobarlos
y que Studio los suba por su camino normal (publish.py, con todas sus comprobaciones), se copian aquí como si los
hubiera montado Studio:

  arthur --studio-importar-short frutinovelas episodio.mp4 --titulo "…" [--descripcion "…"] [--srt subtitulos.srt]

  proyectos/importado-<huella>/importado.json        perfil, título, descripción, etiquetas y de dónde vino
  proyectos/importado-<huella>/salida/corto/video.mp4         la copia (el original no se toca)
                                         miniatura.jpg        un fotograma, solo para la página
                                         control.json         lo que dijo ffprobe (tamaño, duración, códecs)
                                         subtitulos.srt       si se dio (no se sube: YouTube pide un permiso grande)

Reglas:
  - Solo a un perfil que hace cortos (channels.Profile.product == "corto"). A estoicismo no: allí cada corto sale de
    su vídeo largo y lleva sus fuentes.
  - El vídeo debe ser un Short de verdad: vertical 1080×1920, 60 s como mucho, H.264 con sonido AAC. El volumen se
    mide y se apunta, pero no bloquea (no lo montó Studio y Marc ya lo ha visto).
  - La revisión queda «esperando» y marcada como importada (columna `origin`). Nunca se aprueba sola a las 18:00:
    solo con «Aprobar» en la página. Sube en PRIVADO, como todo lo que no es del piloto.
  - Si la publicación del canal está apagada (Frutinovelas: publishing=False), aprobar no sube nada.
  - La carpeta sale de la huella del vídeo: importar dos veces lo mismo no duplica nada, y con textos nuevos la
    revisión nueva sustituye a la que esperaba (como un montaje nuevo).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import channels, montage, shorts
from .montage import MontageError, Runner
from .project import project_dir
from .review import IMPORT_FILE, MAX_DESCRIPTION_BYTES, Review, ReviewBook, ReviewError, outputs

MAX_SECONDS = 60.0              # lo que pidió Marc para un Short; los nuestros salen de menos de 59 s
MAX_TITLE = 100                 # YouTube corta en 100
MAX_SRT = 256 * 1024
VIDEO_CODEC, AUDIO_CODEC = "h264", "aac"
TAGS = ["shorts"]
# YouTube rechaza (400) los títulos y descripciones con < o >: mejor saberlo al importar que al publicar.
FORBIDDEN = re.compile(r"[<>]")


class ImportShortError(Exception):
    """El mensaje se le enseña a Marc tal cual."""


def _profile(name: str) -> channels.Profile:
    try:
        profile = channels.get(name)
    except channels.UnknownProfile as e:
        raise ImportShortError(str(e)) from None
    if profile.product != "corto":
        raise ImportShortError(f"{profile.name} no es un canal de cortos: solo se importan Shorts a "
                               + ", ".join(p.id for p in channels.PROFILES.values() if p.product == "corto"))
    return profile


def _title(text: str) -> str:
    title = " ".join(str(text or "").split())
    if not title:
        raise ImportShortError("Falta el título (--titulo)")
    if len(title) > MAX_TITLE:
        raise ImportShortError(f"El título es demasiado largo ({len(title)} caracteres; máximo {MAX_TITLE})")
    if FORBIDDEN.search(title):
        raise ImportShortError("YouTube no admite < ni > en el título")
    return title


def _description(text: str) -> str:
    # Se respetan los párrafos (las líneas en blanco); solo se quitan espacios sobrantes al final de cada línea.
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    description = "\n".join(lines).strip()
    size = len(description.encode("utf-8"))
    if size > MAX_DESCRIPTION_BYTES:
        raise ImportShortError(f"La descripción ocupa {size} bytes y YouTube admite {MAX_DESCRIPTION_BYTES}")
    if FORBIDDEN.search(description):
        raise ImportShortError("YouTube no admite < ni > en la descripción")
    return description


def _subtitles(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if not path.is_file() or path.stat().st_size > MAX_SRT:
            raise ImportShortError(f"Los subtítulos no están o pasan de {MAX_SRT // 1024} KB: {path}")
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportShortError("Los subtítulos deben ser un .srt de texto (UTF-8)") from None
    except OSError as e:
        raise ImportShortError(f"No puedo leer los subtítulos: {e}") from None


def check_video(video: Path, run: Runner) -> dict:
    """ffprobe (la misma orden que el control de calidad de montage.py) y el volumen. Devuelve el control."""
    try:
        probe = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                                str(video)]) or "{}")
    except (MontageError, ValueError) as e:
        raise ImportShortError(f"ffprobe no entiende el vídeo: {e}") from None
    streams = probe.get("streams", []) if isinstance(probe, dict) else []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    try:
        duration = float(probe.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    problems = []
    if (v.get("width"), v.get("height")) != (shorts.WIDTH, shorts.HEIGHT):
        problems.append(f"tamaño {v.get('width')}x{v.get('height')} en vez de {shorts.WIDTH}x{shorts.HEIGHT} "
                        "(vertical)")
    if v.get("codec_name") != VIDEO_CODEC:
        problems.append(f"vídeo {v.get('codec_name') or 'sin imagen'} en vez de H.264")
    if not a:
        problems.append("no tiene sonido")
    elif a.get("codec_name") != AUDIO_CODEC:
        problems.append(f"sonido {a.get('codec_name')} en vez de AAC")
    if not 0 < duration <= MAX_SECONDS:
        problems.append(f"dura {duration:.1f} s: un Short debe durar {MAX_SECONDS:.0f} s como mucho")
    if problems:
        raise ImportShortError("Este vídeo no vale como Short: " + "; ".join(problems))
    try:
        loudness = run(["ffmpeg", "-v", "info", "-nostats", "-i", str(video), "-map", "0:a",
                        "-af", "ebur128=framelog=quiet", "-f", "null", "-"])
        match = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", loudness)
        lufs = float(match[-1]) if match else None
    except MontageError:
        lufs = None  # solo informativo
    return {"ok": True, "problemas": [], "duracion": round(duration, 2), "lufs": lufs,
            "ancho": v.get("width"), "alto": v.get("height"), "importado": True}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def import_short(book: ReviewBook, profile_name: str, video: Path, title: str, description: str = "",
                 srt: Path | None = None, *, run: Runner | None = None,
                 now=datetime.now) -> Review:
    """Copia un Short terminado a Studio y lo deja «esperando» en la página de revisión de ese canal."""
    run = run or montage.run_ffmpeg  # se busca al llamar: así los tests pueden poner uno falso
    profile = _profile(profile_name)
    title, description = _title(title), _description(description)
    subtitles = _subtitles(srt)
    source = Path(video).expanduser()
    if source.suffix.lower() != ".mp4" or not source.is_file():
        raise ImportShortError(f"No encuentro el vídeo .mp4: {source}")
    source = source.resolve()
    control = check_video(source, run)   # antes de copiar nada: un vídeo que no vale no deja rastro
    digest = _sha(source)

    name = f"importado-{digest[:12]}"
    folder = project_dir(name)
    target, thumb, control_file = outputs(folder, "corto")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or _sha(target) != digest:
        part = target.with_name("video.part.mp4")
        shutil.copyfile(source, part)
        part.replace(target)
    if not thumb.exists():
        try:
            run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(target), "-frames:v", "1", "-q:v", "3",
                 str(thumb)])
        except MontageError as e:
            raise ImportShortError(f"No se ha podido sacar el fotograma para la página: {e}") from None
    if subtitles is not None:
        _write(target.with_name("subtitulos.srt"), subtitles)
    control.update({"video": str(target), "miniatura": str(thumb), "sha256_video": digest})
    _write(control_file, json.dumps(control, ensure_ascii=False, indent=1))
    _write(folder / IMPORT_FILE, json.dumps({
        "perfil": profile.id, "titulo": title, "descripcion": description, "etiquetas": TAGS,
        "origen": str(source), "importado_el": now().replace(microsecond=0).isoformat(),
        "sha256_video": digest, "subtitulos": subtitles is not None,
    }, ensure_ascii=False, indent=1))
    try:
        return book.submit(name, "corto")
    except ReviewError as e:
        raise ImportShortError(str(e)) from None
