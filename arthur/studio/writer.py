"""El escritor de guiones del piloto automático (paso 6): Gemini propone, el código pone las reglas.

Honestidad (docs/studio.md) frente a un modelo que puede inventarse cosas:
  1. CATÁLOGO CERRADO de fuentes clásicas de dominio público. Gemini solo puede citar sus ids; el título, el autor y
     la dirección los pone este código (comprobadas el 2026-10-12). Un libro inventado no puede entrar.
  2. Cada escena con hechos lleva fuente y el lugar exacto (libro, capítulo); los lugares salen también en las
     fuentes de la descripción, para que cualquiera pueda comprobarlos. La lección final va como opinión.
  3. Una segunda pasada de Gemini hace de REVISOR (temperatura 0): señala frases dudosas o sin respaldo. Si hay algo,
     se reescribe una vez; lo que quede se guarda en comprobacion.json y sale como aviso en la página de revisión.
     Si al reescribir se rompe la forma (p. ej. se queda corto), hay una vuelta más solo para arreglar la forma.
  4. Marc lo ve siempre antes de publicar. Nada de esto sustituye su revisión: la ayuda.

Imágenes: Gemini sugiere qué buscar en Wikimedia Commons para cada escena; el código busca y se queda con la
primera imagen de licencia válida (assets.license_verdict). Si no encuentra ninguna, usa un retrato de reserva.

El guion se escribe en TOML (lo lee project.parse_script, el mismo que los guiones hechos a mano).
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from .assets import AssetError, CommonsClient
from .gemini import GeminiClient
from .project import Script, ScriptError, parse_script

log = logging.getLogger(__name__)

# Fuentes que Gemini puede citar (dominio público). Direcciones comprobadas (HTTP 200) el 2026-10-12.
CATALOG: dict[str, dict[str, str]] = {
    "meditaciones": {"autor": "Marco Aurelio", "obra": "Meditaciones (trad. G. Long, dominio público)",
                     "url": "https://www.gutenberg.org/ebooks/2680"},
    "seneca-cartas": {"autor": "Séneca", "obra": "Cartas a Lucilio (trad. R. M. Gummere, dominio público)",
                      "url": "https://en.wikisource.org/wiki/Moral_letters_to_Lucilius"},
    "seneca-brevedad": {"autor": "Séneca", "obra": "Sobre la brevedad de la vida (dominio público)",
                        "url": "https://en.wikisource.org/wiki/On_the_shortness_of_life"},
    "seneca-tranquilidad": {"autor": "Séneca", "obra": "Sobre la tranquilidad del alma (dominio público)",
                            "url": "https://en.wikisource.org/wiki/Of_Peace_of_Mind"},
    "epicteto-enquiridion": {"autor": "Epicteto (Arriano)", "obra": "Enquiridión (dominio público)",
                             "url": "https://www.gutenberg.org/ebooks/45109"},
    "epicteto-disertaciones": {"autor": "Epicteto (Arriano)", "obra": "Disertaciones (selección, dominio público)",
                               "url": "https://www.gutenberg.org/ebooks/10661"},
    "diogenes-laercio": {"autor": "Diógenes Laercio", "obra": "Vidas de los filósofos ilustres (dominio público)",
                         "url": "https://en.wikisource.org/wiki/Lives_of_the_Eminent_Philosophers"},
    "dion-casio": {"autor": "Dion Casio", "obra": "Historia romana (trad. E. Cary, dominio público)",
                   "url": "https://penelope.uchicago.edu/Thayer/E/Roman/Texts/Cassius_Dio/home.html"},
    "historia-augusta": {"autor": "Historia Augusta", "obra": "Vidas de los emperadores (dominio público)",
                         "url": "https://penelope.uchicago.edu/Thayer/E/Roman/Texts/Historia_Augusta/home.html"},
    "suetonio": {"autor": "Suetonio", "obra": "Vidas de los doce césares (dominio público)",
                 "url": "https://penelope.uchicago.edu/Thayer/E/Roman/Texts/Suetonius/12Caesars/home.html"},
    "tacito": {"autor": "Tácito", "obra": "Anales e Historias (dominio público)",
               "url": "https://penelope.uchicago.edu/Thayer/E/Roman/Texts/Tacitus/home.html"},
    "plutarco": {"autor": "Plutarco", "obra": "Vidas paralelas (dominio público)",
                 "url": "https://penelope.uchicago.edu/Thayer/E/Roman/Texts/Plutarch/Lives/home.html"},
}
# Formatos del kit de marca de Business Lab (aprobados por Marc el 2026-10-13). «carta» es un experimento:
# no se sabe aún si funciona, por eso solo sale algunos días (ver autopilot.format_for).
FORMATS = {
    "prueba": ("Propón la MEJOR historia real para el próximo vídeo: un episodio concreto de la vida de un "
               "estoico (o de alguien que lo puso a prueba) que esté contado en esas fuentes, con un gancho fuerte."),
    "carta": ("Propón UNA carta concreta de las Cartas a Lucilio de Séneca (di su número) para el próximo vídeo: "
              "qué problema de la vida trata, qué cuenta Séneca de sí mismo o de Lucilio en ella y por qué sigue "
              "sirviendo hoy. Solo lo que dice esa carta; la aplicación de hoy va en la última escena, como opinión. "
              "La fuente principal es «seneca-cartas»."),
}
# Si para una escena no aparece ninguna imagen con licencia válida (comprobadas el 2026-10-12).
FALLBACK_IMAGE = "File:Marcus Aurelius Capitoline Hill September 2015-1.jpg"
MIN_SCENES, MAX_SCENES = 10, 18
TARGET_WORDS = (550, 800)   # 4-5 minutos con la voz de Kokoro

SYSTEM = """Eres guionista de un canal de YouTube en español sobre estoicismo contado con HISTORIAS REALES.
Reglas que no se pueden romper:
- Solo hechos que estén en las fuentes clásicas del catálogo que se te da. Si no estás seguro de un dato, no lo pongas.
- Cada escena con hechos cita al menos una fuente del catálogo (por su id) y el lugar exacto (libro, capítulo o carta).
- Nada de inventar diálogos ni cifras. Las citas, solo si son conocidas y de esa obra; si no, cuéntalo sin comillas.
- Estilo: frases cortas para leer en voz alta, segunda persona al empezar («Imagina que…»), tono sereno, sin
  sensacionalismo barato. La última escena es la lección y va marcada como opinión.
