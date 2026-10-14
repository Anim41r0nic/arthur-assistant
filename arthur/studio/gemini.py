"""Gemini (Google) en su capa GRATUITA: el que escribe los guiones del piloto automático (paso 6, decisión de Marc
del 2026-10-12). Biblioteca estándar, sin el SDK de Google.

- Solo modelos «Flash» / «Flash-Lite» (los «Pro» ya no son gratis desde abril de 2026).
- La capa gratis da pocas peticiones al día (unas 20 con los Flash nuevos): se cuentan en studio.sqlite3 y se para
  ANTES de agotarlas. Si Google dice «demasiadas» (429), es pasajero: la cola lo reintenta más tarde.
- En la capa gratis Google puede usar lo que se le manda para mejorar sus productos: aquí solo van historias de los
  estoicos y fuentes públicas; nunca nada de Marc.
- La clave (de Google AI Studio) va en <datos>/studio/gemini-key (600) y viaja en la cabecera x-goog-api-key,
  nunca en la dirección.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .store import StudioStore, studio_dir
import os
import re

from .youtube import load_key, quota_day

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
API_NAME = "gemini"
DAILY_LIMIT = 18          # peticiones al día A CADA MODELO como mucho (la capa gratis de los Flash da unas 20 a cada uno)
# Por orden de preferencia: el primero que exista en la cuenta y admita generateContent. «flash-lite» al final:
# escribe algo peor pero tiene muchas más peticiones gratis.
PREFERRED = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash",
             "gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite")
MAX_PROMPT = 60_000       # caracteres: un guion con sus fuentes cabe de sobra
# Google ha cambiado el formato de las claves nuevas de AI Studio (ya no siempre «AIza…» de 39 caracteres). Se acepta
# cualquier cosa razonable SIN espacios ni saltos de línea (va en una cabecera); quien de verdad la valida es Google.
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{20,256}$")


class GeminiError(Exception):
    """Pasajero (red, Google ocupado, límite por minuto): se puede reintentar más tarde."""


class GeminiBusy(GeminiError):
    """Ese modelo está saturado (503) o no contesta a tiempo: se puede probar con otro modelo gratuito."""


class GeminiGone(GeminiBusy):
    """Google ya no da ese modelo (404): se pasa a otro y no se vuelve a probar."""


class GeminiRefused(GeminiError):
    """No se arregla repitiendo: clave mala, sin modelos gratis, límite del día agotado."""


def key_file() -> Path:
    return studio_dir() / "gemini-key"


def save_gemini_key(key: str, path: Path | None = None) -> None:
    key = key.strip()
    if not KEY_PATTERN.match(key):
        raise ValueError("Eso no parece una clave de Gemini (copia la clave entera, sin espacios)")
    path = path or key_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    os.replace(tmp, path)


def load_gemini_key() -> str | None:
    return load_key(key_file())


class GeminiClient:
    def __init__(self, key: str, store: StudioStore, *, model: str | None = None, api: str = API,
                 opener: urllib.request.OpenerDirector | None = None, now: Callable[[], datetime] = datetime.now,
                 daily_limit: int = DAILY_LIMIT) -> None:
        if not KEY_PATTERN.match(key or ""):
            raise GeminiRefused("La clave de Gemini no es válida. Guárdala con: arthur --studio-clave-gemini")
        self._key = key
        self.store = store
        self.api = api
        self._opener = opener or urllib.request.build_opener()
        self.now = now
        self.daily_limit = daily_limit
        self._model = model
        self._spare: list[str] = []      # los modelos gratuitos de la cuenta, por orden (se rellena al elegir)
        self._tried: set[str] = set()    # los que ya dieron «saturado» en esta ronda
        self._gone: set[str] = set()     # los retirados (404): no se vuelven a probar
        self.last_finish = "?"           # por qué terminó la última respuesta (para los mensajes de error)

    def _request(self, method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.api}/{path}", data=data, method=method,
                                         headers={"x-goog-api-key": self._key, "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read(4096).decode("utf-8", errors="replace")
            e.close()
            if e.code == 429:
                raise GeminiError("Gemini dice que hay demasiadas peticiones ahora mismo: se reintentará") from None
            if e.code in (400, 403) and ("API_KEY" in detail or "API key" in detail):
                raise GeminiRefused("Google no acepta la clave de Gemini: créala otra vez en aistudio.google.com/apikey "
                                    "y guárdala con --studio-clave-gemini") from None
            if e.code == 404 and ":generateContent" in path:
                # Google lo sigue listando pero ya no lo da a cuentas nuevas (2.5-flash, visto el 2026-10-13): otro.
                raise GeminiGone("Ese modelo de Gemini ya no está disponible") from None
            if e.code in (500, 502, 503, 504):
                raise GeminiBusy(f"Gemini está ocupado (error {e.code}): se reintentará") from None
            raise GeminiRefused(f"Gemini ha respondido con error {e.code}") from None
        except TimeoutError:
            raise GeminiBusy("Gemini no ha contestado a tiempo: se reintentará") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise GeminiBusy("Gemini no ha contestado a tiempo: se reintentará") from None
            raise GeminiError(f"No puedo conectar con Gemini: {e.reason}") from None
        except ValueError:
            raise GeminiError("Gemini ha devuelto algo que no se entiende") from None

    # --- modelo ---------------------------------------------------------------------

    def models(self) -> list[str]:
        body = self._request("GET", "models?pageSize=200")
        return [m["name"].removeprefix("models/") for m in body.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]

    @property
    def model(self) -> str:
        if self._model is None:
            available = set(self.models())
            # Los de reserva por si el elegido está saturado (el 2026-10-13, 3.8-flash daba 503 toda la mañana).
            self._spare = [m for m in PREFERRED if m in available]
            chosen = next((m for m in PREFERRED if m in available), None)
            if chosen is None:
                # Algún «flash» que no esté en la lista (nombres nuevos), pero nunca un «pro» (no es gratis).
                chosen = next((m for m in sorted(available, reverse=True) if "flash" in m and "pro" not in m
                               and "image" not in m and "tts" not in m and "live" not in m), None)
            if chosen is None:
                raise GeminiRefused("Tu clave no tiene ningún modelo Flash gratuito disponible")
            self._model = chosen
            log.info("Studio: Gemini con el modelo %s", chosen)
        return self._model

    # --- escribir -------------------------------------------------------------------------

    def used_today(self, model: str | None = None) -> int:
        """Peticiones de hoy a ESE modelo (el elegido si no se dice): Google pone el límite gratis por modelo."""
        model = model or self._model
        return self.store.usage(quota_day(self.now()), f"{API_NAME}:{model}") if model else 0

    def _next_model(self, model: str) -> str | None:
        """El siguiente gratuito de la lista que no haya fallado ya en este cliente ni haya agotado lo de hoy."""
        self._tried.add(model)
        return next((m for m in self._spare if m not in self._tried | self._gone
                     and self.used_today(m) < self.daily_limit), None)

    def generate(self, prompt: str, *, system: str = "", as_json: bool = False, temperature: float = 0.7) -> str:
        if len(prompt) + len(system) > MAX_PROMPT:
            raise GeminiRefused("La petición a Gemini es demasiado larga")
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": temperature}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if as_json:
            body["generationConfig"]["responseMimeType"] = "application/json"
        while True:
            model = self.model
            if self.used_today(model) >= self.daily_limit:
                spare = self._next_model(model)
                if spare is None:
                    raise GeminiRefused(f"Hoy ya se han hecho {self.daily_limit} peticiones a cada modelo gratuito de "
                                        "Gemini (para no pasar de la capa gratis): mañana se sigue")
                log.info("Studio: %s ya ha hecho lo de hoy; sigo con %s", model, spare)
                self._model = spare
                continue
            # Se apunta antes: también cuentan las que fallan.
            self.store.add_usage(quota_day(self.now()), f"{API_NAME}:{model}", 1)
            try:
                # Los Flash nuevos «piensan» antes de contestar: un guion entero puede pasar de 2 minutos.
                answer = self._request("POST", f"models/{model}:generateContent", body, timeout=300)
                break
            except GeminiBusy as e:
                # Saturado o retirado: se pasa al siguiente modelo gratuito de la lista y se sigue con él.
                if isinstance(e, GeminiGone):
                    self._gone.add(model)
                spare = self._next_model(model)
                if spare is None:
                    # Todos fallaron en esta ronda: el próximo reintento empieza otra vez por el mejor.
                    self._tried.clear()
                    self._model = next((m for m in self._spare if m not in self._gone), self._model)
                    raise
                log.warning("Studio: %s está saturado; pruebo con %s", model, spare)
                self._model = spare
        candidates = answer.get("candidates", [])
        if not candidates:
            reason = answer.get("promptFeedback", {}).get("blockReason", "sin respuesta")
            raise GeminiRefused(f"Gemini no ha querido responder ({reason})")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        self.last_finish = candidates[0].get("finishReason", "?")   # «MAX_TOKENS» = se cortó por largo
        if not text:
            raise GeminiError(f"Gemini ha devuelto una respuesta vacía ({self.last_finish})")
        return text

    def generate_json(self, prompt: str, *, system: str = "", temperature: float = 0.7) -> dict:
        text = self.generate(prompt, system=system, as_json=True, temperature=temperature)
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        # Los modelos «lite» a veces ponen una frase antes o después aunque se les pida solo JSON.
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start:end + 1] if 0 <= start < end else text)
        except ValueError:
            raise GeminiError(f"Gemini no ha devuelto JSON válido ({len(text)} caracteres, "
                              f"motivo de fin: {self.last_finish})") from None
        if not isinstance(data, dict):
            raise GeminiError("Gemini no ha devuelto un objeto JSON")
        return data
