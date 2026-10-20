"""Frutinovelas diario (0.66.0): cada día un Short nuevo, hecho y publicado solo (decisión de Marc, 18-oct-2026).

Es un trabajo de la cola de studio-piloto (tipo «frutas», un trabajo a la vez con el piloto de estoicismo):

  guion    Workers AI escribe 8–10 escenas usando SOLO planos del catálogo (frutinovelas_catalogo.json). El
           código lo valida (frutas_validation: cada frase solo cuenta lo que su plano enseña) y, si no pasa, se lo
           devuelve con los motivos (3 intentos). Un segundo pase de Workers AI revisa; si ve problemas, se reescribe
           UNA vez. Lo que siga dudoso va a comprobacion.json: entonces NO se publica solo y se avisa a Marc.
  planos   copia a la carpeta del episodio los planos usados, comprobando su sha256. Si falta alguno, FLUX.2 Klein
           lo genera desde la referencia del personaje (como mucho 3 al día) y queda como duda (nadie lo ha visto).
  voces    Kokoro, una voz fija por personaje (las que aceptó Marc: Dora, Alex, Emma, Heart, Santa).
  montar   frutas_directo (1080×1920, subtítulos, movimiento de cámara por escena, control de calidad) y la
           revisión «esperando» en el canal Frutinovelas.

A las 18:00 la página de revisión lo aprueba sola si no tiene dudas (autopilot.auto_publish_loop) y sube en PÚBLICO
con la marca de contenido sintético de YouTube; el aviso de «subido» llega al móvil por el núcleo. El modelo no
tiene ninguna herramienta para publicar: escribe un JSON y lo demás lo decide este código.

Neurons: la capa gratis de Workers AI son 10 000 al día PARA TODA LA CUENTA (chat de la web y cerebro incluidos).
Frutinovelas lleva su propia cuenta (cerebro.budget.Budget, 1 500 por defecto, ARTHUR_STUDIO_FRUTAS_NEURONAS) y
pregunta antes de cada llamada. Un día normal gasta unas 200–400 (guion + revisión); cada imagen, unas 110.

Configuración (variables de entorno del contenedor studio-piloto):
  ARTHUR_STUDIO_FRUTAS_HORA          hora del trabajo diario (sin ella, no se programa)
  ARTHUR_STUDIO_FRUTAS_BIBLIOTECA    carpeta con los planos y referencias (en el servidor, /frutas, solo lectura)
  ARTHUR_STUDIO_CF_ACCOUNT           id de la cuenta de Cloudflare (no es secreto)
  ARTHUR_STUDIO_CF_TOKEN_FILE        token de Workers AI (por defecto /run/secrets/workers_ai_token)
  ARTHUR_STUDIO_FRUTAS_NEURONAS      tope diario de Neurons para Frutinovelas (1 500)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..cerebro.budget import Budget, approx_tokens, estimate
from ..notes.engines import EngineError, EngineRefused
from ..webchat.workers_ai import ACCOUNT_PATTERN, CF_API, TOKEN_PATTERN, WorkersAIEngine, _cf_codes
from . import frutas_directo, montage
from .frutas_validation import validar_guion
from .jobs import Step, StepContext, StepFailed
from .montage import MontageError
from .project import projects_dir, project_dir
from .review import CHECK_FILE, FRUTAS_FILE, ReviewBook, ReviewError
from .store import StudioStore, studio_dir
from .voice import KokoroEngine, VoiceError
from .writer import slugify

log = logging.getLogger(__name__)

KIND = "frutas"
PROFILE = "frutinovelas"
DEFAULT_HOUR = 10   # el servidor duerme de 00:00 a 08:00 y el piloto de estoicismo acaba hacia las 09:10
CATALOG_FILE = Path(__file__).with_name("frutinovelas_catalogo.json")
DEFAULT_LIBRARY = "~/frutas-biblioteca"   # en el portátil; en el servidor, la variable de arriba
DEFAULT_TOKEN_FILE = "/run/secrets/workers_ai_token"

MIN_SCENES, MAX_SCENES = 8, 10
MAX_TEXT = 60             # una frase que Kokoro dice en ~4 s y cabe en 3 líneas de subtítulo
MAX_VOICE = 4.5           # segundos; si se pasa, se vuelve a decir algo más deprisa
FAST_SPEED = 1.12
MIN_SCENE, MAX_SCENE = 2.5, 6.0
SCENE_PAD = frutas_directo.VOICE_DELAY + frutas_directo.VOICE_TAIL + 0.3
MAX_TOTAL = 58.0          # Short de menos de 60 s (importer.MAX_SECONDS), con margen para el AAC
WRITE_TOKENS, REVIEW_TOKENS = 2500, 700
ATTEMPTS = 3

IMAGE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
# Tabla oficial (2026-10): 26,05 Neurons por cada bloque de salida de 512×512 y 5,37 por bloque de entrada. Una toma
# vertical de 576×1024 son 4 bloques de salida y la referencia (<512×512) uno de entrada: ~110. Se cuenta de más.
IMAGE_NEURONS = 115.0
MAX_IMAGES = 3
IMAGE_WIDTH, IMAGE_HEIGHT = 576, 1024
DAILY_NEURONS = 1500
IMAGE_TIMEOUT = 120

FOOTER = ("Frutinovelas: una telenovela de frutas en menos de un minuto.\n"
          "Animación hecha con inteligencia artificial: imágenes y voces sintéticas.")
BASE_TAGS = ["frutinovelas", "shorts", "telenovela", "animación", "frutas"]
MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}


class FrutasDiarioError(Exception):
    """Error definitivo del día (el mensaje va al aviso de Marc)."""


# --- catálogo y biblioteca --------------------------------------------------------------------------------------

def load_catalog(path: Path = CATALOG_FILE) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("planos"), list) or not data.get("personajes"):
        raise FrutasDiarioError("El catálogo de Frutinovelas no tiene planos ni personajes")
    return data


def library_root(env=os.environ) -> Path:
    return Path(env.get("ARTHUR_STUDIO_FRUTAS_BIBLIOTECA") or DEFAULT_LIBRARY).expanduser().resolve()


def generated_dir() -> Path:
    """Los planos que hubo que generar (la biblioteca del servidor es de solo lectura)."""
    return studio_dir() / "frutinovelas" / "generados"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, relative: str) -> Path | None:
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def resolve_planos(catalog: dict, root: Path, generated: Path) -> dict[str, Path | None]:
    """Dónde está la foto de cada plano: la de la biblioteca si su sha256 es el del catálogo (una foto distinta con
    el mismo nombre no se usa: nadie la ha revisado), si no una generada otro día, si no None."""
    found: dict[str, Path | None] = {}
    for plano in catalog["planos"]:
        path = _inside(root, plano["archivo"])
        if path is not None and _sha(path) != plano.get("sha256"):
            log.warning("Frutinovelas: %s no es la foto revisada (huella distinta): no se usa", plano["id"])
            path = None
        if path is None:
            path = next((p for p in (generated / f"{plano['id']}{ext}" for ext in (".jpg", ".png")) if p.is_file()),
                        None)
        found[plano["id"]] = path
    return found


def reference(catalog: dict, root: Path, character: str) -> Path | None:
    info = catalog["personajes"].get(character) or {}
    path = _inside(root, str(info.get("referencia", "")))
    if path is None or (info.get("referencia_sha256") and _sha(path) != info["referencia_sha256"]):
        return None
    return path


# --- presupuesto de Neurons -------------------------------------------------------------------------------------

def budget_from_env(env=os.environ) -> Budget:
    try:
        daily = int(env.get("ARTHUR_STUDIO_FRUTAS_NEURONAS", DAILY_NEURONS))
    except ValueError:
        daily = DAILY_NEURONS
    return Budget(studio_dir() / "frutinovelas-neuronas.json", daily=max(0, daily))


# --- Workers AI: texto e imágenes -----------------------------------------------------------------------------------

def _account_and_token(env=os.environ) -> tuple[str, str]:
    account = env.get("ARTHUR_STUDIO_CF_ACCOUNT", "").strip()
    path = env.get("ARTHUR_STUDIO_CF_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not ACCOUNT_PATTERN.match(account) or not TOKEN_PATTERN.match(token):
        # Ni el token ni un trozo: solo qué falta.
        raise StepFailed("Falta la cuenta (ARTHUR_STUDIO_CF_ACCOUNT) o el token de Workers AI para Frutinovelas")
    return account, token


def text_engine_from_env(env=os.environ) -> WorkersAIEngine:
    account, token = _account_and_token(env)
    return WorkersAIEngine(account, token)


class ImageEngine:
    """FLUX.2 Klein por la API REST (multipart con la referencia como input_image_0). Biblioteca estándar."""

    def __init__(self, account: str, token: str, *, api: str = CF_API, opener=None) -> None:
        self.url = f"{api.rstrip('/')}/accounts/{account}/ai/run/{urllib.parse.quote(IMAGE_MODEL, safe='@/')}"
        self._token = token
        self._opener = opener or urllib.request.build_opener()

    def __repr__(self) -> str:
        return "ImageEngine()"   # sin token

    def generate(self, prompt: str, reference_png: bytes) -> bytes:
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("prompt", prompt), ("width", str(IMAGE_WIDTH)), ("height", str(IMAGE_HEIGHT))):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="input_image_0"; filename="ref.png"\r\n'
                     "Content-Type: image/png\r\n\r\n".encode() + reference_png + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with self._opener.open(request, timeout=IMAGE_TIMEOUT) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as e:
            codes = _cf_codes(e.read(4096))
            e.close()
            if e.code in (400, 401, 403):
                raise EngineRefused(f"Workers AI no genera la imagen (error {e.code}, códigos {sorted(codes)})") from None
            raise EngineError(f"Workers AI no genera la imagen ahora (error {e.code})") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise EngineError(f"No puedo conectar con Workers AI para la imagen: {type(e).__name__}") from None
        try:
            data = base64.b64decode(answer["result"]["image"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise EngineError("Workers AI no ha devuelto ninguna imagen") from None
        return data


# --- guion -----------------------------------------------------------------------------------------------------

def _json_object(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] if isinstance(value, str) else ""


def _plain(text: str) -> bool:
    return not any(ord(c) < 32 or ord(c) == 127 for c in text) and "<" not in text and ">" not in text


def normalize(raw: dict, catalog: dict, usable: set[str]) -> tuple[dict, list[str]]:
    """El JSON del modelo → el guion que entiende frutas_directo, y los motivos para rechazarlo (vacío = vale).
    El código pone los números, los estados y los movimientos válidos: el modelo no decide nada de eso."""
    reasons: list[str] = []
    title = _clean(raw.get("titulo"), 200)
    if not 5 <= len(title) <= 80 or not _plain(title):
        reasons.append("El título debe tener de 5 a 80 caracteres, sin < ni >.")
    summary = _clean(raw.get("resumen"), 400)
    description = _clean(raw.get("descripcion"), 600)
    if not description or len(description) > 400 or not _plain(description):
        reasons.append("La descripción debe tener de 1 a 400 caracteres, sin < ni >.")
    tags = [t for t in (_clean(t, 30) for t in (raw.get("etiquetas") or [])[:5] if isinstance(t, str))
            if t and _plain(t)] if isinstance(raw.get("etiquetas"), list) else []
    scenes_raw = raw.get("escenas")
    if not isinstance(scenes_raw, list) or not MIN_SCENES <= len(scenes_raw) <= MAX_SCENES:
        reasons.append(f"Hacen falta entre {MIN_SCENES} y {MAX_SCENES} escenas.")
        scenes_raw = scenes_raw if isinstance(scenes_raw, list) else []
    planos = {p["id"]: p for p in catalog["planos"]}
    scenes = []
    for number, item in enumerate(scenes_raw, 1):
        if not isinstance(item, dict):
            reasons.append(f"Escena {number}: debe ser un objeto.")
            continue
        text = _clean(item.get("texto"), 200)
        if not 1 <= len(text) <= MAX_TEXT or not _plain(text):
            reasons.append(f"Escena {number}: la frase debe tener de 1 a {MAX_TEXT} caracteres.")
        else:
            try:
                frutas_directo._wrapped(text)
            except frutas_directo.FrutasError as e:
                reasons.append(f"Escena {number}: {e}.")
        plano = item.get("plano")
        if isinstance(plano, str) and plano in planos and plano not in usable:
            reasons.append(f"Escena {number}: el plano {plano!r} no está disponible hoy.")
        move = item.get("camara")
        scenes.append({
            "numero": number, "personaje": item.get("personaje"), "plano": plano, "gesto": item.get("gesto"),
            "objetos": item.get("objetos") if isinstance(item.get("objetos"), list) else [],
            "hechos": item.get("hechos") if isinstance(item.get("hechos"), list) else [],
            # Episodios sueltos: nada cambia de manos entre escenas (un cambio exige un plano que lo enseñe).
            "estado_antes": {}, "cambios": [], "texto": text,
            "movimiento": move if move in frutas_directo.MOVES else frutas_directo.DEFAULT_MOVE,
        })
    if len({s["personaje"] for s in scenes}) < 2:
        reasons.append("Deben hablar al menos dos personajes.")
    result = validar_guion({"escenas": scenes}, catalog)
    reasons.extend(result.motivos)
    episode = {"titulo": title, "descripcion": description, "resumen": summary or description,
               "etiquetas": tags, "escenas": scenes}
    return episode, list(dict.fromkeys(reasons))


def _catalog_lines(catalog: dict, usable: set[str]) -> str:
    lines = []
    for p in catalog["planos"]:
        if p["id"] in usable:
            lines.append(f"- {p['id']}: personaje {p['personaje']}, emoción {p.get('emocion') or '-'}, "
                         f"encuadre {p.get('encuadre') or '-'}; gestos {p['gestos']}; objetos que se ven "
                         f"{p['objetos_visibles']}; hechos que se ven {p['hechos_visibles']}")
    return "\n".join(lines)


def write_prompt(catalog: dict, usable: set[str], previous: list[dict], feedback: list[str]) -> str:
    cast = "\n".join(f"- {cid}: {c['nombre']}. {c['descripcion']} {c.get('papel', '')}"
                     for cid, c in catalog["personajes"].items()
                     if any(p["personaje"] == cid and p["id"] in usable for p in catalog["planos"]))
    before = "\n".join(f"- {e['titulo']}: {e.get('resumen', '')}" for e in previous) or "- (ninguno todavía)"
    fix = ("\n\nTu versión anterior tenía estos problemas; corrígelos todos:\n- " + "\n- ".join(feedback)) \
        if feedback else ""
    moves = ", ".join(frutas_directo.MOVES)
    return f"""Escribe un episodio nuevo de «Frutinovelas», una telenovela corta de frutas para YouTube Shorts, en