- No es un vídeo genérico de «7 consejos»: es UNA historia (o UNA carta) concreta con principio, nudo y final."""


class WriterError(Exception):
    pass


@dataclass(frozen=True)
class Draft:
    script: Script
    toml: str
    problems: list[str]      # lo que el revisor no pudo dar por bueno (sale en la página de revisión)
    idea: dict


def slugify(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].rstrip("-") or "video"


def _s(value: str) -> str:
    """Cadena TOML: las mismas reglas de escape que JSON para comillas, barras y caracteres de control."""
    return json.dumps(" ".join(str(value).split()), ensure_ascii=False)


def to_toml(data: dict, language: str, voice: str) -> str:
    """El JSON de Gemini (ya comprobado) → guion.toml. Fuentes con los datos del CATÁLOGO, no los de Gemini."""
    out = ["# Guion escrito por el piloto automático (Gemini) y comprobado por la máquina. Revísalo antes de aprobar.",
           f"titulo = {_s(data['titulo'])}", f"descripcion = {_s(data['descripcion'])}", f"idioma = {_s(language)}",
           f"voz = {_s(voice)}", "etiquetas = [" + ", ".join(_s(t) for t in data.get("etiquetas", [])[:12]) + "]"]
    if data.get("miniatura_texto"):
        out.append(f"miniatura_texto = {_s(str(data['miniatura_texto'])[:40])}")
    corto = data.get("corto") if isinstance(data.get("corto"), dict) else {}
    # Gemini a veces se equivoca de número: se quedan solo las escenas que existen (sin repetir y en orden).
    total = len(data["escenas"])
    short = sorted({n for n in corto.get("escenas") or [] if isinstance(n, int) and 1 <= n <= total})
    if short:
        out += ["", "[corto]", "escenas = [" + ", ".join(map(str, short)) + "]"]
        if corto.get("titulo"):
            out.append(f"titulo = {_s(str(corto['titulo'])[:100])}")
    places: dict[str, list[str]] = {}
    for scene in data["escenas"]:
        for sid in scene.get("fuentes", []):
            place = " ".join(str(scene.get("lugar", "")).split())
            places.setdefault(sid, [])
            if place and place not in places[sid]:
                places[sid].append(place)
    for sid, where in places.items():
        entry = CATALOG[sid]
        work = entry["obra"] + (f" — {'; '.join(where)}" if where else "")
        out += ["", "[[fuentes]]", f"id = {_s(sid)}", f"obra = {_s(work[:200])}", f"autor = {_s(entry['autor'])}",
                f"url = {_s(entry['url'])}"]
    for scene in data["escenas"]:
        out += ["", "[[escenas]]", f"texto = {_s(scene['texto'])}", f"imagen = {_s(scene['imagen'])}"]
        if scene.get("lugar"):
            out.append(f"lugar = {_s(scene['lugar'])}")
        if scene.get("fuentes"):
            out.append("fuentes = [" + ", ".join(_s(f) for f in scene["fuentes"]) + "]")
        if scene.get("opinion"):
            out.append("opinion = true")
    return "\n".join(out) + "\n"


def count_words(data: dict) -> int:
    scenes = data.get("escenas") if isinstance(data.get("escenas"), list) else []
    return sum(len(str(s.get("texto", "")).split()) for s in scenes if isinstance(s, dict))


def check_shape(data: dict) -> list[str]:
    """Lo que se puede comprobar sin modelo: estructura, catálogo, número de escenas y longitud."""
    scenes = data.get("escenas")
    if not isinstance(scenes, list) or not MIN_SCENES <= len(scenes) <= MAX_SCENES:
        return [f"Debe tener entre {MIN_SCENES} y {MAX_SCENES} escenas"]
    problems = []
    for n, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict) or not str(scene.get("texto", "")).strip():
            problems.append(f"La escena {n} no tiene texto")
            continue
        sources = scene.get("fuentes") or []
        if not isinstance(sources, list):
            problems.append(f"Las fuentes de la escena {n} deben ser una lista de ids")
            continue
        unknown = [s for s in sources if s not in CATALOG]
        if unknown:
            problems.append(f"La escena {n} cita fuentes fuera del catálogo: {', '.join(map(str, unknown))}")
        if not sources and not scene.get("opinion"):
            problems.append(f"La escena {n} no cita ninguna fuente")
        if sources and not str(scene.get("lugar", "")).strip():
            problems.append(f"La escena {n} no dice en qué libro o capítulo de la fuente está")
    words = count_words(data)
    if not TARGET_WORDS[0] - 50 <= words <= TARGET_WORDS[1] + 150:   # margen: unas palabras de más o de menos dan igual
        problems.append(f"Tiene {words} palabras; debería tener entre {TARGET_WORDS[0]} y {TARGET_WORDS[1]}")
    if not str(data.get("titulo", "")).strip() or len(str(data.get("titulo", ""))) > 100:
        problems.append("Falta el título o pasa de 100 caracteres")
    return problems


class Writer:
    def __init__(self, gemini: GeminiClient, commons: CommonsClient,
                 progress: Callable[[str], None] = lambda _: None) -> None:
        self.gemini, self.commons, self.progress = gemini, commons, progress

    @staticmethod
    def _catalog() -> str:
        return "\n".join(f"- {sid}: {e['autor']}, {e['obra']}" for sid, e in CATALOG.items())

    # --- 1. idea --------------------------------------------------------------------

    def idea(self, done_titles: list[str], inspiration: list[str] | None = None, formato: str = "prueba",
             tema: str = "") -> dict:
        if formato not in FORMATS:
            raise WriterError(f"Formato desconocido: {formato}")
        prompt = (f"Catálogo de fuentes:\n{self._catalog()}\n\nVídeos que YA hicimos (no repitas la misma historia):\n"
                  + "\n".join(f"- {t}" for t in (done_titles[-60:] or ["(ninguno)"])))
        if inspiration:
            prompt += ("\n\nTítulos que están funcionando en YouTube en este nicho (solo inspiración, no copies):\n"
                       + "\n".join(f"- {t}" for t in inspiration[:15]))
        if tema:
            # Lo que Marc pidió a ARTHUR («un vídeo sobre Séneca»): orienta la idea, pero las reglas del nicho y del
            # catálogo siguen mandando (si no hay fuente para eso, Gemini elige lo más cercano que sí la tenga).
            prompt += f"\n\nMarc ha pedido este tema (respétalo si hay fuente en el catálogo): {tema[:200]}"
        prompt += ("\n\n" + FORMATS[formato] + " "
                   "Devuelve JSON: {\"tema\": \"…\", \"protagonista\": \"…\", \"gancho\": \"…\", "
                   "\"fuentes\": [\"id\", …], \"lugares\": \"libro/capítulo de cada fuente\"}")
        idea = self.gemini.generate_json(prompt, system=SYSTEM, temperature=0.9)
        sources = idea.get("fuentes")
        if not idea.get("tema") or not isinstance(sources, list) or not sources \
                or not all(isinstance(s, str) and s in CATALOG for s in sources):
            raise WriterError("Gemini ha propuesto una idea sin tema o con fuentes fuera del catálogo")
        if formato == "carta" and "seneca-cartas" not in sources:
            raise WriterError("Gemini ha propuesto una «carta» que no cita las Cartas a Lucilio")
        idea["formato"] = formato  # viaja con la idea hasta el guion (y queda en la salida del trabajo)
        return idea

    # --- 2. guion ---------------------------------------------------------------------

    def _write(self, idea: dict, fix: list[str] | None = None, previous: dict | None = None) -> dict:
        schema = ('{"titulo": "≤ 70 caracteres, con gancho", "descripcion": "2-3 frases", "etiquetas": ["…"], '
                  '"miniatura_texto": "2-3 PALABRAS", "corto": {"escenas": [3-4 números de escena seguidos que se '
                  'entiendan solos, menos de 50 s en total], "titulo": "…"}, "escenas": [{"texto": "lo que dice la '
                  'voz (35-70 palabras)", "fuentes": ["id del catálogo"], "lugar": "libro, capítulo o carta", '
                  '"imagen_busqueda": "qué buscar en Wikimedia Commons, EN INGLÉS (busto, cuadro, lugar…)", '
                  '"opinion": false}]}')
        if fix and previous:
            prompt = ("Este guion tiene problemas:\n- " + "\n- ".join(fix) + "\n\nCorrígelos SIN añadir datos que no "
                      f"estén en las fuentes. Mantén {MIN_SCENES}-{MAX_SCENES} escenas y {TARGET_WORDS[0]}-"
                      f"{TARGET_WORDS[1]} palabras en total (ahora tiene {count_words(previous)}): si quitas algo, "
                      f"desarrolla más lo que sí está en las fuentes. Guion:\n{json.dumps(previous, ensure_ascii=False)}"
                      "\n\nDevuelve el guion completo con el mismo formato JSON.")
        else:
            prompt = (f"Catálogo de fuentes:\n{self._catalog()}\n\nHistoria elegida:\n{json.dumps(idea, ensure_ascii=False)}"
                      f"\n\nFormato del vídeo: {FORMATS.get(idea.get('formato', 'prueba'), FORMATS['prueba'])}"
                      f"\n\nEscribe el guion: {MIN_SCENES}-{MAX_SCENES} escenas, {TARGET_WORDS[0]}-{TARGET_WORDS[1]} "
                      f"palabras en total. Devuelve SOLO este JSON:\n{schema}")
        return self.gemini.generate_json(prompt, system=SYSTEM, temperature=0.3 if fix else 0.6)

    def review(self, data: dict) -> list[str]:
        """Segunda pasada: Gemini como revisor estricto (temperatura 0)."""
        prompt = ("Eres un historiador muy estricto. Revisa este guion: busca datos inventados, fechas o cifras dudosas, "
                  "citas que no sean de esa obra, lugares (libro/capítulo) mal puestos y frases que afirmen más de lo "
                  "que dicen las fuentes. Devuelve JSON {\"problemas\": [\"…\"]} (lista vacía si todo está bien).\n\n"
                  + json.dumps(data, ensure_ascii=False))
        answer = self.gemini.generate_json(prompt, temperature=0.0)
        return [str(p)[:300] for p in answer.get("problemas", []) if str(p).strip()][:10]

    def script(self, idea: dict) -> tuple[dict, list[str]]:
        self.progress("Gemini escribe el guion")
        data = self._write(idea)
        problems = check_shape(data)
        if not problems:
            self.progress("Gemini revisa el guion")
            problems = self.review(data)
        if problems:
            data, problems = self._rewrite(idea, data, problems)
        if check_shape(data):  # una vuelta más solo para la forma (al quitar dudas a veces se queda corto)
            data, problems = self._rewrite(idea, data, check_shape(data))
        if check_shape(data):  # la estructura tiene que estar bien sí o sí; las dudas de contenido van a Marc
            raise WriterError("El guion sigue mal hecho tras reescribirlo: " + "; ".join(check_shape(data)))
        return data, problems

    def _rewrite(self, idea: dict, data: dict, problems: list[str]) -> tuple[dict, list[str]]:
        self.progress(f"reescribiendo ({len(problems)} problemas): " + "; ".join(problems)[:300])
        data = self._write(idea, problems, data)
        return data, check_shape(data) or self.review(data)

    # --- 3. imágenes ---------------------------------------------------------------------

    def images(self, data: dict, protagonist: str = "") -> dict:
        chosen: dict[str, str] = {}
        for n, scene in enumerate(data["escenas"], start=1):
            query = str(scene.get("imagen_busqueda") or protagonist or "stoic philosopher bust")[:150]
            if query not in chosen:
                self.progress(f"buscando imagen para la escena {n}: {query}")
                chosen[query] = self._find_image(query)
            scene["imagen"] = chosen[query]
        return data

    def _find_image(self, query: str) -> str:
        try:
            for title in self.commons.search(query, 8):
                try:
                    info = self.commons.info(title)
                except AssetError:
                    continue
                if info["ok"] and info["mime"] in ("image/jpeg", "image/png"):
                    return info["title"]
        except AssetError as e:
            log.warning("Studio: no se pudo buscar «%s» en Commons: %s", query, e)
        return FALLBACK_IMAGE

    # --- todo junto ----------------------------------------------------------------------

    def draft(self, idea: dict, language: str = "es", voice: str = "kokoro:ef_dora") -> Draft:
        data, problems = self.script(idea)
        data = self.images(data, str(idea.get("protagonista", "")))
        text = to_toml(data, language, voice)
        try:
            script = parse_script(tomllib.loads(text))  # las mismas reglas que un guion hecho a mano
        except (ScriptError, tomllib.TOMLDecodeError) as e:
            raise WriterError(f"El guion de Gemini no cumple las reglas: {e}") from None
        return Draft(script, text, problems, idea)

    def translate(self, draft_toml: str, language: str = "en", voice: str = "kokoro:af_heart") -> str:
        """El mismo guion en inglés (mismas escenas, imágenes y fuentes). Solo cambian los textos y la voz."""
        original = tomllib.loads(draft_toml)
        texts = {"titulo": original["titulo"], "descripcion": original["descripcion"],
                 "miniatura_texto": original.get("miniatura_texto", ""),
                 "corto_titulo": original.get("corto", {}).get("titulo", ""),
                 "escenas": [s["texto"] for s in original["escenas"]]}
        prompt = ("Traduce al inglés natural de YouTube (narración documental, frases cortas) este guion. Mantén el "
                  "mismo número de escenas y no añadas ni quites datos. Los títulos, con gancho para público "
                  "angloparlante. Devuelve el MISMO JSON con los textos traducidos:\n"
                  + json.dumps(texts, ensure_ascii=False))
        out = self.gemini.generate_json(prompt, temperature=0.3)
        scenes = out.get("escenas")
        if not isinstance(scenes, list) or len(scenes) != len(original["escenas"]) or \
                not all(isinstance(t, str) and t.strip() for t in scenes):
            raise WriterError("La traducción no tiene las mismas escenas que el original")
        translated = dict(original)
        translated.update({"titulo": out.get("titulo") or original["titulo"],
                           "descripcion": out.get("descripcion") or original["descripcion"],
                           "miniatura_texto": out.get("miniatura_texto") or original.get("miniatura_texto", ""),
                           "etiquetas": ["stoicism", "stoic stories", "philosophy", "ancient rome"],
                           "escenas": [{**s, "texto": t} for s, t in zip(original["escenas"], scenes)]})
        if original.get("corto"):
            translated["corto"] = {**original["corto"],
                                   "titulo": out.get("corto_titulo") or original["corto"].get("titulo", "")}
        text = to_toml(translated, language, voice)
        try:
            parse_script(tomllib.loads(text))
        except (ScriptError, tomllib.TOMLDecodeError) as e:
            raise WriterError(f"La traducción no cumple las reglas: {e}") from None
        return text
