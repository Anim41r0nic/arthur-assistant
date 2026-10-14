"""Un proyecto de vídeo: su carpeta, su guion y la preparación de recursos (paso 3).

Carpeta: <datos>/studio/proyectos/<nombre>/
  guion.toml   ← lo escribe una persona o un modelo; Marc lo puede abrir y corregir con cualquier editor
  recursos/    ← imágenes descargadas (img-<huella del título>.jpg) (con su licencia apuntada en studio.sqlite3)
  voz/, salida/ (pasos siguientes)

El guion (TOML, se lee con la biblioteca estándar):

  titulo = "Su mejor general lo traicionó. Marco Aurelio hizo esto"
  descripcion = "…"
  idioma = "es"

  voz = "kokoro:ef_dora"            # opcional: si falta, la del idioma (es → Dora, en → Heart)
  miniatura_texto = "LO TRAICIONÓ"  # opcional: texto grande de la miniatura
  miniatura_imagen = "File:….jpg"   # opcional: una de las imágenes de las escenas

  [[fuentes]]                      # de dónde sale cada dato: sin fuente, la escena no vale
  id = "dion"
  obra = "Historia romana, libro LXXII"
  autor = "Dion Casio"
  url = "https://…"                # opcional

  [corto]                          # opcional: el corto vertical (Shorts, Reels, TikTok)
  escenas = [1, 2, 9, 10]          # qué escenas lleva (si falta: las primeras hasta ~55 s)
  titulo = "Su general lo traicionó"

  [[escenas]]
  texto = "En el año 175, …"       # lo que dice la voz
  imagen = "File:….jpg"            # imagen de Wikimedia Commons (se comprueba su licencia)
  fuentes = ["dion"]

Honestidad: cada escena debe citar al menos una fuente de la lista; si no, el guion no se acepta. Una
escena puede marcarse `opinion = true` (la lección final, p. ej.): entonces no necesita fuente, pero queda
claro que es opinión.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .assets import TITLE, CommonsClient, attribution_lines
from .store import Asset, StudioStore, studio_dir

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
SOURCE_ID = re.compile(r"^[a-z0-9_-]{1,30}$")
MAX_SCENES = 60
MAX_SCENE_CHARS = 700
WORDS_PER_MINUTE = 150  # ritmo aproximado de la voz de Piper a velocidad normal


class ScriptError(ValueError):
    """El guion tiene un fallo: el mensaje dice cuál, para corregirlo a mano."""


@dataclass(frozen=True)
class Source:
    id: str
    obra: str
    autor: str
    url: str | None = None


@dataclass(frozen=True)
class Scene:
    number: int
    texto: str
    imagen: str
    fuentes: tuple[str, ...]
    opinion: bool = False


@dataclass(frozen=True)
class Short:
    """El corto vertical (Shorts, Reels, TikTok) que se saca del vídeo largo. Todo opcional."""
    escenas: tuple[int, ...] = ()   # qué escenas lleva, en orden; vacío = las primeras hasta ~55 s
    titulo: str = ""                # vacío = el título del vídeo largo
    descripcion: str = ""           # vacío = la del vídeo largo


@dataclass(frozen=True)
class Script:
    titulo: str
    descripcion: str
    idioma: str
    fuentes: tuple[Source, ...]
    escenas: tuple[Scene, ...]
    etiquetas: tuple[str, ...] = field(default=())
    miniatura_texto: str = ""     # 2-5 palabras grandes; si falta, el principio del título
    miniatura_imagen: str = ""    # una de las imágenes del guion; si falta, la de la primera escena
    voz: str = ""                 # «kokoro:ef_dora», «piper:es_ES-davefx-medium»…; si falta, la del idioma
    corto: Short = field(default_factory=Short)

    @property
    def thumbnail_text(self) -> str:
        return self.miniatura_texto or " ".join(self.titulo.rstrip(".").split()[:4])

    @property
    def thumbnail_image(self) -> str:
        return self.miniatura_imagen or self.escenas[0].imagen

    @property
    def words(self) -> int:
        return sum(len(s.texto.split()) for s in self.escenas)

    @property
    def minutes(self) -> float:
        return self.words / WORDS_PER_MINUTE


def projects_dir() -> Path:
    return studio_dir() / "proyectos"


def project_dir(name: str) -> Path:
    if not NAME.match(name):
        raise ScriptError("El nombre del proyecto solo puede tener minúsculas, números y guiones (p. ej. marco-casio)")
    return projects_dir() / name


def load_script(path: Path) -> Script:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScriptError(f"No encuentro el guion: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ScriptError(f"El guion tiene un error de formato: {e}") from None
    return parse_script(data)


def parse_script(data: dict) -> Script:
    def text(value, what: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ScriptError(f"Falta {what}")
        value = " ".join(value.split())
        if len(value) > limit:
            raise ScriptError(f"{what[0].upper()}{what[1:]} es demasiado largo (máximo {limit} caracteres)")
        return value

    titulo = text(data.get("titulo"), "el título", 100)  # YouTube corta en 100
    descripcion = text(data.get("descripcion"), "la descripción", 4000)
    idioma = str(data.get("idioma", "es"))
    if not re.match(r"^[a-z]{2}$", idioma):
        raise ScriptError("El idioma debe ser un código de dos letras (es, en…)")
    etiquetas = data.get("etiquetas", [])
    if not isinstance(etiquetas, list) or not all(isinstance(t, str) and 0 < len(t) <= 40 for t in etiquetas):
        raise ScriptError("Las etiquetas deben ser una lista de palabras cortas")

    sources = []
    for n, raw in enumerate(data.get("fuentes", []), start=1):
        if not isinstance(raw, dict):
            raise ScriptError(f"La fuente {n} está mal escrita")
        sid = str(raw.get("id", ""))
        if not SOURCE_ID.match(sid):
            raise ScriptError(f"La fuente {n} necesita un id corto (minúsculas, números, - o _)")
        url = raw.get("url")
        if url is not None and not (isinstance(url, str) and url.startswith("https://")):
            raise ScriptError(f"La dirección de la fuente «{sid}» debe empezar por https://")
        sources.append(Source(sid, text(raw.get("obra"), f"la obra de la fuente «{sid}»", 200),
                              text(raw.get("autor"), f"el autor de la fuente «{sid}»", 100), url))
    ids = [s.id for s in sources]
    if len(set(ids)) != len(ids):
        raise ScriptError("Hay dos fuentes con el mismo id")

    scenes = []
    raw_scenes = data.get("escenas", [])
    if not isinstance(raw_scenes, list) or not 1 <= len(raw_scenes) <= MAX_SCENES:
        raise ScriptError(f"El guion necesita entre 1 y {MAX_SCENES} escenas")
    for n, raw in enumerate(raw_scenes, start=1):
        if not isinstance(raw, dict):
            raise ScriptError(f"La escena {n} está mal escrita")
        image = str(raw.get("imagen", ""))
        if not TITLE.match(image):
            raise ScriptError(f"La escena {n} necesita una imagen de Commons que empiece por «File:»")
        cited = raw.get("fuentes", [])
        opinion = bool(raw.get("opinion", False))
        if not isinstance(cited, list) or not all(isinstance(c, str) for c in cited):
            raise ScriptError(f"Las fuentes de la escena {n} deben ser una lista de ids")
        unknown = [c for c in cited if c not in ids]
        if unknown:
            raise ScriptError(f"La escena {n} cita fuentes que no están en la lista: {', '.join(unknown)}")
        if not cited and not opinion:
            raise ScriptError(f"La escena {n} no cita ninguna fuente (si es opinión, márcala con opinion = true)")
        scenes.append(Scene(n, text(raw.get("texto"), f"el texto de la escena {n}", MAX_SCENE_CHARS), image,
                            tuple(cited), opinion))
    thumb_text = data.get("miniatura_texto", "")
    if not isinstance(thumb_text, str) or len(thumb_text) > 40:
        raise ScriptError("miniatura_texto debe ser un texto corto (máximo 40 caracteres)")
    thumb_image = data.get("miniatura_imagen", "")
    if thumb_image and thumb_image not in {s.imagen for s in scenes}:
        raise ScriptError("miniatura_imagen debe ser una de las imágenes de las escenas")
    voice = data.get("voz", "")
    if voice and not (isinstance(voice, str) and re.fullmatch(r"(piper|kokoro):[A-Za-z0-9_.-]{1,60}", voice)):
        raise ScriptError("voz debe ser «kokoro:nombre» o «piper:nombre» (p. ej. kokoro:ef_dora)")
    raw_short = data.get("corto", {})
    if not isinstance(raw_short, dict):
        raise ScriptError("[corto] está mal escrito")
    short_scenes = raw_short.get("escenas", [])
    numbers = {sc.number for sc in scenes}
    if not isinstance(short_scenes, list) or not all(isinstance(n, int) and n in numbers for n in short_scenes) \
            or len(set(short_scenes)) != len(short_scenes):
        raise ScriptError("corto.escenas debe ser una lista de números de escena del guion, sin repetir")
    short = Short(tuple(short_scenes),
                  text(raw_short["titulo"], "el título del corto", 100) if raw_short.get("titulo") else "",
                  text(raw_short["descripcion"], "la descripción del corto", 2000) if raw_short.get("descripcion") else "")
    return Script(titulo, descripcion, idioma, tuple(sources), tuple(scenes), tuple(etiquetas),
                  " ".join(thumb_text.split()), thumb_image, voice, short)


@dataclass
class Preparation:
    script: Script
    assets: list[Asset]

    @property
    def blocked(self) -> list[Asset]:
        return [a for a in self.assets if a.status != "ok"]

    @property
    def ready(self) -> bool:
        return not self.blocked


def prepare(name: str, store: StudioStore, commons: CommonsClient) -> Preparation:
    """Lee el guion del proyecto y descarga (o reutiliza) la imagen de cada escena, comprobando su licencia."""
    folder = project_dir(name)
    script = load_script(folder / "guion.toml")
    known = {a.title.replace("_", " "): a for a in store.assets(name)}
    assets = []
    for title in dict.fromkeys(s.imagen for s in script.escenas):  # una vez cada imagen aunque se repita
        # El nombre sale del título: así cambiar el orden de las escenas no hace que una imagen pise a otra.
        name_hint = "img-" + hashlib.sha256(title.replace("_", " ").encode()).hexdigest()[:12]
        previous = known.get(title.replace("_", " "))
        if previous and previous.status == "ok" and (folder / "recursos" / previous.name).exists():
            assets.append(previous)  # ya estaba bien descargada: no se vuelve a pedir
            continue
        assets.append(commons.fetch(title, name, folder / "recursos", name_hint))
    return Preparation(script, assets)


def full_description(script: Script, assets: list[Asset]) -> str:
    """La descripción que irá a YouTube: el texto del guion, las fuentes y los créditos de las imágenes."""
    lines = [script.descripcion, "", "Fuentes:"]
    for s in script.fuentes:
        lines.append(f"• {s.autor}, {s.obra}" + (f" — {s.url}" if s.url else ""))
    credits = attribution_lines(assets)
    if credits:
        lines += ["", "Imágenes:"] + [f"• {c}" for c in credits]
    return "\n".join(lines)
