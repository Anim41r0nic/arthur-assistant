"""El motor principal del chat de la web: Cloudflare Workers AI por su API REST, desde el servidor.

Decisión de Marc (2026-10-17): el chat público ya no usa Gemini. Las condiciones de la API de Gemini solo dejan
ofrecer un cliente a usuarios del Espacio Económico Europeo con los servicios DE PAGO, y Marc usa la clave gratuita.
Workers AI tiene una capa gratis de 10 000 «neuronas» al día (se renueva a las 00:00 UTC): con eso contesta; si se
acaba o falla, contesta Ollama (ver answer.py).

Una sola petición por pregunta:
    POST https://api.cloudflare.com/client/v4/accounts/{cuenta}/ai/run/{modelo}
    Authorization: Bearer <token>
    {"messages": [...], "max_completion_tokens": N}
El modelo por defecto (Gemma 4 26B A4B, disponible en el plan gratis) contesta al estilo OpenAI: el texto viene en
`result.choices[0].message.content`. Los modelos más antiguos (p. ej. Llama 3.3) lo dan en `result.response`: se
aceptan los dos para poder cambiar de modelo sin tocar código; si no hay ninguno, cuenta como fallo y contesta Ollama.
En la ficha de Gemma 4, `max_tokens` está obsoleto en favor de `max_completion_tokens`.

El token es un secreto: nunca va en un mensaje de error ni en el registro. Por eso los errores solo dicen el código
HTTP y el código numérico de Cloudflare, nunca el cuerpo ni la petición.

Biblioteca estándar, sin SDK.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timezone

from arthur.notes.engines import EngineError, EngineRefused

log = logging.getLogger(__name__)

CF_API = "https://api.cloudflare.com/client/v4"
# Gemma 4 26B A4B: en el plan gratis (algunos modelos grandes pasaron a pedir Workers Paid en 2026-07 y en el plan
# gratis contestan 403) y barato en neuronas (~22 por respuesta del chat).
DEFAULT_MODEL = "@cf/google/gemma-4-26b-a4b-it"
# Id de cuenta de Cloudflare: 32 caracteres hexadecimales. Se valida porque va DENTRO de la URL: así nadie puede
# colar «../» ni otra ruta de la API por una variable de entorno mal puesta.
ACCOUNT_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
# Los tokens de API de Cloudflare son 40 caracteres [A-Za-z0-9_-]; se deja algo de margen por si cambian el formato
# (como pasó con las claves de Google), pero SIN espacios ni saltos de línea: va en una cabecera.
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{20,256}$")
# «@cf/autor/modelo» (o «@hf/…»): también va en la URL. Nada de «..» ni caracteres raros.
MODEL_PATTERN = re.compile(r"^@(cf|hf)/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$")
# Código de Cloudflare para «se ha gastado la capa gratis de hoy» (va con un 429). Con él no tiene sentido volver a
# llamar hasta el día siguiente (UTC): cada visita se esperaría un 429 seguro.
DAILY_ALLOCATION_USED = 3036
MAX_TOKENS = 400       # ~120 palabras de respuesta (regla del prompt) caben de sobra; corta respuestas desbocadas
REQUEST_TIMEOUT = 20   # el mismo plazo que tenía Gemini: si no ha contestado en 20 s, contesta Ollama (40 s más)


def utc_day(now: datetime | None = None) -> date:
    """El «día» de la capa gratis de Workers AI: Cloudflare renueva todos los límites a las 00:00 UTC."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def _cf_codes(detail: bytes) -> set[int]:
    """Los códigos numéricos de `errors[].code` de una respuesta de error de Cloudflare (vacío si no se entiende)."""
    try:
        body = json.loads(detail)
        return {int(e["code"]) for e in body.get("errors") or [] if isinstance(e, dict) and "code" in e}
    except (ValueError, TypeError, KeyError, AttributeError):
        return set()


