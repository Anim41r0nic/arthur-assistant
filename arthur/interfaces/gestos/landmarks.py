"""Los 21 puntos de una mano.

Es el ÚNICO dato que entra al reconocedor de gestos: ni imágenes ni nada de la cámara. El orden es el de
MediaPipe (muñeca, y por cada dedo sus cuatro articulaciones de la base a la punta), que es también el de los
modelos ONNX de manos que menciona docs/gestos.md, así que el detector real que se elija más adelante encaja sin
tocar nada de aquí.

Las coordenadas son (x, y) en cualquier escala (0-1 normalizadas, píxeles…) con la Y creciendo HACIA ABAJO, como
en una imagen. Un tercer valor (profundidad) se acepta y se descarta: es el más ruidoso y no hace falta.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

POINT_COUNT = 21

WRIST = 0
THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20

# (articulación PIP, punta) de los cuatro dedos largos: lo que mira el reconocedor para saber si están estirados.
LONG_FINGERS = ((INDEX_PIP, INDEX_TIP), (MIDDLE_PIP, MIDDLE_TIP), (RING_PIP, RING_TIP), (PINKY_PIP, PINKY_TIP))


def _coordinate(value: object) -> float:
    # bool es un int en Python: `True` como coordenada sería un error de quien llama, no un 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Coordenada no válida en la mano: {value!r}")
    return float(value)


@dataclass(frozen=True)
class HandLandmarks:
    """Una mano detectada: 21 puntos y la seguridad (0-1) con que el detector dice que es una mano."""

    # `repr=False`: un `log.debug(mano)` despistado no puede dejar la forma de la mano en el registro
    # (docs/gestos.md: nunca sale un punto de la mano, ni a la tubería ni al log).
    points: tuple[tuple[float, float], ...] = field(repr=False)
    confidence: float = 1.0

    def __post_init__(self) -> None:
        raw = tuple(self.points)
        if len(raw) != POINT_COUNT:
            raise ValueError(f"Una mano tiene {POINT_COUNT} puntos, no {len(raw)}")
        clean = []
        for point in raw:
            if not isinstance(point, Sequence) or isinstance(point, str) or len(point) < 2:
                raise ValueError("Cada punto de la mano necesita al menos (x, y)")
            clean.append((_coordinate(point[0]), _coordinate(point[1])))
        confidence = _coordinate(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"La seguridad de la mano va de 0 a 1, no {confidence}")
        # la clase es frozen: se normaliza saltándose el bloqueo UNA vez, al crearla
        object.__setattr__(self, "points", tuple(clean))
        object.__setattr__(self, "confidence", confidence)

    def distance(self, a: int, b: int) -> float:
        (ax, ay), (bx, by) = self.points[a], self.points[b]
        return math.hypot(ax - bx, ay - by)

    def vector(self, start: int, end: int) -> tuple[float, float]:
        (sx, sy), (ex, ey) = self.points[start], self.points[end]
        return ex - sx, ey - sy
