"""Presupuesto diario de Neurons de Workers AI para el cerebro.

La capa gratis de Workers AI da 10 000 Neurons al día PARA TODA LA CUENTA, compartidos con el chat de la web y las
imágenes de Frutinovelas. Si el cerebro se lo comiera todo indexando, el chat se quedaría sin Workers AI. Por eso el
cerebro lleva su propia cuenta con un tope (3 000 por defecto, ajustable) y un orden de prioridad:

    preguntas de Marc  → pueden usar hasta el 100 % del tope
    embeddings         → hasta el 90 % (si no caben, esperan a mañana; mientras, el trozo se encuentra por palabras)
    generar preguntas  → hasta el 80 % (lo menos urgente: sin presupuesto, esa noche no se generan)

Así, aunque la indexación y la generación nocturna vayan a tope, siempre queda un 10-20 % para que Marc pregunte.

El «día» es el de Cloudflare: se renueva a las 00:00 UTC (las 01:00 o 02:00 en Madrid), no a medianoche local.
La cuenta se guarda en un JSON pequeño `{"day": "2026-10-17", "spent": {"ask": 12.5, ...}}` para que un reinicio
del contenedor no la ponga a cero. Es una ESTIMACIÓN (tokens contados a ojo): Cloudflare es quien manda de verdad,
y si dice 429 el motor ya cae a Ollama por su cuenta.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from arthur.webchat.workers_ai import utc_day

log = logging.getLogger(__name__)

Kind = Literal["ask", "embed", "generate"]

# Hasta qué parte del tope diario puede llegar cada uso (contando lo que ya han gastado TODOS).
SHARES: dict[str, float] = {"ask": 1.0, "embed": 0.9, "generate": 0.8}

# Neurons por millón de tokens (entrada, salida), copiados de la tabla oficial de precios el 2026-10-17:
# https://developers.cloudflare.com/workers-ai/platform/pricing/
#   @cf/baai/bge-m3                → 1 075 Neurons por M tokens de entrada (los embeddings no tienen salida)
#   @cf/google/gemma-4-26b-a4b-it  → 9 091 por M de entrada y 27 273 por M de salida
# «chat» es el modelo del chat de la web (DEFAULT_MODEL de arthur/webchat/workers_ai.py): si se cambia allí, hay que
# mirar su fila en esa página y cambiarla aquí.
COSTS: dict[str, tuple[float, float]] = {
    "bge-m3": (1075.0, 0.0),
    "chat": (9091.0, 27273.0),
}


def approx_tokens(text: str) -> int:
    """Tokens aproximados de un texto: ~3,5 caracteres por token en español (con tildes y fórmulas, algo menos
    que los ~4 del inglés). Se redondea hacia arriba para no quedarse corto con el presupuesto."""
    return math.ceil(len(text) / 3.5)


def estimate(model_key: str, tokens_in: int, tokens_out: int) -> float:
    """Neurons estimados de una llamada. `model_key` es una clave de COSTS (KeyError si no existe: mejor fallar
    que contar 0 y gastar sin control)."""
    per_in, per_out = COSTS[model_key]
    return (tokens_in * per_in + tokens_out * per_out) / 1_000_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Budget:
    """La cuenta diaria. Segura entre hilos (la web contesta preguntas mientras el reindexador hace embeddings)."""

    def __init__(self, path: Path, daily: int = 3000, now: Callable[[], datetime] = _utc_now) -> None:
        self.path = path
        self.daily = daily
        self.now = now
        self._lock = threading.Lock()

    def spend(self, kind: Kind, neurons: float) -> None:
        """Apunta lo gastado. No comprueba el tope: si la llamada ya se hizo, se gastó (se pregunta antes con
        `allows`)."""
        self._check(kind, neurons)
        with self._lock:
            day, spent = self._load()
            spent[kind] = spent.get(kind, 0) + neurons
            self._save(day, spent)

    def allows(self, kind: Kind, neurons: float) -> bool:
        """¿Cabe un gasto de `neurons` para este uso? Se compara lo gastado HOY por todos más lo nuevo con la parte
        del tope que le toca a este uso."""
        self._check(kind, neurons)
        with self._lock:
            _, spent = self._load()
        return sum(spent.values()) + neurons <= self.daily * SHARES[kind]

    def spent_today(self) -> float:
        with self._lock:
            _, spent = self._load()
        return sum(spent.values())

    @staticmethod
    def _check(kind: str, neurons: float) -> None:
        if kind not in SHARES:
            raise ValueError(f"Uso de presupuesto desconocido: {kind!r}")
        # Un NaN o un negativo estropearían la suma para el resto del día sin que nadie se diera cuenta.
        if not isinstance(neurons, (int, float)) or not math.isfinite(neurons) or neurons < 0:
            raise ValueError("Neurons debe ser un número positivo")

    def _load(self) -> tuple[str, dict[str, float]]:
        """Lo gastado hoy (día UTC). Si el fichero es de otro día, no existe o está roto, se empieza de cero:
        perder la cuenta de un día es mucho mejor que dejar el cerebro sin arrancar."""
        today = utc_day(self.now()).isoformat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("day") != today:
                return today, {}
            spent = {k: float(v) for k, v in data.get("spent", {}).items()
                     if k in SHARES and math.isfinite(float(v)) and float(v) >= 0}
            return today, spent
        except FileNotFoundError:
            return today, {}
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("Presupuesto del cerebro ilegible: se empieza de cero hoy")
            return today, {}

    def _save(self, day: str, spent: dict[str, float]) -> None:
        # Temporal + os.replace: un corte a mitad nunca deja un JSON a medias, o queda el viejo o el nuevo.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"day": day, "spent": spent}, f)
        os.replace(tmp, self.path)
