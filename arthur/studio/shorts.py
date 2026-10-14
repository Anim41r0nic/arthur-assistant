"""Cortos verticales (Shorts, Reels, TikTok) sacados de un vídeo largo ya hecho (petición de Marc, 2026-10-12).

No se genera nada nuevo con IA: se recorta la voz que ya tiene el vídeo largo y se vuelven a montar sus imágenes en
vertical. Cuesta poco y el corto dice exactamente lo mismo que el largo (mismas fuentes, mismas licencias).

  guion [corto] (o las primeras escenas hasta ~55 s)
     │  voz/narracion.wav + voz/linea_tiempo.json del vídeo largo
     ▼
  salida/corto/voz.wav      la voz de esas escenas, con pausas cortas
  salida/corto/escenas/     cada imagen en vertical (1080×1920) con movimiento suave (se reutilizan)
  salida/corto/video.mp4    + subtítulos GRANDES quemados (en los cortos casi todos lo ven sin sonido)
                            + rótulo final: «Historia completa en el canal @…» (lleva al vídeo largo, que es el que paga)
  salida/corto/miniatura.jpg  un fotograma (YouTube no deja poner miniatura propia a un Short por la API)
  salida/corto/control.json   tamaño vertical, menos de 60 s, sonido y volumen

ffmpeg con lista fija de argumentos; cada subtítulo va en su propio fichero (textfile=), nunca dentro del filtro.
"""

from __future__ import annotations

import json
import wave
from collections.abc import Callable
from pathlib import Path

from . import montage
from .montage import FONTS, MontageError, Runner, ScenePlan, run_ffmpeg
from .project import Script
from .review import CHANNELS
from .store import Asset

WIDTH, HEIGHT = 1080, 1920
MAX_SECONDS = 59.0      # Shorts admite hasta 3 min, pero Reels/TikTok y la atención funcionan mejor por debajo de 1 min
AUTO_SECONDS = 55.0     # sin [corto] en el guion: las primeras escenas hasta aquí
GAP = 0.35              # pausa entre escenas (más corta que en el largo: el ritmo de un corto es más rápido)
END_CARD = 2.5          # segundos del rótulo final
SUB_CHARS = 20          # caracteres por línea de subtítulo en vertical (letra grande)
END_TEXT = {"es": "Historia completa\nen el canal", "en": "Full story\non the channel"}


def pick_scenes(script: Script, timeline: dict) -> list[int]:
    times = {int(n): (float(a), float(b)) for n, a, b in timeline["escenas"]}

    def length(n: int) -> float:
        return times[n][1] - times[n][0] + GAP

    if script.corto.escenas:
        missing = [n for n in script.corto.escenas if n not in times]
        if missing:
            raise MontageError(f"Las escenas {missing} no tienen voz: ejecuta --studio-voz otra vez")
        chosen = list(script.corto.escenas)
        total = sum(length(n) for n in chosen) + END_CARD
        if total > MAX_SECONDS:
            raise MontageError(f"El corto duraría {total:.0f} s (máximo {MAX_SECONDS:.0f}): quita escenas de [corto]")
        return chosen
    chosen, total = [], END_CARD
    for scene in script.escenas:
        if scene.number not in times:
            break
        if chosen and total + length(scene.number) > AUTO_SECONDS:
            break
        chosen.append(scene.number)
        total += length(scene.number)
    if not chosen or total > MAX_SECONDS:
        raise MontageError("La primera escena ya es demasiado larga para un corto: elige escenas en [corto]")
    return chosen


def build_voice(narration: Path, timeline: dict, chosen: list[int], output: Path) -> tuple[float, list[dict], list[float]]:
    """Recorta de la narración del largo las escenas elegidas y las une con pausas cortas.

    Devuelve (duración, frases con sus tiempos NUEVOS, duración de cada escena en el corto)."""
    times = {int(n): (float(a), float(b)) for n, a, b in timeline["escenas"]}
    frases = timeline.get("frases", [])
    output.parent.mkdir(parents=True, exist_ok=True)
    moved, spans, position = [], [], 0.0
    tmp = output.with_suffix(".part.wav")
    with wave.open(str(narration), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        with wave.open(str(tmp), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(width)
            out.setframerate(rate)
            for number in chosen:
                start, end = times[number]
                first, last = int(start * rate), int(end * rate)
                source.setpos(first)
                out.writeframes(source.readframes(last - first))
                shift = position - first / rate
                moved += [{"texto": f["texto"], "empieza": round(f["empieza"] + shift, 3),
                           "acaba": round(f["acaba"] + shift, 3)} for f in frases if f["escena"] == number]
                gap = int(GAP * rate)
                out.writeframes(b"\x00" * gap * width * channels)
                span = (last - first + gap) / rate
                spans.append(span)
                position += span
    tmp.replace(output)
    return round(position, 3), moved, spans


def end_card(image: Path, language: str, channel: str, folder: Path, run: Runner) -> Path:
    font = _font()
    # Colores del kit de marca: velo obsidiana sobre la imagen, mensaje en mármol y el canal en bronce.
    # Una línea por fichero: drawtext no cambia de color a media frase, y un bloque de varias líneas
    # se alinea a la izquierda en vez de centrarse.
    message = END_TEXT.get(language, END_TEXT["en"]).split("\n")
    texts = []
    for n, line in enumerate(message):
        path = folder / f"final-{n}.txt"
        path.write_text(line, encoding="utf-8")
        y = f"h/2-40-{(len(message) - n) * 100}"
        texts.append(f"drawtext=fontfile={font}:textfile={path}:fontsize=84:fontcolor={montage.MARMOL}:borderw=6:"
                     f"bordercolor=black:x=(w-text_w)/2:y={y},")
    name = folder / "canal.txt"
    name.write_text(channel, encoding="utf-8")
    target = folder / "final.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(montage.FPS), "-t", str(END_CARD),
         "-i", str(image), "-vf",
         f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},boxblur=20:2,"
         f"drawbox=x=0:y=0:w=iw:h=ih:color={montage.OBSIDIANA}@0.6:t=fill,"
         + "".join(texts)
         + f"drawbox=x=(iw-360)/2:y=ih/2-3:w=360:h=6:color={montage.BRONCE}:t=fill,"
         f"drawtext=fontfile={font}:textfile={name}:fontsize=72:fontcolor={montage.BRONCE}:borderw=5:bordercolor=black:"
         "x=(w-text_w)/2:y=h/2+50,"
         f"fade=t=in:st=0:d={montage.FADE},format=yuv420p",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(montage.FPS), "-an",
         "-threads", str(montage.THREADS), str(target)])
    return target