español de España. Drama familiar apto para todos los públicos: celos, secretos, malentendidos, sorpresas y
reconciliaciones. Nada de violencia, insultos, marcas ni personas reales.

Personajes (solo estos):
{cast}

Planos disponibles (cada escena usa UNO; cada plano es una foto fija de un solo personaje):
{_catalog_lines(catalog, usable)}

Episodios anteriores (no repitas la historia; puedes continuar alguna trama):
{before}

Reglas:
- Entre {MIN_SCENES} y {MAX_SCENES} escenas. Cada escena es UNA frase que dice en voz alta el personaje del plano.
- Frases cortas y naturales, de {MAX_TEXT} caracteres como mucho (mejor 30-50).
- La frase solo puede hablar de lo que se ve en su plano: no narres acciones que la foto no enseña (sacar algo,
  dar algo, abrazar…). Si la frase menciona un objeto, debe estar en «objetos que se ven» de ese plano y ponlo en
  "objetos". "hechos" solo puede llevar «hechos que se ven» de ese plano. "gesto" debe ser uno de sus gestos.
- Al menos dos personajes hablan. Empieza con un gancho fuerte y acaba con un giro o una pregunta abierta.
- "camara" es el movimiento de cámara de la escena: {moves}. Usa «empuje» para la sorpresa.
- Título con gancho de 80 caracteres como mucho, sin emojis. Descripción de una o dos frases.

