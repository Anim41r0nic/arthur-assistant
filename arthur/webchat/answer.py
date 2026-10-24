"""Quién contesta al visitante: Cloudflare Workers AI, el modelo local (Ollama) o un mensaje fijo.

Sin Gemini a propósito (decisión de Marc, 2026-10-17): sus condiciones solo permiten ofrecer un cliente a usuarios
del Espacio Económico Europeo con la API de pago, y la clave de Marc es la gratuita. Ver workers_ai.py.

El limitador reserva el hueco del día ANTES de llamar a nadie (así dos visitas a la vez no se pasan del cupo).
Si Workers AI falla se devuelve su hueco y contesta el local; si todo falla, el mensaje fijo con el correo de Marc:
el visitante nunca se queda sin respuesta ni ve un error técnico.
"""

from __future__ import annotations

import http.client
import logging
import os
import time
from collections.abc import Callable

from arthur.notes.engines import EngineError
from arthur.webchat.knowledge import Knowledge
from arthur.webchat.limits import RateLimiter
from arthur.webchat.prompt import build_local_prompt, build_prompt, clean_reply, norm_lang

log = logging.getLogger(__name__)

_CORREO = os.environ.get("ARTHUR_WEBCHAT_CORREO", "").strip() or "hola@ejemplo.com"
_NOMBRE = os.environ.get("ARTHUR_WEBCHAT_NOMBRE", "").strip()

# Textos provisionales: cámbialos por los tuyos. El nombre y el correo salen de ARTHUR_WEBCHAT_NOMBRE y
# ARTHUR_WEBCHAT_CORREO (.env); sin nombre se usa una forma genérica en cada idioma.
FIXED = {
    "es": f"Ahora no puedo responder con IA. Si quieres hablar con {_NOMBRE or 'quien lleva la web'}, escribe a {_CORREO}.",
    "en": f"I can't answer with AI right now. To reach {_NOMBRE or 'the site owner'}, email {_CORREO}.",
    "ca": f"Ara no puc respondre amb IA. Si vols parlar amb {_NOMBRE or 'qui porta el web'}, escriu a {_CORREO}.",
}

# Lo que puede lanzar una llamada remota: nuestro EngineError y la familia de red (URLError, timeouts y cortes
# son OSError; una lectura a medias es HTTPException). No se captura Exception a secas: un fallo de programación
# no debe quedar escondido tras un «motor caído».
_FALLOS = (EngineError, OSError, http.client.HTTPException)

# Tiempos. El chat de la web espera 70 s (js/chat.js del portfolio) y nginx 90 s: entre los dos motores no se pasa de
# ANSWER_BUDGET. Workers AI tiene hasta 20 s; Ollama, lo que quede (si Workers AI falla al momento, ~60 s; si se
# agota, ~42 s; nunca menos de LOCAL_MIN_TIMEOUT). Antes eran 40 s fijos, menos de lo que tarda el servidor en
# leer el prompt completo con el procesador.
ANSWER_BUDGET = 62
WORKERS_AI_TIMEOUT = 20
LOCAL_MIN_TIMEOUT = 15
# El prompt del local pide ~60 palabras (~100 piezas). En el servidor escribe ~5 piezas por segundo: 160 deja acabar
# la frase y corta a los ~30 s a un modelo que se enrolle (con 400 eran hasta 80 s solo escribiendo).
LOCAL_NUM_PREDICT = 160


class Answerer:
    def __init__(self, knowledge: Knowledge, limiter: RateLimiter, workers_ai, local,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """`workers_ai`: el motor de Cloudflare Workers AI (None = sin cuenta o token: contesta el local).
        El motor se acuerda solo de si hoy (día UTC) ya se gastó la capa gratis; no hace falta rehacerlo cada día."""
        self._knowledge, self._limiter, self._workers_ai, self._local = knowledge, limiter, workers_ai, local
        self._clock = clock

    def reply(self, history: list[dict], message: str, lang: str) -> dict:
        lang = norm_lang(lang)
        # Sin conocimiento no hay respuesta posible; se mira ANTES de reservar para que un fichero que falta
        # no se coma el cupo del día.
        text = self._knowledge.text()
        if text is None:
            return self._fixed(lang)
        turn = self._limiter.reserve_answer()
        if turn == "fijo":
            return self._fixed(lang)
        prices = self._knowledge.has_price_placeholder()
        start = self._clock()
        if turn == "workers-ai":
            prompt = build_prompt(text, history, message, lang, prices)
            found = self._try(self._workers_ai, prompt, max_tokens=400, timeout=WORKERS_AI_TIMEOUT)
            if found is not None:
                return self._ok(found, "workers-ai")
            # Workers AI no ha contestado (429, cupo gastado, red…): su hueco vuelve y se intenta con el local.
            self._limiter.release_workers_ai()
        # El local recibe su prompt corto (ver prompt.py): con el completo no llega a tiempo en el servidor.
        left = int(ANSWER_BUDGET - (self._clock() - start))
        found = self._try(self._local, build_local_prompt(text, history, message, lang, prices),
                          num_predict=LOCAL_NUM_PREDICT, timeout=max(LOCAL_MIN_TIMEOUT, left))
        if found is not None:
            return self._ok(found, "local")
        return self._fixed(lang)

    @staticmethod
    def _try(engine, prompt: str, **kw) -> tuple[str, bool] | None:
        if engine is None:
            return None
        try:
            raw = engine.ask(prompt, **kw)
        except _FALLOS as e:
            log.info("Chat web: %s no contesta (%s)", getattr(engine, "name", "motor"), type(e).__name__)
            return None
        reply, mark = clean_reply(raw or "")
        # Una respuesta vacía (o solo la marca de contacto) no le sirve a nadie: cuenta como fallo.
        return (reply, mark) if reply else None

    @staticmethod
    def _ok(found: tuple[str, bool], engine: str) -> dict:
        return {"reply": found[0], "offer_contact": found[1], "engine": engine}

    @staticmethod
    def _fixed(lang: str) -> dict:
        # Se ofrece el contacto porque el texto fijo ya lleva el correo de Marc.
        return {"reply": FIXED[lang], "offer_contact": True, "engine": "fijo"}
