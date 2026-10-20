"""Embeddings de Workers AI (`@cf/baai/bge-m3`): convierten un trozo de texto en 1 024 números que dicen «de qué va».

Dos textos que significan lo mismo con palabras distintas («a qué tiende la serie» y «límite de una sucesión»)
dan vectores parecidos: así el cerebro encuentra un apunte aunque Marc no use sus mismas palabras. bge-m3 es
multilingüe, así que también acerca el catalán al castellano.

Una petición por lote de hasta 50 textos:
    POST https://api.cloudflare.com/client/v4/accounts/{cuenta}/ai/run/@cf/baai/bge-m3
    {"text": ["…", "…"]}  →  {"result": {"shape": [n, 1024], "data": [[…], …]}}

Antes de cada lote se pregunta al presupuesto (budget.py): si no cabe, se lanza EngineError SIN llamar. Los
embeddings esperan entonces a mañana y ese trozo se sigue encontrando por palabras. El token es un secreto: como en
webchat/workers_ai.py, los errores solo dicen el código HTTP y los códigos de Cloudflare, nunca el cuerpo.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request

from ..notes.engines import EngineError, EngineRefused
from ..webchat.workers_ai import ACCOUNT_PATTERN, CF_API, DAILY_ALLOCATION_USED, TOKEN_PATTERN, _cf_codes
from .budget import Budget, Kind, approx_tokens, estimate

log = logging.getLogger(__name__)

MODEL = "@cf/baai/bge-m3"
BATCH = 50
DIM = 1024
TIMEOUT = 30
# float16 (como se guardan en el índice) no llega más allá de 65 504. bge-m3 da vectores de longitud 1, así que un
# número fuera de ese rango solo puede ser una respuesta rota: mejor rechazarla que guardar un infinito.
_MAX_ABS = 65504.0


class Embedder:
    def __init__(self, account: str | None, token: str | None, *, api: str = CF_API,
                 opener: urllib.request.OpenerDirector | None = None, budget: Budget,
                 timeout: float = TIMEOUT) -> None:
        if not account or not ACCOUNT_PATTERN.match(account):
            raise EngineRefused("Falta o no vale el id de cuenta de Cloudflare (ARTHUR_WEBCHAT_CF_ACCOUNT)")
        if not token or not TOKEN_PATTERN.match(token):
            raise EngineRefused("Falta o no vale el token de Workers AI")
        self._token = token
        self.url = f"{api.rstrip('/')}/accounts/{account}/ai/run/{MODEL}"
        self._opener = opener or urllib.request.build_opener()
        self.budget = budget
        self.timeout = timeout

    def __repr__(self) -> str:
        return "Embedder(model='@cf/baai/bge-m3')"  # sin token, por si alguien registra el objeto

    def embed(self, texts: list[str], *, kind: Kind = "embed") -> list[list[float]]:
        """Un vector de 1 024 números por texto, en el mismo orden.

        `kind` dice a qué parte del presupuesto se carga: «embed» para indexar (puede esperar) y «ask» para la
        pregunta de Marc (va primero: si no, con los embeddings del índice a tope no se podría ni buscar)."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = [str(t) for t in texts[start:start + BATCH]]
            cost = estimate("bge-m3", sum(approx_tokens(t) for t in batch), 0)
            if not self.budget.allows(kind, cost):
                raise EngineError("Presupuesto del cerebro gastado por hoy: los embeddings esperan a mañana")
            vectors.extend(self._run(batch))
            # Se apunta después: una llamada fallida no la cobra Cloudflare.
            self.budget.spend(kind, cost)
        return vectors

    def _run(self, batch: list[str]) -> list[list[float]]:
        request = urllib.request.Request(self.url, data=json.dumps({"text": batch}).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self._token}",
                                                  "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as e:
            codes = _cf_codes(e.read(4096))
            e.close()
            if e.code == 429:
                if DAILY_ALLOCATION_USED in codes:
                    raise EngineError("Workers AI: se ha gastado la capa gratis de hoy") from None
                raise EngineError("Workers AI dice que hay demasiadas peticiones (429)") from None
            if e.code in (401, 403):
                raise EngineRefused(f"Workers AI no acepta el token (error {e.code}, códigos {sorted(codes)})") from None
            raise EngineError(f"Workers AI ha respondido con error {e.code} (códigos {sorted(codes)})") from None
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise EngineError(f"No puedo conectar con Workers AI: {type(reason).__name__}") from None
        except ValueError:
            raise EngineError("Workers AI ha devuelto algo que no se entiende") from None
        return self._vectors(answer, len(batch))

    @staticmethod
    def _vectors(answer: object, expected: int) -> list[list[float]]:
        """Comprueba la forma ANTES de devolver nada: un vector de otra longitud o con un NaN guardado en el índice
        estropearía todas las búsquedas por significado sin que se viera."""
        try:
            data = answer["result"]["data"]
        except (KeyError, TypeError):
            raise EngineError("Workers AI no ha devuelto los embeddings") from None
        if not isinstance(data, list) or len(data) != expected:
            raise EngineError("Workers AI ha devuelto un número de embeddings distinto del pedido")
        out = []
        for vector in data:
            if not isinstance(vector, list) or len(vector) != DIM:
                raise EngineError(f"Un embedding no tiene {DIM} números")
            try:
                values = [float(x) for x in vector]
            except (TypeError, ValueError):
                raise EngineError("Un embedding trae algo que no es un número") from None
            if any(isinstance(x, bool) for x in vector) or not all(math.isfinite(x) and abs(x) <= _MAX_ABS
                                                                    for x in values):
                raise EngineError("Un embedding trae números fuera de rango")
            out.append(values)
        return out