Contesta SOLO con este JSON, sin nada más:
{{"titulo": "…", "descripcion": "…", "resumen": "una frase con lo que pasa", "etiquetas": ["…"],
 "escenas": [{{"personaje": "fina", "plano": "…", "gesto": "…", "objetos": [], "hechos": [],
              "camara": "acercar", "texto": "…"}}]}}{fix}"""


def review_prompt(episode: dict, catalog: dict) -> str:
    planos = {p["id"]: p for p in catalog["planos"]}
    lines = "\n".join(
        f"{s['numero']}. {s['personaje']} (plano {s['plano']}: emoción {planos[s['plano']].get('emocion') or '-'}, "
        f"se ven {planos[s['plano']]['objetos_visibles']}): «{s['texto']}»"
        for s in episode["escenas"] if s.get("plano") in planos)
    return f"""Eres el revisor de «Frutinovelas», una telenovela de frutas para todos los públicos que se publica
SIN que nadie la mire antes. Revisa este episodio con ojo crítico.

Título: {episode['titulo']}
Descripción: {episode['descripcion']}
Escenas:
{lines}

Señala SOLO problemas concretos y graves: historia incoherente o sin sentido, frases que no son español natural,
contenido no apto para niños, marcas o personas reales, título engañoso, o una frase que afirma una acción u objeto
que su plano no enseña. Si todo está bien, devuelve la lista vacía.

