"""Montaje directo de un Short de Frutinovelas desde fotos y voces WAV ya preparadas (2026-10-17).

  guion.json (8–12 escenas) ──► [con catálogo] validar_guion: plano + gesto + objetos que se ven de verdad
        │                                     (si falla, no se monta nada y se dice por qué)
        ├─ foto de cada escena ──► clip vertical 1080×1920 con acercamiento de cámara y su subtítulo
        └─ voz de cada escena  ──► una pista con cada frase en su sitio ──► volumen de YouTube (-14 LUFS)
                                                                         │
  destino/video.mp4  ◄── unir clips + voz (montage.assemble) ◄───────────┘
          subtitulos.srt   (las mismas frases, para --srt al importar)
          control.json     (lo que se comprobó: tamaño, códecs, duración, volumen, escenas)

Por qué aparte de shorts.py: aquel recorta el vídeo largo de estoicismo y le pone un rótulo «historia completa»;
Frutinovelas es un Short directo, sin vídeo largo. Este módulo NO elige recursos, no genera voces, no publica y
no está conectado al piloto automático: deja el MP4 listo para `arthur --studio-importar-short frutinovelas …`,
que lo pone «esperando» en la página (y con publishing=False, aprobar no sube nada).

Las fotos solo reciben movimiento de cámara (acercar, alejar, barrido, empuje): no se les atribuye movimiento
del cuerpo.

Seguridad:
  - ffmpeg se llama con listas fijas de argumentos (sin shell) y por montage.run_ffmpeg (prioridad mínima).
  - Las rutas del guion se resuelven (enlaces incluidos) y deben quedar DENTRO de la carpeta del guion o, con
    catálogo, de su `raiz_recursos`: un guion escrito por un modelo no puede leer ficheros de otro sitio.
  - Cada foto debe empezar como un PNG/JPEG/WebP de verdad y se abre con el lector de imágenes de ffmpeg
    (`-f image2 -pattern_type none`): un «.png» que en realidad sea una lista de reproducción no hace que ffmpeg
    lea otros ficheros ni la red, y un «%» en el nombre no se toma como serie de imágenes.
  - Los textos van a ffmpeg en ficheros (textfile=, sin expansión), nunca dentro del filtro; las rutas que sí
    van dentro del filtro se comprueban con una lista cerrada de caracteres.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from . import importer, montage, shorts
from .frutas_validation import MAX_ESCENAS, MIN_ESCENAS, validar_guion
from .montage import FONTS, MontageError, Runner

WIDTH, HEIGHT, FPS = shorts.WIDTH, shorts.HEIGHT, 24   # 24 fps como los episodios privados ya montados
VOICE_DELAY = 0.35     # silencio antes de cada frase: el corte de plano se ve antes de oír la voz
VOICE_TAIL = 0.2       # y un poco después, para que la frase no se pise con el plano siguiente
MAX_VOICE = 3.35
MIN_SCENE, MAX_SCENE = 2, 6
MAX_TEXT = 70
MAX_TOTAL = importer.MAX_SECONDS   # lo que importer acepta como Short
DURATION_SLACK = 0.25  # la duración real puede variar un fotograma o dos por el AAC
ZOOM = 0.06            # el mismo 6 % que el montaje de Studio
PUSH = 0.15            # «empuje»: acercamiento rápido para el momento de la sorpresa
# Movimientos de cámara (opción 2 de Marc, 18-oct-2026): solo la cámara se mueve sobre la foto fija. Lista
# cerrada: el nombre viene de un guion escrito por un modelo y nunca entra en el filtro de ffmpeg tal cual.
MOVES = ("acercar", "alejar", "barrido_izq", "barrido_der", "empuje")
DEFAULT_MOVE = "acercar"
SUBTITLE_WIDTH, SUBTITLE_LINES = 25, 3
# Primeros bytes de cada formato: la extensión sola no dice qué es el fichero.
IMAGE_MAGIC = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
               ".webp": (b"RIFF",)}
SAFE_FILTER_PATH = re.compile(r"/[A-Za-z0-9_./-]+")


class FrutasError(ValueError):
    """El material o el resultado no permite un Short válido. El mensaje se le enseña a Marc tal cual."""


@dataclass(frozen=True)
class Scene:
    image: Path
    voice: Path
    text: str
    duration: float
    voice_duration: float
    plano: str | None = None
    movimiento: str = DEFAULT_MOVE


# --- comprobaciones previas (sin ffmpeg) ----------------------------------------------------

def _resource(value: object, kind: str, base: Path, roots: list[Path]) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FrutasError(f"Falta la ruta de {kind}")
    path = Path(value).expanduser()
    # resolve() sigue los enlaces simbólicos: un enlace dentro de la carpeta que apunte fuera también se rechaza.
    path = (path if path.is_absolute() else base / path).resolve()
    if not any(path.is_relative_to(root) for root in roots):
        raise FrutasError(f"La {kind} está fuera de las carpetas permitidas: {path}")
    if not path.is_file():
        raise FrutasError(f"No existe {kind}: {path}")
    return path


def _check_image(path: Path, number: int) -> None:
    magic = IMAGE_MAGIC.get(path.suffix.lower())
    if magic is None:
        raise FrutasError(f"Escena {number}: se admiten fotos PNG, JPG y WebP; no vídeo")
    try:
        with path.open("rb") as f:
            head = f.read(12)
    except OSError as e:
        raise FrutasError(f"Escena {number}: no puedo leer la foto: {e}") from None
    if not head.startswith(magic) or (path.suffix.lower() == ".webp" and head[8:12] != b"WEBP"):
        raise FrutasError(f"Escena {number}: {path.name} no es de verdad una imagen {path.suffix.lower()[1:].upper()}")


def _voice_seconds(path: Path) -> float:
    # El módulo wave lee la cabecera RIFF/WAVE: si no es un WAV PCM, falla aquí y no en ffmpeg a medio montaje.
    try:
        with wave.open(str(path), "rb") as source:
            if source.getcomptype() != "NONE" or source.getnframes() == 0:
                raise FrutasError(f"La voz debe ser WAV PCM con sonido: {path}")
            return source.getnframes() / source.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError, OSError) as exc:
        raise FrutasError(f"La voz no es WAV PCM válido: {path}") from exc


def _wrapped(text: str, width: int = SUBTITLE_WIDTH) -> str:
    """Corta el subtítulo en líneas de 25 caracteres como mucho: más ancho no cabe en vertical con letra grande."""
    lines, current = [], ""
    for word in text.split():
        if len(word) > width:
            raise FrutasError(f"Palabra demasiado larga para subtítulo: {word}")
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    if len(lines) > SUBTITLE_LINES:
        raise FrutasError("El subtítulo ocupa más de tres líneas")
    return "\n".join(lines)


def _catalog_root(catalog: dict) -> Path:
    root = catalog.get("raiz_recursos") if isinstance(catalog, dict) else None
    if not isinstance(root, str) or not root.strip():
        raise FrutasError("El catálogo no dice dónde están las fotos (raiz_recursos)")
    path = Path(root).expanduser()
    if not path.is_absolute():
        raise FrutasError("raiz_recursos del catálogo debe ser una ruta absoluta (o empezar por ~)")
    return path.resolve()


def load_catalog(path: Path) -> dict:
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FrutasError(f"No se puede leer el catálogo JSON: {exc}") from None
    if not isinstance(data, dict):
        raise FrutasError("El catálogo debe ser un objeto JSON")
    return data


def load_manifest(path: Path, catalog: dict | None = None, *, max_voice: float = MAX_VOICE) -> list[Scene]:
    """Valida 8–12 escenas, tiempos, fotos, voces y subtítulos antes de usar ffmpeg.

    Sin catálogo, cada escena da su `imagen`. Con catálogo, cada escena da su `plano` (y gesto, objetos, hechos…)
    y pasa antes por validar_guion; la foto sale del `archivo` del plano y no se puede elegir otra a mano: así el
    texto no puede afirmar un plano mientras enseña una foto distinta.

    `max_voice`: la frase más larga admitida. 3,35 s para las escenas de 4 s de los episodios a mano; el diario
    (frutas_diario) admite frases algo más largas en escenas de hasta 6 s.
    """
    path = Path(path).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FrutasError(f"No se puede leer el guion JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("escenas"), list):
        raise FrutasError("El guion necesita una lista 'escenas'")
    if not MIN_ESCENAS <= len(data["escenas"]) <= MAX_ESCENAS:
        raise FrutasError(f"El Short necesita entre {MIN_ESCENAS} y {MAX_ESCENAS} escenas")
    base = path.parent
    planos: dict[str, dict] = {}
    root = base
    if catalog is not None:
        result = validar_guion(data, catalog)
        if not result.valido:
            raise FrutasError("El guion no pasa la validación editorial:\n- " + "\n- ".join(result.motivos))
        root = _catalog_root(catalog)
        planos = {p["id"]: p for p in catalog["planos"]}   # validar_guion ya comprobó su forma
    scenes = []
    for number, item in enumerate(data["escenas"], 1):
        if not isinstance(item, dict):
            raise FrutasError(f"Escena {number}: formato incorrecto")
        if catalog is not None:
            if "imagen" in item:
                raise FrutasError(f"Escena {number}: con catálogo, la foto sale del plano; quita «imagen»")
            image = _resource(planos[item["plano"]].get("archivo"), "imagen", root, [root])
        else:
            image = _resource(item.get("imagen"), "imagen", base, [base])
        _check_image(image, number)
        # Las voces son de este episodio: siempre junto al guion.
        voice = _resource(item.get("voz"), "voz", base, [base])
        if voice.suffix.lower() != ".wav":
            raise FrutasError(f"Escena {number}: la voz debe ser un .wav")
        text = item.get("texto")
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT
                or any(ord(char) < 32 or ord(char) == 127 for char in text)):
            raise FrutasError(f"Escena {number}: diálogo de 1 a {MAX_TEXT} caracteres en una línea")
        _wrapped(text.strip())
        duration = item.get("duracion", 4)
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or not MIN_SCENE <= duration <= MAX_SCENE):
            raise FrutasError(f"Escena {number}: duración entre {MIN_SCENE} y {MAX_SCENE} segundos")
        seconds = _voice_seconds(voice)
        if seconds > max_voice or seconds > duration - VOICE_DELAY - VOICE_TAIL:
            raise FrutasError(f"Escena {number}: voz de {seconds:.2f} s demasiado larga")
        move = item.get("movimiento", DEFAULT_MOVE)
        if move not in MOVES:
            raise FrutasError(f"Escena {number}: movimiento de cámara desconocido (hay: {', '.join(MOVES)})")
        scenes.append(Scene(image, voice, text.strip(), float(duration), seconds,
                            item.get("plano") if catalog is not None else None, move))
    if sum(scene.duration for scene in scenes) > MAX_TOTAL:
        raise FrutasError(f"El Short supera {MAX_TOTAL:.0f} segundos")
    return scenes


# --- montaje ----------------------------------------------------------------------------------

def _timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _safe_filter_path(path: Path) -> str:
    # Dentro de un filtro de ffmpeg, «:», «,», «'» o «\» cambian el significado. Las rutas que van ahí (la letra y
    # los ficheros temporales con nombres nuestros) se aceptan solo con caracteres que no hay que escapar.
    if not SAFE_FILTER_PATH.fullmatch(str(path)):
        raise FrutasError(f"Ruta incompatible con el filtro de ffmpeg: {path}")
    return str(path)


def _camera(move: str, frames: int) -> str:
    """Las expresiones de zoompan de cada movimiento. Todo se calcula con el número de fotograma (on)."""
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if move == "alejar":
        zoom, x = f"1+{ZOOM}*(1-on/{frames})", center_x
    elif move == "empuje":
        zoom, x = f"1+{PUSH}*on/{frames}", center_x
    elif move in ("barrido_izq", "barrido_der"):
        # Acercado fijo y la ventana se desliza de un lado a otro de la foto (de derecha a izquierda o al revés).
        progress = f"on/{frames}" if move == "barrido_der" else f"(1-on/{frames})"
        zoom, x = f"1+{ZOOM}", f"(iw-iw/zoom)*{progress}"
    elif move == "acercar":
        zoom, x = f"1+{ZOOM}*on/{frames}", center_x
    else:
        raise FrutasError(f"Movimiento de cámara desconocido: {move!r}")
    return f"zoompan=z='{zoom}':x='{x}':y='{center_y}'"


def scene_filter(duration: float, font: Path, subtitle: Path, move: str = DEFAULT_MOVE) -> str:
    """Foto entera en vertical, movimiento de cámara (por defecto, acercamiento lento HACIA EL CENTRO) y el
    subtítulo mientras suena la frase."""
    frames = max(1, round(duration * FPS))
    fade_end = max(0.0, duration - VOICE_TAIL)
    return (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        # Como montage.scene_filter: el zoom se calcula con el número de fotograma (on). «zoom+paso» no avanza
        # con una foto en bucle (cada fotograma de entrada vuelve a empezar): el primer montaje salía quieto.
        # Sin x/y, zoompan se acerca a la esquina de arriba a la izquierda: se fija el centro. Se trabaja al
        # doble de tamaño para que el acercamiento no tiemble (zoompan redondea a píxeles enteros).
        f"scale={WIDTH * 2}:{HEIGHT * 2},"
        f"{_camera(move, frames)}"
        f":d=1:s={WIDTH}x{HEIGHT}:fps={FPS},"
        f"drawtext=fontfile={_safe_filter_path(font)}:textfile={_safe_filter_path(subtitle)}:"
        "fontsize=58:fontcolor=white:borderw=5:bordercolor=black:expansion=none:"
        f"x=(w-text_w)/2:y=h-text_h-170:enable='between(t,{VOICE_DELAY},{fade_end:.3f})',"
        "format=yuv420p"
    )


def _render_clip(scene: Scene, index: int, tmp: Path, font: Path, run: Runner) -> Path:
    subtitle = tmp / f"sub-{index:02}.txt"
    subtitle.write_text(_wrapped(scene.text), encoding="utf-8")
    clip = tmp / f"clip-{index:02}.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "image2", "-pattern_type", "none", "-loop", "1",
         "-framerate", str(FPS), "-i", str(scene.image), "-frames:v", str(round(scene.duration * FPS)),
         "-vf", scene_filter(scene.duration, font, subtitle, scene.movimiento),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(FPS),
         "-threads", str(montage.THREADS), "-an", str(clip)])
    return clip


def _mix_voices(scenes: list[Scene], total: float, out: Path, run: Runner) -> Path:
    """Una sola pista con cada frase en su momento; el volumen se ajusta luego en montage.assemble."""
    inputs, delays, labels, elapsed = [], [], [], 0.0
    for index, scene in enumerate(scenes):
        inputs += ["-f", "wav", "-i", str(scene.voice)]
        delays.append(f"[{index}:a]adelay={round((elapsed + VOICE_DELAY) * 1000)}:all=1[a{index}]")
        labels.append(f"[a{index}]")
        elapsed += scene.duration
    graph = ";".join(delays + ["".join(labels) + f"amix=inputs={len(scenes)}:duration=longest:normalize=0,"
                               f"apad,atrim=duration={total:.3f}[a]"])
    run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[a]",
         "-ar", "48000", "-c:a", "pcm_s16le", str(out)])
    return out


def _subtitles(scenes: list[Scene]) -> str:
    blocks, elapsed = [], 0.0
    for index, scene in enumerate(scenes, 1):
        start = elapsed + VOICE_DELAY
        end = min(elapsed + scene.duration - VOICE_TAIL, start + scene.voice_duration + 0.15)
        blocks.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{_wrapped(scene.text)}")
        elapsed += scene.duration
    return "\n\n".join(blocks) + "\n"


def produce(manifest: Path, destination: Path, *, catalog: dict | None = None, run: Runner | None = None,
            max_voice: float = MAX_VOICE) -> dict:
    """Monta el Short y lo comprueba (como lo comprobará importer) antes de dejarlo en destino."""
    run = run or montage.run_ffmpeg   # se busca al llamar: así los tests ponen uno falso
    scenes = load_manifest(manifest, catalog, max_voice=max_voice)
    font = next((f for f in FONTS if f.is_file()), None)
    if font is None:
        raise FrutasError("Falta una letra local para los subtítulos (instala noto-fonts)")
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    video, srt, control_file = destination / "video.mp4", destination / "subtitulos.srt", destination / "control.json"
    if video.exists():
        raise FrutasError(f"Ya existe el vídeo: {video}")
    expected = sum(scene.duration for scene in scenes)
    try:
        with tempfile.TemporaryDirectory(prefix="frutas-directo-") as tmp_name:
            tmp = Path(tmp_name).resolve()
            _safe_filter_path(tmp)   # también va en la lista de clips de concat: mejor fallar antes de montar
            clips = [_render_clip(scene, index, tmp, font, run) for index, scene in enumerate(scenes, 1)]
            voices = _mix_voices(scenes, expected, tmp / "voces.wav", run)
            part = montage.assemble(clips, voices, tmp / "video.mp4", run)
            control = importer.check_video(part, run)
            if abs(control["duracion"] - expected) > DURATION_SLACK:
                raise FrutasError(f"Duración final {control['duracion']:.2f} s; se esperaban {expected:.2f} s")
            srt.write_text(_subtitles(scenes), encoding="utf-8")
            _copy(part, video)
    except MontageError as e:
        raise FrutasError(str(e)) from None
    except importer.ImportShortError as e:
        raise FrutasError(str(e)) from None
    control.pop("importado", None)
    # «ok»: ha pasado las mismas comprobaciones que la importación (la página de revisión lo exige).
    control.update({"ok": True, "ruta": str(video), "subtitulos": str(srt), "numero_escenas": len(scenes),
                    "validado_con_catalogo": catalog is not None,
                    "escenas": [{"plano": s.plano, "imagen": str(s.image), "voz": str(s.voice), "texto": s.text,
                                 "duracion": s.duration, "movimiento": s.movimiento} for s in scenes]})
    control_file.write_text(json.dumps(control, ensure_ascii=False, indent=1), encoding="utf-8")
    return control


def _copy(source: Path, target: Path) -> None:
    # /tmp suele ser otro disco (tmpfs) y replace() no cruza discos: se copia al lado y luego se renombra, para
    # que nunca quede a medias un video.mp4 que parezca bueno.
    part = target.with_name(target.name + ".part")
    shutil.copyfile(source, part)
    part.replace(target)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Monta un Short local de 8 a 12 fotos con voces WAV preparadas")
    parser.add_argument("guion", type=Path, help="JSON con escenas (imagen o plano, voz, texto y duración)")
    parser.add_argument("destino", type=Path, help="Carpeta local para el MP4, los subtítulos y el control")
    parser.add_argument("--catalogo", type=Path, help="Catálogo de planos: valida el guion y saca de ahí las fotos")
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalogo) if args.catalogo else None
        print(json.dumps(produce(args.guion, args.destino, catalog=catalog), ensure_ascii=False))
    except FrutasError as exc:
        print(f"No se monta: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