def subtitle_filter(frases: list[dict], folder: Path) -> str:
    """Un drawtext por frase, visible solo mientras se dice. Cada texto va en su fichero (nunca dentro del filtro)."""
    font = _font()
    subs = folder / "subs"
    subs.mkdir(parents=True, exist_ok=True)
    parts = []
    for n, frase in enumerate(frases, start=1):
        path = subs / f"{n:03d}.txt"
        path.write_text(lines(frase["texto"]), encoding="utf-8")
        parts.append(f"drawtext=fontfile={font}:textfile={path}:fontsize=68:fontcolor=white:borderw=7:"
                     f"bordercolor=black:line_spacing=12:x=(w-text_w)/2:y=h*0.70:"
                     f"enable='between(t,{frase['empieza']:.3f},{frase['acaba']:.3f})'")
    return ",".join(parts) if parts else "null"


def lines(text: str, width: int = SUB_CHARS) -> str:
    """Líneas de como mucho `width` caracteres (en vertical caben pocas letras grandes por línea)."""
    out, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return "\n".join(out)


def _font() -> Path:
    font = next((f for f in FONTS if f.exists()), None)
    if font is None:
        raise MontageError("No encuentro una letra para los textos (instala noto-fonts)")
    return font


def produce_short(script: Script, assets: list[Asset], folder: Path, run: Runner = run_ffmpeg,
                  progress: Callable[[str], None] = lambda _: None) -> dict:
    images = montage.check_assets(script, assets, folder)  # misma regla: sin licencia válida no se monta
    timeline_path, narration = folder / "voz" / "linea_tiempo.json", folder / "voz" / "narracion.wav"
    if not timeline_path.exists() or not narration.exists():
        raise MontageError("Falta la voz: ejecuta --studio-voz primero")
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    chosen = pick_scenes(script, timeline)
    out = folder / "salida" / "corto"
    out.mkdir(parents=True, exist_ok=True)
    voice_path = out / "voz.wav"
    duration, frases, spans = build_voice(narration, timeline, chosen, voice_path)
    by_number = {s.number: s for s in script.escenas}
    clips, position = [], 0.0
    for i, (number, span) in enumerate(zip(chosen, spans)):
        progress(f"escena {number} en vertical ({span:.1f} s)")
        plan = ScenePlan(number, images[by_number[number].imagen], round(position, 3), round(span, 3))
        clips.append(montage.render_scene(plan, out / "escenas", run, zoom_in=i % 2 == 0, width=WIDTH, height=HEIGHT))
        position += span
    progress("rótulo final")
    clips.append(end_card(images[by_number[chosen[-1]].imagen], script.idioma, CHANNELS.get(script.idioma, ""), out, run))
    listing = out / "escenas.txt"
    listing.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    progress("subtítulos, voz y volumen")
    loud = montage.measure_loudness(voice_path, run)
    loudnorm = (f"loudnorm=I={montage.LUFS}:TP=-1.5:LRA=11:measured_I={loud['input_i']}:measured_TP={loud['input_tp']}:"
                f"measured_LRA={loud['input_lra']}:measured_thresh={loud['input_thresh']}:"
                f"offset={loud['target_offset']}:linear=true")
    video, tmp = out / "video.mp4", out / "video.part.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(voice_path),
         "-map", "0:v", "-map", "1:a", "-vf", subtitle_filter(frases, out),
         "-af", f"{montage.COMPRESS},{loudnorm},apad", "-shortest",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-threads", str(montage.THREADS),
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(tmp)])
    tmp.replace(video)
    progress("fotograma para la página")
    thumb = out / "miniatura.jpg"
    run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(thumb)])
    report = montage.quality_check(video, duration + END_CARD, run, width=WIDTH, height=HEIGHT)
    if report["duracion"] > MAX_SECONDS + 1:
        report["ok"] = False
        report["problemas"].append(f"dura {report['duracion']:.0f} s: un corto debe durar menos de un minuto")
    report.update({"video": str(video), "miniatura": str(thumb), "escenas": chosen,
                   "sha256_video": montage._sha(video)})
    (out / "control.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report
