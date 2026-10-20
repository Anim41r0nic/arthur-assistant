"""Los «motores» que leen una foto de apuntes y devuelven el texto (Markdown con fórmulas en LaTeX).

Dos que leen imágenes, a elegir en cada nota (decisión de Marc del 2026-10-12):

- gemini: Google Gemini, capa GRATUITA (clave de aistudio.google.com en <datos>/notes/gemini-key, 600). El que mejor
  lee la letra a mano y las fórmulas, y tarda segundos. OJO: en la capa gratis Google puede usar lo que se le manda
  para mejorar sus productos; la página lo avisa. Se prueban primero los «Flash» (mejores) y, si Google dice que
  se ha acabado lo gratis de hoy (429), los «Flash-Lite» (muchas más páginas gratis al día).
- local: qwen3-vl en el Ollama del servidor (el mismo modelo que el cerebro de ARTHUR, así no se cargan dos).
  Nada sale de casa, pero tarda ~1-2 min por página y lee peor la letra a mano.

Y dos que no son «motores» de verdad (worker.py los resuelve página a página, decisión del 2026-10-13):

- texto: solo el texto que ya trae el PDF (pdftotext), sin IA y sin salir del servidor. Ideal para el material de
  los profes; las fórmulas salen como texto plano, no en LaTeX.
- auto (por defecto): el texto del PDF si lo tiene y, si la página es una foto o un escaneo, Gemini (o el local si
  no hay clave de Gemini).

Biblioteca estándar, sin el SDK de Google.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

ENGINES = ("auto", "gemini", "local", "texto")
AI_ENGINES = ("gemini", "local")
NAMES = {"auto": "Automático", "gemini": "Gemini", "local": "Local", "texto": "Texto del PDF"}
# Google ha cambiado el formato de las claves nuevas de AI Studio (ya no siempre «AIza…» de 39 caracteres; igual que
# en studio/gemini.py). Se acepta cualquier cosa razonable SIN espacios ni saltos de línea (va en una cabecera);
# quien de verdad la valida es Google.
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{20,256}$")
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash",
                 "gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite")
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3-vl:4b-instruct"
# El MISMO contexto que el cerebro de ARTHUR (`brain.context_tokens`, 8192): si una petición pide otro num_ctx,
# Ollama descarga el modelo y lo vuelve a cargar con ese tamaño (y la siguiente pregunta de ARTHUR, otra vez). Sin
# num_ctx se usa el de Ollama (normalmente 4096), que no es el del cerebro: cada respuesta del chat de la web y cada
# resumen del panel recargaban el modelo de 4,3 GB y, con un historial largo, Ollama cortaba el principio del prompt
# (las reglas) para que cupiera.
OLLAMA_NUM_CTX = 8192
OLLAMA_KEEP_ALIVE = "30m"
_KEEP_ALIVE = re.compile(r"^-?\d{1,6}(s|m|h)?$")  # «30m», «5m», «-1m» (siempre), «0» (descargar al acabar)
MIME = {".jpg": "image/jpeg", ".png": "image/png"}

PROMPT = """Eres un transcriptor de apuntes universitarios (ingeniería informática: cálculo, álgebra, matemática \
discreta, física, programación). Transcribe la página de la imagen EXACTAMENTE, sin resumir, sin traducir, sin \
corregir el contenido y sin añadir nada que no esté escrito.

