"""Gestos de MOVIMIENTO a partir de la trayectoria de la mano: una FUNCIÓN PURA (nivel 2).

Desde la 0.38.1 el punto que se sigue es la punta del dedo corazón, no la muñeca (ver `track_point`); el texto de
abajo dice «muñeca» por historia, pero vale para cualquier punto de la mano.

`classify_motion(trayectoria)` recibe los últimos instantes de la muñeca (instante, posición, tamaño de la palma)
y dice si es un `saludo` (la muñeca va y viene de lado a lado varias veces) o un `mover_lateral` (un solo barrido
en una dirección). No tiene reloj ni estado: quien la llama (el futuro `GestureWatcher`) decide qué ventana de
tiempo le pasa, por ejemplo los últimos 2 s, y le quita los fotogramas sin mano.

Todo se mide en PALMAS (muñeca → nudillo del corazón, la mediana de la trayectoria), no en píxeles: así da igual
lo lejos que estés de la cámara. Y solo cuenta el movimiento de lado a lado: un movimiento que es sobre todo
vertical (rascarse la cabeza, coger el móvil de la mesa) no es ninguno de los dos.

La idea para no confundir temblores con gestos: la trayectoria se parte en TRAMOS de ida o de vuelta, y un cambio
de sentido solo cuenta si la muñeca ha retrocedido al menos `SWING_PALMS` desde el punto más lejano del tramo
(histéresis). Un pulso que tiembla un poco no llega nunca a ese retroceso.

Como en `classify.py`, cada condición da un margen de 0 a 1 y la seguridad del gesto es el margen más flojo.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from .landmarks import MIDDLE_MCP, MIDDLE_TIP, WRIST, HandLandmarks
from .names import Gesture


def _number(value: object, what: str) -> float:
    # bool es un int en Python: `True` como instante sería un error de quien llama, no un 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} no válido en la trayectoria: {value!r}")
    return float(value)


@dataclass(frozen=True)
class TrackPoint:
    """Dónde estaba la muñeca en un instante. Sale de una mano con `track_point`."""

    timestamp: float            # segundos, de un reloj monótono
    x: float
    y: float                    # coordenadas de imagen: la Y crece hacia ABAJO
    palm: float                 # tamaño de la palma en ese fotograma, en la misma escala que x e y
    confidence: float = 1.0     # seguridad del detector de manos en ese fotograma (0-1)

    def __post_init__(self) -> None:
        # la clase es frozen: se normaliza saltándose el bloqueo UNA vez, al crearla
        for name in ("timestamp", "x", "y", "palm", "confidence"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if self.palm < 0.0:
            raise ValueError("El tamaño de la palma no puede ser negativo")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"La seguridad de la mano va de 0 a 1, no {self.confidence}")


def track_point(timestamp: float, hand: HandLandmarks) -> TrackPoint:
    """El punto de la trayectoria de una mano: la PUNTA DEL CORAZÓN y el tamaño de su palma. Nada más se guarda.

    No la muñeca (0.38.1): medido en una prueba real, al saludar se gira la mano SOBRE la muñeca, que casi no se mueve
    (0,2-0,5 palmas de lado a lado, y tanto arriba/abajo como de lado). La punta del dedo es la que va y viene.
    """
    x, y = hand.points[MIDDLE_TIP]
    return TrackPoint(timestamp, x, y, hand.distance(WRIST, MIDDLE_MCP), hand.confidence)


@dataclass(frozen=True)
class MotionReading:
    gesture: Gesture
    score: float     # 0-1
    # Solo en `mover_lateral`: +1 si la muñeca fue hacia la X creciente de la imagen, -1 si hacia la decreciente.
    # Es el sentido EN LA IMAGEN: si la cámara la da en espejo, «derecha» para el usuario es al revés, y eso lo decide
    # quien lo consuma (que sabe cómo está la cámara), no esta función. En `saludo` vale 0.
    direction: int = 0


# --- umbrales: (umbral, saturación), como en classify.py ------------------------------------------------------
# ⚠ TODOS SON ESTIMACIONES SIN PROBAR, hechas con trayectorias inventadas, sin cámara ni manos reales. Cada usuario debe
# medirlos con su mano y su cámara (docs/gestos.md). Distancias en palmas; tiempos en segundos.

SWING_PALMS = 0.35              # lo mínimo que tiene que retroceder la muñeca para contar un cambio de sentido
MAX_GAP_S = 0.70                # más de esto sin mano entre dos puntos: la trayectoria está rota, no se juzga
                                # (0.38.1: la cámara de una persona real, a ~5 fotogramas/s, pierde la mano 0,6-0,7 s al moverse)
MIN_POINTS = 4
MIN_PALM_SIZE = 1e-3

# Saludo: varios tramos de ida y vuelta, de tamaño razonable y casi horizontales.
WAVE_LEGS = (2.0, 3.0)          # nº de tramos: 3 (ida-vuelta-ida) ya es claro. 0.38.2: con la cámara de una persona real se
                                # pierde la mano en la mitad de los fotogramas al saludar y casi nunca se ven 4
WAVE_AMPLITUDE = (0.35, 0.70)   # mediana del largo de los tramos, en palmas
WAVE_MAX_AMPLITUDE = (4.0, 3.0) # tramos enormes: es el brazo entero moviéndose (estirarse, coger algo), no un saludo
WAVE_DURATION = (0.30, 0.50)    # el saludo entero dura al menos esto…
WAVE_MAX_DURATION = (4.0, 3.0)  # …y no más de esto (más lento ya no es saludar)

# Barrido: un solo tramo, largo, recto, horizontal y rápido.
SWIPE_DISTANCE = (1.20, 2.00)   # desplazamiento neto de lado, en palmas
SWIPE_SPEED = (1.50, 3.00)      # palmas por segundo: una mano que se desliza despacio al apoyarse no es un barrido
SWIPE_STRAIGHTNESS = (0.75, 0.90)  # desplazamiento neto / camino recorrido (1 = línea recta)

# Barrido: cuánto sube o baja comparado con lo que va de lado, de extremo a extremo (0 = horizontal del todo).
VERTICAL_RATIO = (0.70, 0.40)
# Saludo: lo mismo pero por CAMINO RECORRIDO (suma de lo que sube/baja en cada paso / suma de lo que va de lado).
# 0.38.2: medido en una prueba real, de extremo a extremo salía 1,0-1,5 porque la ventana incluye subir el brazo para empezar
# a saludar (un solo movimiento vertical); paso a paso, el vaivén de lado pesa lo que de verdad es.
# 0.38.3: en saludos reales (3 tramos, todo lo demás a 1,00) da 0,96-1,13: a ~5 fotogramas/s la punta del
# dedo girando hace un arco amplio. Subir la mano al encuadre da 2,5-3,0 y no tiene tres tramos de lado.
WAVE_VERTICAL_PATH = (1.60, 1.20)


def _margin(value: float, threshold: float, saturation: float) -> float:
    return min(1.0, max(0.0, (value - threshold) / (saturation - threshold)))


def _legs(xs: Sequence[float], swing: float) -> list[float]:
    """Parte el recorrido horizontal en tramos de ida y vuelta (con signo), con histéresis de `swing`.

    Un tramo empieza a contar cuando la muñeca se ha alejado `swing` de su punto más bajo/alto, y termina cuando
    retrocede `swing` desde su punto más lejano. Un temblor más pequeño que `swing` no crea tramos.
    """
    legs: list[float] = []
    low = high = xs[0]
    direction = 0
    pivot = extreme = xs[0]
    for x in xs[1:]:
        if direction == 0:
            low, high = min(low, x), max(high, x)
            if x - low >= swing:
                direction, pivot, extreme = 1, low, x
            elif high - x >= swing:
                direction, pivot, extreme = -1, high, x
        elif (x - extreme) * direction > 0:
            extreme = x  # sigue en el mismo sentido: el tramo se alarga
        elif (extreme - x) * direction >= swing:
            legs.append(extreme - pivot)  # ha retrocedido de verdad: el tramo termina en su punto más lejano
            direction, pivot, extreme = -direction, extreme, x
    if direction != 0:
        legs.append(extreme - pivot)
    return legs


def motion_scores(track: Sequence[TrackPoint]) -> tuple[dict[Gesture, float], int]:
    """La seguridad (0-1) de cada gesto de movimiento, sin contar la del detector, y el sentido del barrido.

    Lanza `ValueError` si los instantes no van en orden: eso es un fallo de quien llama, no un gesto dudoso.
    """
    nothing = {Gesture.WAVE: 0.0, Gesture.SWIPE: 0.0}
    for before, after in zip(track, track[1:]):
        if after.timestamp <= before.timestamp:
            raise ValueError("Los instantes de la trayectoria tienen que ir en orden y sin repetirse")
    if len(track) < MIN_POINTS:
        return nothing, 0
    if any(after.timestamp - before.timestamp > MAX_GAP_S for before, after in zip(track, track[1:])):
        return nothing, 0
    palm = statistics.median(p.palm for p in track)
    if palm < MIN_PALM_SIZE:
        return nothing, 0

    xs = [p.x / palm for p in track]
    ys = [p.y / palm for p in track]
    duration = track[-1].timestamp - track[0].timestamp
    width = max(xs) - min(xs)
    if width <= 0.0:
        return nothing, 0
    horizontal = _margin((max(ys) - min(ys)) / width, *VERTICAL_RATIO)
    legs = _legs(xs, SWING_PALMS)

    wave = 0.0
    if len(legs) >= 3:
        amplitude = statistics.median(abs(leg) for leg in legs)
        side_path = sum(abs(x1 - x0) for x0, x1 in zip(xs, xs[1:]))
        up_down_path = sum(abs(y1 - y0) for y0, y1 in zip(ys, ys[1:]))
        sideways = _margin(up_down_path / side_path, *WAVE_VERTICAL_PATH) if side_path > 0.0 else 0.0
        wave = min(_margin(len(legs), *WAVE_LEGS), _margin(amplitude, *WAVE_AMPLITUDE),
                   _margin(max(abs(leg) for leg in legs), *WAVE_MAX_AMPLITUDE),
                   _margin(duration, *WAVE_DURATION), _margin(duration, *WAVE_MAX_DURATION), sideways)

    swipe, direction = 0.0, 0
    if len(legs) == 1:  # exactamente un tramo: con ida y vuelta ya no es un barrido (y con dos, tampoco un saludo)
        net = xs[-1] - xs[0]
        path = sum(math.hypot(x1 - x0, y1 - y0) for x0, y0, x1, y1 in zip(xs, ys, xs[1:], ys[1:]))
        swipe = min(_margin(abs(net), *SWIPE_DISTANCE), _margin(abs(net) / duration, *SWIPE_SPEED),
                    _margin(abs(net) / path, *SWIPE_STRAIGHTNESS) if path > 0.0 else 0.0, horizontal)
        direction = 1 if net > 0 else -1

    return {Gesture.WAVE: wave, Gesture.SWIPE: swipe}, direction


def classify_motion(track: Sequence[TrackPoint]) -> MotionReading | None:
    """El gesto de movimiento de la trayectoria (`saludo` o `mover_lateral`), o None si no es ninguno."""
    track = tuple(track)
    scores, direction = motion_scores(track)
    best, score = max(scores.items(), key=lambda item: item[1])
    # La seguridad del detector cuenta como en las posturas: la media de la de cada fotograma.
    score *= statistics.fmean(p.confidence for p in track) if track else 0.0
    if score <= 0.0:
        return None
    return MotionReading(best, score, direction if best is Gesture.SWIPE else 0)
