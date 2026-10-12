"""El idioma de la tubería entre el proceso de la cámara y ARTHUR (docs/gestos.md).

Una línea JSON por mensaje, y solo hay DOS formas válidas:

    {"gesture": "palma", "score": 0.93}     un gesto (o "gesture": null si en ese fotograma no hay ninguno)
    {"status": "no-camera"}                 cómo está el proceso (de una lista cerrada)

Todo lo demás se IGNORA: claves de más o de menos, nombres de gesto fuera de la lista, seguridad fuera de 0-1,
`NaN`, claves repetidas, texto que no es ASCII y cualquier línea de más de `MAX_LINE_BYTES`. Así, aunque la
librería de terceros que ve la cámara tuviese un fallo o una puerta trasera, por aquí no puede llegar una imagen
ni los puntos de la mano: no caben en 128 bytes ni tienen clave donde ir.

La lista blanca de gestos son las POSTURAS con acción en `actions.py` (decisiones de diseño, 0.37.0): palma,
pulgares, puño, índice arriba, círculo (👌), dos dedos, y el saludo (movimiento, 0.38.0). NO viajan el barrido `mover_lateral` (0.38.1: con la cámara de
en una prueba real, a ~5 fotogramas/s, una mano que cruza deprisa sale borrosa y el detector no la ve), la `pinza` (nivel 3) ni `pulgar_menique` (🤙:
con la mano de una persona real la cámara pierde la mano o lo lee como un 👍 de lado, medido en 0.37.2). El proceso de la cámara
los manda como `null` y, si llegaran, ARTHUR los ignoraría igual que cualquier otra línea rara.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .classify import Gesture, GestureReading

ALLOWED_GESTURES = tuple(g.value for g in (
    Gesture.PALM, Gesture.THUMB_UP, Gesture.THUMB_DOWN, Gesture.FIST, Gesture.INDEX_UP, Gesture.OK,
    Gesture.TWO_FINGERS, Gesture.WAVE,
))
STATUSES = (
    "ready",        # la cámara está abierta y el detector cargado
    "no-camera",    # no hay cámara o no se pudo abrir
    "no-detector",  # falta el detector de manos (pasos 0 y 1 de docs/gestos.md, aún sin hacer)
    "error",        # cualquier otro fallo (sin detalles: el texto de un error podría llevar cualquier cosa)
)
# Una línea válida ocupa menos de 50 bytes; con 128 sobra margen y no cabe ni un fotograma diminuto.
MAX_LINE_BYTES = 128


@dataclass(frozen=True)
class GestureLine:
    gesture: str | None  # uno de ALLOWED_GESTURES, o None: ningún gesto en este fotograma
    score: float


@dataclass(frozen=True)
class StatusLine:
    status: str  # uno de STATUSES


Message = GestureLine | StatusLine


# --- lado del proceso de la cámara -------------------------------------------------------------------------------

def encode_reading(reading: GestureReading | None) -> str:
    """El fotograma reconocido, como línea para la tubería. Lo que no está en la lista blanca sale como `null`."""
    if reading is None or reading.gesture.value not in ALLOWED_GESTURES:
        return json.dumps({"gesture": None, "score": 0.0})
    return json.dumps({"gesture": reading.gesture.value, "score": round(reading.score, 3)})


def encode_status(status: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"Estado desconocido: {status!r}")
    return json.dumps({"status": status})


# --- lado de ARTHUR ----------------------------------------------------------------------------------------------

class _Rejected(Exception):
    pass


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    # Con `json` normal, {"gesture":"palma","gesture":"x"} se queda con la última: mejor no adivinar.
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _Rejected
    return dict(pairs)


def _no_constants(name: str) -> float:
    raise _Rejected  # NaN, Infinity, -Infinity: no son JSON de verdad


def decode(raw: bytes) -> Message | None:
    """Una línea de la tubería (sin el salto de línea) → mensaje, o None si no está en la lista blanca."""
    if len(raw) > MAX_LINE_BYTES or not raw.isascii():
        return None
    try:
        data = json.loads(raw.decode("ascii"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except (ValueError, _Rejected, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    if data.keys() == {"status"}:
        status = data["status"]
        return StatusLine(status) if isinstance(status, str) and status in STATUSES else None
    if data.keys() == {"gesture", "score"}:
        gesture, score = data["gesture"], data["score"]
        # bool es un int en Python: `true` como seguridad sería un error, no un 1
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            return None
        if not 0.0 <= score <= 1.0:
            return None
        if gesture is not None and not (isinstance(gesture, str) and gesture in ALLOWED_GESTURES):
            return None
        return GestureLine(gesture, float(score))
    return None