Contesta SOLO con JSON: {{"problemas": ["…"]}}"""


class EpisodeWriter:
    def __init__(self, engine, budget: Budget, catalog: dict, usable: set[str]) -> None:
        self.engine, self.budget, self.catalog, self.usable = engine, budget, catalog, usable

    def _ask(self, prompt: str, max_tokens: int) -> str:
        if not self.budget.allows("ask", estimate("chat", approx_tokens(prompt), max_tokens)):
            raise StepFailed("Frutinovelas ya ha gastado hoy su parte de Neurons de Workers AI")
        text = self.engine.ask(prompt, max_tokens=max_tokens, timeout=120)
        self.budget.spend("ask", estimate("chat", approx_tokens(prompt), approx_tokens(text)))
        return text

    def write(self, previous: list[dict], feedback: list[str] = ()) -> dict:
        """Un episodio que pasa TODAS las comprobaciones del código (o StepFailed tras 3 intentos)."""
        feedback = list(feedback)
        reasons: list[str] = []
        for _ in range(ATTEMPTS):
            raw = _json_object(self._ask(write_prompt(self.catalog, self.usable, previous, feedback), WRITE_TOKENS))
            if raw is None:
                reasons = ["La respuesta no era un JSON válido."]
            else:
                episode, reasons = normalize(raw, self.catalog, self.usable)
                if not reasons:
                    return episode
            feedback = reasons
        raise StepFailed("El guion de hoy no pasa las comprobaciones: " + "; ".join(reasons[:5]))

    def review(self, episode: dict) -> list[str]:
        for _ in range(2):
            data = _json_object(self._ask(review_prompt(episode, self.catalog), REVIEW_TOKENS))
            if data is not None and isinstance(data.get("problemas"), list):
                return [str(p)[:300] for p in data["problemas"] if str(p).strip()][:10]
        return ["El revisor automático no ha contestado en el formato esperado."]


def previous_episodes(limit: int = 7) -> list[dict]:
    found = []
    if projects_dir().exists():
        for path in sorted(projects_dir().glob(f"*/{FRUTAS_FILE}"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                found.append({"titulo": str(data["titulo"])[:100], "resumen": str(data.get("resumen", ""))[:300]})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return found


def description_for(episode: dict) -> str:
    return f"{episode['descripcion']}\n\n{FOOTER}\n\n#Frutinovelas #Shorts"


# --- planos que faltan ----------------------------------------------------------------------------------------

def image_prompt(plano: dict, character: dict) -> str:
    if plano.get("prompt"):
        return plano["prompt"]
    objects = ", ".join(plano.get("objetos_visibles") or []) or "empty hands"
    return (f"Vertical 9:16 cinematic 3D family animation still in a moonlit garden. Use image 0 as the exact "
            f"reference for {character['nombre']}: {character['descripcion']} {plano.get('encuadre') or 'Medium'} "
            f"shot of {character['nombre']} alone, emotion: {plano.get('emocion') or 'neutral'}, visible: {objects}. "
            "Keep the exact face, colors, body shape and clothes of image 0. Blue evening light with a warm edge "
            "light, softly blurred background. One character only, no other characters, no text, no letters, no logo.")


def _small_reference(path: Path, tmp: Path, run) -> bytes:
    """FLUX.2 pide referencias de menos de 512×512: se reduce con ffmpeg (lista fija de argumentos)."""
    out = tmp / "referencia.png"
    run(["ffmpeg", "-v", "error", "-y", "-f", "image2", "-pattern_type", "none", "-i", str(path),
         "-vf", "scale=448:448:force_original_aspect_ratio=decrease", "-frames:v", "1", str(out)])
    return out.read_bytes()


def generate_plano(plano: dict, catalog: dict, root: Path, images: ImageEngine, budget: Budget, folder: Path,
                   run) -> Path:
    character = catalog["personajes"][plano["personaje"]]
    ref = reference(catalog, root, plano["personaje"])
    if ref is None:
        raise StepFailed(f"Falta el plano {plano['id']} y tampoco está la referencia de {character['nombre']}")
    if not budget.allows("generate", IMAGE_NEURONS):
        raise StepFailed(f"Falta el plano {plano['id']} y no queda presupuesto de Neurons para generarlo")
    tmp = folder / "tmp"
    tmp.mkdir(exist_ok=True)
    try:
        data = images.generate(image_prompt(plano, character), _small_reference(ref, tmp, run))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    budget.spend("generate", IMAGE_NEURONS)
    ext = next((e for magic, e in MAGIC.items() if data.startswith(magic)), None)
    if ext is None:
        raise StepFailed(f"Workers AI ha devuelto algo que no es una imagen para {plano['id']}")
    target = generated_dir() / f"{plano['id']}{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    part.write_bytes(data)
    part.replace(target)
    log.info("Frutinovelas: plano %s generado con FLUX.2 Klein (sin revisión visual)", plano["id"])
    return target


# --- voces ----------------------------------------------------------------------------------------------------

def _seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / source.getframerate()


def kokoro_voice(voice: str, speed: float):
    kind, _, name = voice.partition(":")
    if kind != "kokoro" or not name:
        raise StepFailed(f"Voz no disponible en Studio: {voice}")
    return KokoroEngine(name, "es", speed=speed) if speed != 1 else KokoroEngine(name, "es")


def speak_scenes(scenes: list[dict], catalog: dict, folder: Path,
                 engine_for: Callable[[str, float], object] = kokoro_voice) -> list[dict]:
    """Una voz por escena (voz/NN.wav) y su duración. Una frase demasiado larga se repite algo más deprisa."""
    voice_dir = folder / "voz"
    voice_dir.mkdir(parents=True, exist_ok=True)
    engines: dict[tuple[str, float], object] = {}

    def engine(voice: str, speed: float):
        if (voice, speed) not in engines:
            made = engine_for(voice, speed)
            made.check()
            engines[voice, speed] = made
        return engines[voice, speed]

    done = []
    try:
        for scene in scenes:
            voice = catalog["personajes"][scene["personaje"]]["voz"]
            path = voice_dir / f"{scene['numero']:02}.wav"
            seconds = 0.0
            for speed in (1, FAST_SPEED):
                part = path.with_suffix(".part.wav")
                engine(voice, speed).speak(scene["texto"], part)
                part.replace(path)
                seconds = _seconds(path)
                if seconds <= MAX_VOICE:
                    break
            if seconds > MAX_VOICE:
                raise StepFailed(f"La frase de la escena {scene['numero']} dura {seconds:.1f} s (máximo {MAX_VOICE})")
            duration = round(min(MAX_SCENE, max(MIN_SCENE, seconds + SCENE_PAD)), 2)
            done.append({**scene, "voz": f"voz/{path.name}", "duracion": duration})
    finally:
        for made in engines.values():
            made.close()
    total = sum(s["duracion"] for s in done)
    if total > MAX_TOTAL:
        raise StepFailed(f"El episodio duraría {total:.0f} s: un Short debe quedarse por debajo de {MAX_TOTAL:.0f}")
    return done


# --- los pasos del trabajo ------------------------------------------------------------------------------------

def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _problems(folder: Path) -> list[str]:
    try:
        return list(json.loads((folder / CHECK_FILE).read_text(encoding="utf-8"))["problemas"])
    except (OSError, ValueError, KeyError, TypeError):
        return []


def frutas_steps(store: StudioStore, *, text_engine: Callable[[], object] | None = None,
                 image_engine: Callable[[], ImageEngine] | None = None, budget: Callable[[], Budget] | None = None,
                 catalog_path: Path = CATALOG_FILE, root: Callable[[], Path] = library_root,
                 voices: Callable[[str, float], object] = kokoro_voice, run=None,
                 now: Callable[[], datetime] = datetime.now) -> tuple[Step, ...]:
    shared: dict[str, object] = {}

    def get_budget() -> Budget:
        if "budget" not in shared:
            shared["budget"] = budget() if budget else budget_from_env()
        return shared["budget"]

    def catalog() -> dict:
        if "catalog" not in shared:
            shared["catalog"] = load_catalog(catalog_path)
        return shared["catalog"]

    def usable() -> set[str]:
        lib = root()
        found = resolve_planos(catalog(), lib, generated_dir())
        return {pid for pid, path in found.items() if path is not None
                or reference(catalog(), lib, next(p["personaje"] for p in catalog()["planos"] if p["id"] == pid))}

    def guarded(fn):
        def wrapper(ctx: StepContext) -> dict:
            try:
                return fn(ctx)
            except EngineRefused as e:
                raise StepFailed(str(e)) from None
            except EngineError:
                raise   # pasajero: la cola lo reintenta más tarde
            except (frutas_directo.FrutasError, FrutasDiarioError, VoiceError, ReviewError, MontageError,
                    OSError) as e:
                raise StepFailed(str(e)) from None
        return wrapper

    def guion(ctx):
        engine = text_engine() if text_engine else text_engine_from_env()
        writer = EpisodeWriter(engine, get_budget(), catalog(), usable())
        previous = previous_episodes()
        episode = writer.write(previous)
        problems = writer.review(episode)
        if problems:
            # Una reescritura con lo que vio el revisor; si la nueva no pasa el código, se queda la primera.
            try:
                second = writer.write(previous, feedback=problems)
                second_problems = writer.review(second)
                episode, problems = second, second_problems
            except StepFailed:
                pass
        base = f"{now():%Y%m%d}-frutas-{slugify(episode['titulo'], 40)}"
        name, n = base, 2
        while project_dir(name).exists():
            name, n = f"{base}-{n}", n + 1
        folder = project_dir(name)
        folder.mkdir(parents=True)
        _write_json(folder / "episodio.json", episode)
        _write_json(folder / FRUTAS_FILE, {
            "perfil": PROFILE, "titulo": episode["titulo"], "descripcion": description_for(episode),
            "etiquetas": list(dict.fromkeys(BASE_TAGS + episode["etiquetas"]))[:12], "resumen": episode["resumen"]})
        _write_json(folder / CHECK_FILE, {"problemas": problems})
        return {"proyecto": name, "titulo": episode["titulo"], "avisos": len(problems)}

    def planos(ctx):
        name = ctx.outputs["guion"]["proyecto"]
        folder = project_dir(name)
        episode = json.loads((folder / "episodio.json").read_text(encoding="utf-8"))
        lib = root()
        found = resolve_planos(catalog(), lib, generated_dir())
        by_id = {p["id"]: p for p in catalog()["planos"]}
        used = list(dict.fromkeys(s["plano"] for s in episode["escenas"]))
        target = folder / "planos"
        target.mkdir(exist_ok=True)
        generated, episode_planos = [], []
        images = None
        for pid in used:
            source = found.get(pid)
            if source is None:
                if len(generated) >= MAX_IMAGES:
                    raise StepFailed(f"Faltan más de {MAX_IMAGES} planos: no se generan tantos en un día")
                if images is None:
                    images = image_engine() if image_engine else ImageEngine(*_account_and_token())
                source = generate_plano(by_id[pid], catalog(), lib, images, get_budget(), folder,
                                        run or montage.run_ffmpeg)
                generated.append(pid)
            elif source.resolve().parent == generated_dir().resolve():
                generated.append(pid)   # generado otro día: tampoco lo ha visto nadie
            copy = target / f"{pid}{source.suffix.lower()}"
            shutil.copyfile(source, copy)
            episode_planos.append({**by_id[pid], "archivo": f"planos/{copy.name}"})
        _write_json(folder / "catalogo.json", {**catalog(), "raiz_recursos": str(folder), "planos": episode_planos})
        if generated:
            problems = _problems(folder) + [f"El plano {pid} lo ha generado la IA hoy y nadie lo ha revisado: "
                                            "míralo antes de aprobar." for pid in generated]
            _write_json(folder / CHECK_FILE, {"problemas": problems})
        return {"planos": len(used), "generados": generated}

    def voces(ctx):
        name = ctx.outputs["guion"]["proyecto"]
        folder = project_dir(name)
        episode = json.loads((folder / "episodio.json").read_text(encoding="utf-8"))
        scenes = speak_scenes(episode["escenas"], catalog(), folder, voices)
        _write_json(folder / "guion.json", {"titulo": episode["titulo"], "escenas": scenes})
        return {"duracion": round(sum(s["duracion"] for s in scenes), 2)}

    def montar(ctx):
        name = ctx.outputs["guion"]["proyecto"]
        folder = project_dir(name)
        out = folder / "salida" / "corto"
        video = out / "video.mp4"
        runner = run or montage.run_ffmpeg
        control_file = out / "control.json"
        if not (video.exists() and control_file.exists() and json.loads(control_file.read_text()).get("ok")):
            if video.exists():
                video.unlink()   # de un intento cortado a medias: se vuelve a montar entero
            episode_catalog = json.loads((folder / "catalogo.json").read_text(encoding="utf-8"))
            frutas_directo.produce(folder / "guion.json", out, catalog=episode_catalog, run=runner,
                                   max_voice=MAX_VOICE)
        thumb = out / "miniatura.jpg"
        # Un fotograma solo para la página de revisión (a un Short no se le pone miniatura por la API).
        runner(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(video), "-frames:v", "1", "-q:v", "3",
                str(thumb)])
        review = ReviewBook(store).submit(name, "corto")
        return {"corto": review.id}

    return (Step("guion", guarded(guion)), Step("planos", guarded(planos)), Step("voces", guarded(voces)),
            Step("montar", guarded(montar)))


def running(store: StudioStore):
    return next((j for j in store.list_jobs(limit=20) if j.kind == KIND and j.status in ("queued", "running")), None)


# --- avisos ---------------------------------------------------------------------------------------------------

def job_notice(store: StudioStore, job, publish_hour: int = 18) -> str | None:
    if job.kind != KIND or job.status not in ("done", "failed"):
        return None
    if job.status == "failed":
        return f"Studio: hoy no hay Frutinovelas ({(job.error or 'sin detalle')[:150]})."
    outputs = {s.name: s.output or {} for s in store.steps(job.id)}
    title = str(outputs.get("guion", {}).get("titulo", "sin título"))[:120]
    try:
        doubts = len(_problems(project_dir(str(outputs.get("guion", {}).get("proyecto", "")))))
    except Exception:
        doubts = int(outputs.get("guion", {}).get("avisos", 0) or 0)
    if doubts:
        return (f"Studio: Frutinovelas «{title}» tiene {doubts} duda{'s' if doubts != 1 else ''}: no se publica "
                "solo. Revísalo en la página.")
    return f"Studio: Frutinovelas «{title}» listo. Se publica solo, en público, a las {publish_hour}:00."


def job_review(store: StudioStore, job) -> int | None:
    if job.kind != KIND or job.status != "done":
        return None
    outputs = {s.name: s.output or {} for s in store.steps(job.id)}
    made = outputs.get("montar", {}).get("corto")
    return made if isinstance(made, int) and not isinstance(made, bool) else None


def hour_from_env(env=os.environ) -> int | None:
    """La hora del trabajo diario, o None si no se ha configurado (en el portátil no se programa solo)."""
    value = env.get("ARTHUR_STUDIO_FRUTAS_HORA", "").strip()
    if not value or value.lower() == "no":
        return None
    if not re.fullmatch(r"\d{1,2}", value) or not 0 <= int(value) <= 23:
        raise ValueError("ARTHUR_STUDIO_FRUTAS_HORA debe ser una hora de 0 a 23 (o «no»)")
    return int(value)
