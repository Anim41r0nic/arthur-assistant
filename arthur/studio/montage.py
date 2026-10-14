"""Montaje, miniatura y control de calidad de un vídeo (paso 3c), con ffmpeg.

  linea_tiempo.json + imágenes ──► una escena = una imagen con movimiento suave ──► salida/escenas/<huella>.mp4
                                                                                    │ (se reutilizan si no cambian)
  narracion.wav ──► volumen de YouTube (-14 LUFS) ──► salida/video.mp4 ◄── unir escenas ┘
                                                     salida/miniatura.jpg   (imagen + texto grande)
                                                     salida/descripcion.txt (fuentes y créditos)
                                                     salida/control.json    (lo que se comprobó)

Antes de montar se comprueba OTRA VEZ que cada imagen tiene licencia «ok» en el registro y que el fichero es
el mismo que se descargó (sha256): si no, no se monta (regla de diseño: sin licencia, no hay vídeo).

Imágenes verticales (bustos) en un vídeo horizontal: la imagen entera en el centro y, detrás, ella misma
ampliada, desenfocada y oscurecida, en vez de franjas negras.

ffmpeg se llama siempre con una lista fija de argumentos (sin shell); los textos de la miniatura van en un
fichero (textfile=) para que ningún carácter del título se interprete como orden de ffmpeg.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .project import Script, full_description
from .store import Asset

WIDTH, HEIGHT, FPS = 1920, 1080, 30
TAIL = 1.5             # segundos de imagen tras la última frase (que no se corte de golpe)
FADE = 0.4             # fundido a negro al entrar y salir de cada escena
ZOOM = 0.06            # cuánto se acerca (o se aleja) la cámara en cada escena: 6 %
LUFS = -14             # volumen al que YouTube normaliza: si subes más alto, lo baja él
# Compresión suave de voz (como en un pódcast): baja los picos para que el volumen pueda subir sin distorsionar.
COMPRESS = "acompressor=threshold=-20dB:ratio=3:attack=5:release=100:makeup=2"
THREADS = 2           # núcleos para codificar: con todos, el portátil se calienta y no se puede usar mientras
FILTER_VERSION = "1"   # súbelo si cambias el filtro de escena: así se vuelven a hacer las escenas guardadas
FONTS = (Path("/usr/share/fonts/noto/NotoSerif-Black.ttf"), Path("/usr/share/fonts/noto/NotoSerif-Bold.ttf"),
         Path("/usr/share/fonts/TTF/DejaVuSerif-Bold.ttf"),
         Path("/usr/share/fonts/truetype/noto/NotoSerif-Black.ttf"),    # nombres en Debian (servidor,
         Path("/usr/share/fonts/truetype/noto/NotoSerif-Bold.ttf"),     # paquete fonts-noto-core)
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"))
# Paleta de los canales (kit de marca de Business Lab, aprobado por Marc el 2026-10-13). Formato de color de ffmpeg.
OBSIDIANA, MARMOL, BRONCE = "0x111113", "0xEDE7DB", "0xC29A5B"

Runner = Callable[[list[str]], str]


class MontageError(Exception):
    pass


@dataclass(frozen=True)
class ScenePlan:
    number: int
    image: Path
    start: float
    duration: float


def run_ffmpeg(args: list[str]) -> str:
    """Ejecuta ffmpeg/ffprobe y devuelve lo que escribe (ffprobe en la salida normal, ffmpeg sus medidas en la de errores)."""
    if args[0] == "ffmpeg":
        # Prioridad mínima: si usas el equipo mientras tanto, tú vas primero y el montaje espera.
        args = ["nice", "-n", "19", *args]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=True, timeout=3600)
    except FileNotFoundError:
        raise MontageError(f"No encuentro {args[0]} (en Arch/CachyOS: sudo pacman -S ffmpeg)") from None
    except subprocess.CalledProcessError as e:
        raise MontageError(f"{args[0]} ha fallado: {(e.stderr or '').strip()[-400:]}") from None
    except subprocess.TimeoutExpired:
        raise MontageError(f"{args[0]} ha tardado más de una hora") from None
    return (proc.stdout or "") + (proc.stderr or "")


# --- comprobaciones previas ----------------------------------------------------------

def check_assets(script: Script, assets: list[Asset], folder: Path) -> dict[str, Path]:
    """Imagen de cada escena → fichero, solo si su licencia está «ok» y el fichero no ha cambiado."""
    by_title = {a.title.replace("_", " "): a for a in assets}
    images: dict[str, Path] = {}
    problems = []
    for title in dict.fromkeys([s.imagen for s in script.escenas] + [script.thumbnail_image]):
        asset = by_title.get(title.replace("_", " "))
        if asset is None:
            problems.append(f"{title}: no está descargada (ejecuta --studio-preparar)")
            continue
        if asset.status != "ok":
            problems.append(f"{title}: bloqueada ({asset.reason})")
            continue
        path = folder / "recursos" / asset.name
        if not path.exists() or _sha(path) != asset.sha256:
            problems.append(f"{title}: el fichero ha cambiado o no está; vuelve a ejecutar --studio-preparar")
            continue
        images[title] = path
    if problems:
        raise MontageError("No se monta:\n  " + "\n  ".join(problems))
    return images


def plan(script: Script, timeline: dict, images: dict[str, Path]) -> list[ScenePlan]:
    """Cada imagen se ve desde que empieza su escena hasta que empieza la siguiente (incluida la pausa)."""
    times = {int(n): (float(a), float(b)) for n, a, b in timeline["escenas"]}
    plans = []
    for i, scene in enumerate(script.escenas):
        if scene.number not in times:
            raise MontageError(f"La escena {scene.number} no tiene voz: ejecuta --studio-voz otra vez")
        start = 0.0 if i == 0 else times[scene.number][0]
        if i + 1 < len(script.escenas):
            end = times[script.escenas[i + 1].number][0]
        else:
            end = times[scene.number][1] + TAIL
        plans.append(ScenePlan(scene.number, images[scene.imagen], round(start, 3), round(end - start, 3)))
    return plans


# --- escenas, vídeo y miniatura ----------------------------------------------------------

def scene_filter(duration: float, zoom_in: bool, width: int = WIDTH, height: int = HEIGHT) -> str:
    """`width`×`height`: 1920×1080 para los vídeos largos, 1080×1920 para los cortos verticales (shorts.py)."""
    frames = max(1, round(duration * FPS))
    grow = f"{ZOOM}*on/{frames}"
    zoom = f"1+{grow}" if zoom_in else f"{1 + ZOOM}-{grow}"
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"boxblur=40:4,eq=brightness=-0.25[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        # Se trabaja al doble y se reduce al final: así el zoom no tiembla (zoompan redondea a píxeles enteros).
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,scale={width * 2}:{height * 2},"
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={FPS},"
        f"fade=t=in:st=0:d={FADE},fade=t=out:st={max(0.0, duration - FADE):.3f}:d={FADE},format=yuv420p"
    )


def render_scene(scene: ScenePlan, out_dir: Path, run: Runner, zoom_in: bool,
                 width: int = WIDTH, height: int = HEIGHT) -> Path:
    size = "" if (width, height) == (WIDTH, HEIGHT) else f"|{width}x{height}"  # las horizontales ya guardadas valen
    key = hashlib.sha256(f"{FILTER_VERSION}|{_sha(scene.image)}|{scene.duration}|{zoom_in}{size}"
                         .encode()).hexdigest()[:16]
    target = out_dir / f"{key}.mp4"
    if target.exists():
        return target  # misma imagen, misma duración, mismo filtro: ya está hecha
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"{key}.part.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-t", f"{scene.duration:.3f}",
         "-i", str(scene.image), "-filter_complex", scene_filter(scene.duration, zoom_in, width, height),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS), "-an",
         "-threads", str(THREADS), "-filter_complex_threads", str(THREADS), str(tmp)])
    tmp.replace(target)
    return target


def measure_loudness(narration: Path, run: Runner) -> dict[str, str]:
    """Primera pasada de loudnorm: mide la voz. En una sola pasada, una narración con muchas pausas se queda
    corta (el primer vídeo con Dora salió a -15,8 LUFS en vez de -14)."""
    text = run(["ffmpeg", "-v", "info", "-nostats", "-i", str(narration),
                "-af", f"{COMPRESS},loudnorm=I={LUFS}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    found = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text)
    if not found:
        raise MontageError("ffmpeg no ha devuelto la medida del volumen")
    data = json.loads(found.group(0))
    return {k: str(data[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}


def assemble(clips: list[Path], narration: Path, output: Path, run: Runner) -> Path:
    """Une las escenas sin volver a codificar el vídeo y pone la voz al volumen de YouTube (dos pasadas)."""
    m = measure_loudness(narration, run)
    listing = output.parent / "escenas.txt"
    # Nombres generados por nosotros (huellas hex): no hay comillas ni caracteres raros que escapar.
    listing.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    tmp = output.with_suffix(".part.mp4")
    loudnorm = (f"loudnorm=I={LUFS}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
                f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
                "linear=true")
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(narration),
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", f"{COMPRESS},{loudnorm},apad", "-shortest",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(tmp)])
    tmp.replace(output)
    return output


def thumbnail(image: Path, text: str, output: Path, run: Runner) -> Path:
    font = next((f for f in FONTS if f.exists()), None)
    if font is None:
        raise MontageError("No encuentro una letra para la miniatura (instala noto-fonts)")
    lines = _thumb_lines(text.upper())
    # Franja obsidiana con un filo de bronce arriba. Con dos líneas, la segunda va en bronce (el acento:
    # como mucho uno por miniatura); cada línea en su fichero porque drawtext no cambia de color a media frase.
    files = [output.with_name(f"{output.stem}-{n}.txt") for n in range(len(lines))]
    for path, line in zip(files, lines):
        path.write_text(line, encoding="utf-8")
    text = "drawtext=fontfile={font}:textfile={file}:fontsize={size}:fontcolor={color}:borderw=6:bordercolor=black:x=(w-text_w)/2:y={y}"
    if len(lines) == 1:
        texts = [text.format(font=font, file=files[0], size=120, color="white", y="h*0.70+(h*0.30-text_h)/2")]
    else:  # dos líneas caben en la franja de abajo, una encima y otra debajo del centro
        texts = [text.format(font=font, file=files[0], size=84, color="white", y="h*0.85-text_h-4"),
                 text.format(font=font, file=files[1], size=84, color=BRONCE, y="h*0.85+4")]
    tmp = output.with_suffix(".part.jpg")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(image), "-frames:v", "1", "-vf",
         "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720:(iw-1280)/2:0,eq=brightness=-0.12:saturation=1.1,"
         f"drawbox=x=0:y=ih*0.70:w=iw:h=ih*0.30:color={OBSIDIANA}@0.75:t=fill,"
         f"drawbox=x=0:y=ih*0.70:w=iw:h=6:color={BRONCE}:t=fill,"
         + ",".join(texts),
         "-q:v", "2", str(tmp)])
    tmp.replace(output)
    for path in files:
        path.unlink(missing_ok=True)
    return output


def _thumb_lines(text: str, width: int = 16) -> list[str]:
    """Como mucho dos líneas cortas: una miniatura se lee en un segundo o no se lee."""
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    return lines[:2]


# --- control de calidad ---------------------------------------------------------------

def quality_check(video: Path, expected: float, run: Runner, width: int = WIDTH, height: int = HEIGHT) -> dict:
    probe = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                            str(video)]) or "{}")
    streams = probe.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float(probe.get("format", {}).get("duration", 0) or 0)
    loudness = run(["ffmpeg", "-v", "info", "-nostats", "-i", str(video), "-map", "0:a",
                    "-af", "ebur128=framelog=quiet", "-f", "null", "-"])
    match = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", loudness)
    lufs = float(match[-1]) if match else None
    problems = []
    if (v.get("width"), v.get("height")) != (width, height):
        problems.append(f"tamaño {v.get('width')}x{v.get('height')} en vez de {width}x{height}")
    if not a:
        problems.append("no tiene sonido")
    if abs(duration - expected) > 1.0:
        problems.append(f"dura {duration:.1f} s y debería durar {expected:.1f} s")
    if lufs is None or abs(lufs - LUFS) > 1.5:
        problems.append(f"volumen {lufs} LUFS (debería estar cerca de {LUFS})")
    return {"ok": not problems, "problemas": problems, "duracion": round(duration, 2), "lufs": lufs,
            "ancho": v.get("width"), "alto": v.get("height")}


# --- todo junto -------------------------------------------------------------------------

def produce(script: Script, assets: list[Asset], folder: Path, run: Runner = run_ffmpeg,
            progress: Callable[[str], None] = lambda _: None) -> dict:
    images = check_assets(script, assets, folder)
    timeline_path = folder / "voz" / "linea_tiempo.json"
    narration = folder / "voz" / "narracion.wav"
    if not timeline_path.exists() or not narration.exists():
        raise MontageError("Falta la voz: ejecuta --studio-voz primero")
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    scenes = plan(script, timeline, images)
    out = folder / "salida"
    clips = []
    for i, scene in enumerate(scenes):
        progress(f"escena {scene.number} de {len(scenes)} ({scene.duration:.1f} s)")
        clips.append(render_scene(scene, out / "escenas", run, zoom_in=i % 2 == 0))
    progress("uniendo escenas y voz")
    video = assemble(clips, narration, out / "video.mp4", run)
    progress("miniatura")
    thumb = thumbnail(images[script.thumbnail_image], script.thumbnail_text, out / "miniatura.jpg", run)
    (out / "descripcion.txt").write_text(full_description(script, assets), encoding="utf-8")
    progress("control de calidad")
    expected = sum(s.duration for s in scenes)
    report = quality_check(video, expected, run)
    report.update({"video": str(video), "miniatura": str(thumb), "sha256_video": _sha(video)})
    (out / "control.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