class WorkersAIEngine:
    name = "workers-ai"

    def __init__(self, account: str | None, token: str | None, model: str | None = None, *, api: str = CF_API,
                 opener: urllib.request.OpenerDirector | None = None,
                 day: Callable[[], date] = utc_day) -> None:
        if not account or not ACCOUNT_PATTERN.match(account):
            raise EngineRefused("Falta o no vale el id de cuenta de Cloudflare (ARTHUR_WEBCHAT_CF_ACCOUNT)")
        if not token or not TOKEN_PATTERN.match(token):
            # Ni el token ni un trozo en el mensaje: solo que no vale.
            raise EngineRefused("Falta o no vale el token de Workers AI")
        model = model or DEFAULT_MODEL
        if not MODEL_PATTERN.match(model) or ".." in model:
            raise EngineRefused("El modelo de Workers AI no tiene la forma @cf/autor/modelo")
        self.account, self.model = account, model
        self._token = token
        # El modelo lleva «@» y «/»: las barras separan autor y modelo en la ruta y se dejan tal cual (así lo
        # documenta Cloudflare); lo demás se codifica.
        self.url = f"{api.rstrip('/')}/accounts/{account}/ai/run/{urllib.parse.quote(model, safe='@/')}"
        self._opener = opener or urllib.request.build_opener()
        self._day = day
        self._exhausted_on: date | None = None   # día UTC en que Cloudflare dijo «capa gratis gastada»

    def __repr__(self) -> str:
        # Por si alguien registra el objeto entero: sin token.
        return f"WorkersAIEngine(model={self.model!r})"

    def ask(self, prompt: str, *, max_tokens: int = MAX_TOKENS, timeout: float = REQUEST_TIMEOUT) -> str:
        """Una pregunta de solo texto. Lanza EngineError (pasajero: red, 429, 5xx) o EngineRefused (token o modelo
        que Cloudflare no acepta); answer.py pasa entonces a Ollama."""
        if self._exhausted_on is not None and self._exhausted_on == self._day():
            # Sin llamar: hoy ya dijo que no queda capa gratis y no se renueva hasta las 00:00 UTC.
            raise EngineError("Workers AI: hoy ya no queda capa gratis")
        # Gemma 4 razona antes de contestar y ese razonamiento gasta los mismos tokens: con el prompt del chat se
        # comía los 400 y la respuesta llegaba vacía (visto en el servidor el 2026-10-17). enable_thinking=False
        # lo apaga (probado en real: 0 caracteres de razonamiento y unas 4 veces menos tokens de salida).
        body = {"messages": [{"role": "user", "content": prompt}], "max_completion_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False}}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self._token}",
                                                  "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as e:
            codes = _cf_codes(e.read(4096))
            e.close()
            raise self._http_error(e.code, codes) from None
        except (urllib.error.URLError, TimeoutError) as e:
            # El motivo (p. ej. «timed out», «Name or service not known») no lleva cabeceras: no hay token.
            reason = getattr(e, "reason", e)
            raise EngineError(f"No puedo conectar con Workers AI: {type(reason).__name__}") from None
        except ValueError:
            raise EngineError("Workers AI ha devuelto algo que no se entiende") from None
        text = self._text(answer)
        if not text:
            raise EngineError("Workers AI ha devuelto una respuesta vacía")
        return text

    def _http_error(self, status: int, codes: set[int]) -> EngineError:
        if status == 429:
            if DAILY_ALLOCATION_USED in codes:
                self._exhausted_on = self._day()
                log.info("Chat web: Workers AI sin capa gratis hasta las 00:00 UTC; contesta el modelo local")
                return EngineError("Workers AI: se ha gastado la capa gratis de hoy")
            return EngineError("Workers AI dice que hay demasiadas peticiones (429)")
        if status in (401, 403):
            # Token caducado, sin permiso «Workers AI», o un modelo que el plan gratis no deja usar (5035).
            return EngineRefused(f"Workers AI no acepta el token o el modelo (error {status}, códigos {sorted(codes)})")
        if status >= 500:
            return EngineError(f"Workers AI está ocupado (error {status})")
        return EngineRefused(f"Workers AI ha respondido con error {status} (códigos {sorted(codes)})")

    @staticmethod
    def _text(answer: object) -> str:
        if not isinstance(answer, dict):
            return ""
        result = answer.get("result")
        if not isinstance(result, dict):
            return ""
        try:
            # Formato OpenAI (el de Gemma 4). Solo `content`: el razonamiento interno, si lo hay, no es para la visita.
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = result.get("response")   # formato de los modelos antiguos
        return text.strip() if isinstance(text, str) else ""