Formato de la respuesta: solo Markdown, sin ``` alrededor y sin comentarios tuyos.
- Títulos con #, ## y ###; listas con - o 1.; tablas con | ... |; negrita para lo subrayado o destacado.
- TODA fórmula o símbolo matemático en LaTeX estándar (amsmath): en línea con $...$ y las fórmulas sueltas con \
$$...$$ en su propia línea. Ejemplos: $\\int_a^b f(x)\\,dx$, $\\sum_{i=1}^{n} i$, $\\forall x \\in A$, \
$p \\land \\neg q$, $\\binom{n}{k}$, matrices con \\begin{pmatrix}, funciones a trozos con \\begin{cases}.
- Código de programación entre ``` con su lenguaje.
- Mantén el idioma original (castellano, catalán o inglés).
- Lo que no se lea bien: [?]. Dibujos, gráficas o esquemas: una línea en cursiva *[Dibujo: qué muestra]*.
- Si la página está en blanco o no tiene apuntes, responde solo: *[Página sin texto]*"""


class EngineError(Exception):
    """Pasajero (red, servicio ocupado): se puede reintentar más tarde."""


class EngineRefused(EngineError):
    """No se arregla repitiendo: falta la clave, clave mala, imagen rechazada."""


# --- la clave de Gemini ---------------------------------------------------------------------------------------

def save_key(key: str, path: Path) -> None:
    """Fichero 600 en una carpeta 700. Se escribe primero a un temporal para no dejarlo a medias."""
    key = key.strip()
    if not KEY_PATTERN.match(key):
        raise ValueError("Eso no parece una clave de API de Google: cópiala entera, sin espacios")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    os.replace(tmp, path)


def load_key(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def clean_answer(text: str) -> str:
    """Quita el ```markdown ... ``` que a veces envuelve TODA la respuesta (el de los trozos de código se queda)."""
    text = text.strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", text, re.S)
    return (match.group(1) if match else text).strip()


def _image(path: Path) -> tuple[str, str]:
    mime = MIME.get(path.suffix.lower())
    if mime is None:
        raise EngineRefused(f"Tipo de imagen no admitido: {path.suffix}")
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


# --- Gemini ---------------------------------------------------------------------------------------------------------

class _TooMany(Exception):
    pass


class _Busy(Exception):
    """Ese modelo no contesta ahora (503, se cuelga): se prueba el siguiente en vez de esperarlo."""


class _Gone(Exception):
    """Ese modelo ya no existe (404: Google lo retira): se aparta para siempre."""


BUSY_PAUSE = 600  # s que se deja tranquilo un modelo saturado (visto el 2026-10-13: los Flash grandes, colgados)
REQUEST_TIMEOUT = 45  # una página tarda segundos (medido: 1-2 s); 45 s sin contestar es que está atascado
ASK_TIMEOUT = 75      # una explicación o un examen inventado es más largo que una página: más margen por modelo…
ASK_DEADLINE = 90     # … pero en total menos de 100 s, lo que Cloudflare deja esperar a una página
# Qué modelos están saturados, para TODO el proceso: el motor se crea de nuevo en cada apunte, y sin esto cada apunte
# volvería a esperar a los mismos modelos colgados (visto el 2026-10-13: 283 s para una página).
_BUSY: dict[str, float] = {}


class GeminiEngine:
    name = "gemini"

    def __init__(self, key: str | None, *, api: str = GEMINI_API, models: tuple[str, ...] = GEMINI_MODELS,
                 opener: urllib.request.OpenerDirector | None = None,
                 clock: Callable[[], float] = time.monotonic, busy: dict[str, float] | None = None) -> None:
        if not key or not KEY_PATTERN.match(key):
            raise EngineRefused("Falta la clave de Gemini. En el servidor: arthur --apuntes-clave-gemini")
        self._key = key
        self.api = api
        self.models = models
        self._opener = opener or urllib.request.build_opener()
        self._available: list[str] | None = None
        self._busy = _BUSY if busy is None else busy  # modelo -> hasta cuándo no se le pregunta
        self._clock = clock
        self._exhausted: set[str] = set()  # sin capa gratis hoy (429 «por día»): se prueba el siguiente

    def _request(self, method: str, path: str, body: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.api}/{path}", data=data, method=method,
                                         headers={"x-goog-api-key": self._key, "Content-Type": "application/json"})
        with self._opener.open(request, timeout=timeout) as response:
            return json.loads(response.read())

    def _call(self, method: str, path: str, body: dict | None = None, *, model: bool = False,
              timeout: int = REQUEST_TIMEOUT) -> dict:
        """`model`: es una petición a UN modelo; si ese modelo está saturado o retirado se dice con _Busy/_Gone
        para probar el siguiente (los demás fallos valen para todos y se dicen igual que siempre)."""
        try:
            return self._request(method, path, body, timeout)
        except urllib.error.HTTPError as e:
            detail = e.read(4096).decode("utf-8", errors="replace")
            e.close()
            if e.code == 429:
                raise _TooMany(detail) from None
            if e.code in (400, 403) and ("API_KEY" in detail or "API key" in detail):
                raise EngineRefused("Google no acepta la clave de Gemini: créala otra vez en aistudio.google.com/apikey "
                                    "y guárdala con --apuntes-clave-gemini") from None
            if e.code == 404 and model:
                raise _Gone() from None
            if e.code in (500, 502, 503, 504) and model:
                raise _Busy(f"error {e.code}") from None
            if e.code in (500, 502, 503, 504):
                raise EngineError(f"Gemini está ocupado (error {e.code}): se reintentará") from None
            raise EngineRefused(f"Gemini ha respondido con error {e.code}") from None
        except TimeoutError as e:
            if model:
                raise _Busy("no contesta") from None
            raise EngineError(f"No puedo conectar con Gemini: {e}") from None
        except urllib.error.URLError as e:
            if model and isinstance(e.reason, TimeoutError):
                raise _Busy("no contesta") from None
            raise EngineError(f"No puedo conectar con Gemini: {getattr(e, 'reason', e)}") from None
        except ValueError:
            raise EngineError("Gemini ha devuelto algo que no se entiende") from None

    def _candidates(self, until: float | None = None) -> list[str]:
        if self._available is None:
            # Con plazo total (`ask`), la lista de modelos también cuenta: si no, con 45 s fijos se comía todo el
            # plazo del chat web (20 s) antes de preguntar a ningún modelo. Sin plazo (Apuntes), como siempre.
            timeout = REQUEST_TIMEOUT
            if until is not None:
                left = until - self._clock()
                if left < 5:
                    raise EngineError("Gemini está tardando demasiado ahora mismo: prueba otra vez en un rato")
                timeout = min(REQUEST_TIMEOUT, int(left))
            body = self._call("GET", "models?pageSize=200", timeout=timeout)
            names = {m["name"].removeprefix("models/") for m in body.get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])}
            self._available = [m for m in self.models if m in names]
            if not self._available:
                raise EngineRefused("Tu clave de Gemini no tiene ningún modelo Flash gratuito disponible")
            log.info("Apuntes: modelos de Gemini disponibles: %s", ", ".join(self._available))
        now = self._clock()
        return [m for m in self._available if m not in self._exhausted and self._busy.get(m, 0) <= now]

    def transcribe(self, image: Path) -> str:
        mime, data = _image(image)
        return self._generate([{"text": PROMPT}, {"inline_data": {"mime_type": mime, "data": data}}], temperature=0,
                              what="leer esta página")

    def ask(self, prompt: str, *, deadline: float = ASK_DEADLINE) -> str:
        """Una pregunta de texto (la «IA» de la página: explicar, hacer ejemplos…). Se da por perdida a los
        `deadline` segundos aunque queden modelos: la página espera la respuesta y, si se entra por Cloudflare,
        Cloudflare corta a los 100 s."""
        return self._generate([{"text": prompt}], temperature=0.4, what="contestar", timeout=ASK_TIMEOUT,
                              until=self._clock() + deadline)

    def _generate(self, parts: list, *, temperature: float, what: str, timeout: int = REQUEST_TIMEOUT,
                  until: float | None = None) -> str:
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": temperature}}
        busy = []
        for model in self._candidates(until):
            left = None if until is None else until - self._clock()
            if left is not None and left < 5:
                raise EngineError("Gemini está tardando demasiado ahora mismo: prueba otra vez en un rato")
            try:
                answer = self._call("POST", f"models/{model}:generateContent", body, model=True,
                                    timeout=timeout if left is None else max(5, min(timeout, int(left))))
            except _Busy as e:
                log.info("Apuntes: %s saturado (%s); pruebo el siguiente y lo dejo %d min", model, e, BUSY_PAUSE // 60)
                self._busy[model] = self._clock() + BUSY_PAUSE
                busy.append(model)
                continue
            except _Gone:
                log.info("Apuntes: %s ya no existe en Gemini; lo aparto", model)
                self._exhausted.add(model)
                continue
            except _TooMany as e:
                if "perday" in str(e).lower().replace(" ", ""):
                    log.info("Apuntes: %s sin capa gratis por hoy; pruebo el siguiente", model)
                    self._exhausted.add(model)
                    continue
                raise EngineError("Gemini dice que hay demasiadas peticiones seguidas: se reintentará") from None
            candidates = answer.get("candidates", [])
            if not candidates:
                reason = answer.get("promptFeedback", {}).get("blockReason", "sin respuesta")
                raise EngineRefused(f"Gemini no ha querido {what} ({reason})")
            parts_out = candidates[0].get("content", {}).get("parts", [])
            text = clean_answer("".join(p.get("text", "") for p in parts_out))
            if not text:
                raise EngineError(f"Gemini ha devuelto una respuesta vacía ({candidates[0].get('finishReason', '?')})")
            return text
        if busy or any(until_ > self._clock() for until_ in self._busy.values()):
            raise EngineError("Todos los modelos de Gemini están saturados ahora mismo: se reintentará")
        raise EngineError("Hoy se ha acabado lo gratis de Gemini: se seguirá mañana (o usa el motor local)")


# --- qwen3-vl en el Ollama del servidor --------------------------------------------------------------------------

def _num_ctx(raw: str | None) -> int:
    """`ARTHUR_OLLAMA_NUM_CTX` solo hace falta si el cerebro del servidor usa otro `brain.context_tokens`: tiene que
    ser el mismo número. Un valor raro no tumba nada: se usa el de siempre."""
    try:
        value = int(raw or OLLAMA_NUM_CTX)
    except ValueError:
        return OLLAMA_NUM_CTX
    return value if 2048 <= value <= 131072 else OLLAMA_NUM_CTX


class LocalEngine:
    name = "local"

    def __init__(self, url: str | None = None, model: str | None = None,
                 opener: urllib.request.OpenerDirector | None = None) -> None:
        self.url = (url or os.environ.get("ARTHUR_OLLAMA_URL") or OLLAMA_URL).rstrip("/")
        self.model = model or os.environ.get("ARTHUR_NOTES_MODEL") or OLLAMA_MODEL
        self.num_ctx = _num_ctx(os.environ.get("ARTHUR_OLLAMA_NUM_CTX"))
        # Cuánto sigue el modelo en RAM tras usarlo (~5 GB en el servidor). Cada petición reinicia la cuenta con SU
        # valor, así que debe ser el mismo que el `brain.keep_alive` del core: si no, el último que pregunta manda.
        keep_alive = (os.environ.get("ARTHUR_OLLAMA_KEEP_ALIVE") or "").strip()
        self.keep_alive = keep_alive if _KEEP_ALIVE.match(keep_alive) else OLLAMA_KEEP_ALIVE
        self._opener = opener or urllib.request.build_opener()

    def transcribe(self, image: Path) -> str:
        _, data = _image(image)
        body = {"model": self.model, "stream": False, "keep_alive": self.keep_alive,
                "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": 3000},
                "messages": [{"role": "user", "content": PROMPT, "images": [data]}]}
        answer = self._chat(body, timeout=900)  # ~95 s la página en el servidor
        text = clean_answer(answer.get("message", {}).get("content", ""))
        if not text:
            raise EngineError("El modelo local ha devuelto una respuesta vacía")
        return text

    def _chat(self, body: dict, *, timeout: int) -> dict:
        request = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read(2048).decode("utf-8", errors="replace")
            e.close()
            if e.code == 404:
                raise EngineRefused(f"El servidor no tiene el modelo {self.model} (ollama pull {self.model})") from None
            raise EngineError(f"Ollama ha respondido con error {e.code}: {detail[:200]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise EngineError(f"No puedo conectar con Ollama: {getattr(e, 'reason', e)}") from None
        except ValueError:
            raise EngineError("Ollama ha devuelto algo que no se entiende") from None

    def ask(self, prompt: str, *, num_predict: int = 600, timeout: int = 120) -> str:
        """Pregunta de solo texto (el resumen de la mañana del panel): sin imagen, algo de creatividad, corto.
        `num_predict` y `timeout` se pueden bajar (el chat público no espera tanto); por defecto, como siempre."""
        body = {"model": self.model, "stream": False, "keep_alive": self.keep_alive,
                "options": {"temperature": 0.4, "num_ctx": self.num_ctx, "num_predict": num_predict},
                "messages": [{"role": "user", "content": prompt}]}
        text = clean_answer(self._chat(body, timeout=timeout).get("message", {}).get("content", ""))
        if not text:
            raise EngineError("El modelo local ha devuelto una respuesta vacía")
        return text
