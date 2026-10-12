"""De los 21 puntos de una mano a la POSTURA: una FUNCIÓN PURA (docs/gestos.md, paso 2).

Sin cámara, sin reloj, sin estado: el mismo dato da siempre el mismo resultado, así que se prueba entera con listas
de números. «Mantener el gesto 1 s» y la pausa entre gestos (anti-rebote) NO se hacen aquí: son del futuro
`GestureWatcher`, que necesita reloj. Los gestos de MOVIMIENTO (saludo, barrido) están en `motion.py`.

Va en dos pasos:
1. `features(mano)`: unas pocas MEDIDAS de la mano (¿cuánto está estirado cada dedo?, ¿dónde queda la punta del
   pulgar respecto a la palma?, ¿hacia dónde apunta?). Solo distancias y ángulos entre puntos, siempre divididos
   por el tamaño de la palma: da igual lo grande que salga la mano, dónde esté en la imagen, y si es la izquierda,
   la derecha o una imagen en espejo. Sirven también para que cada usuario calibre (docs/gestos.md).
2. `posture_scores(medidas)`: cada postura es una lista de condiciones sobre esas medidas. `classify` se queda
   con la mejor.

Cómo decide: cada condición ("este dedo está estirado", "el pulgar apunta hacia arriba"…) da un MARGEN de 0 a 1:
0 si no se cumple (o apenas), 1 si se cumple de sobra. Un gesto solo existe si TODAS sus condiciones tienen margen
mayor que 0, y su seguridad es la del margen más flojo, multiplicada por la seguridad del detector de manos. Así
una postura dudosa («casi» un pulgar arriba) no se descarta a ciegas: sale con seguridad baja, y quien consume el
resultado decide con `min_score` (0,8 en docs/gestos.md). Para los gestos que confirman cosas, pedir seguridad
alta es la protección contra un pulgar arriba que no era para ARTHUR.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .landmarks import (
    INDEX_MCP,
    INDEX_TIP,
    LONG_FINGERS,
    MIDDLE_MCP,
    PINKY_MCP,
    THUMB_IP,
    THUMB_MCP,
    THUMB_TIP,
    WRIST,
    HandLandmarks,
)
from .names import Gesture


@dataclass(frozen=True)
class GestureReading:
    gesture: Gesture
    score: float  # 0-1: lo clara que es la postura, por la seguridad del detector de manos


# --- umbrales: (umbral, saturación) ---------------------------------------------------------------------------
# El margen vale 0 en el umbral y 1 en la saturación (la saturación puede ser menor que el umbral: "menos es
# mejor"). ⚠ TODOS SON ESTIMACIONES SIN PROBAR, sacadas de proporciones de una mano típica y de manos dibujadas
# con números, SIN cámara ni manos reales. Cada usuario debe medirlos con su mano y ajustarlos (docs/gestos.md).
# Todos son relativos al tamaño de la palma (muñeca → nudillo del corazón), nunca a píxeles.

# Dedo largo: distancia muñeca→punta / distancia muñeca→articulación PIP. Estirado ≈ 1,35; puño ≈ 0,7.
EXTENDED_RATIO = (1.08, 1.20)  # medido (prueba real, 0.35.2-3): dedos estirados de verdad 1,20-1,43 (el índice, el más corto)
# El índice estirado de una persona real es más corto que los otros (palma: 1,01-1,31; señalar: 1,05-1,23; 0.37.1): umbral propio.
INDEX_EXTENDED_RATIO = (1.00, 1.12)
CURLED_RATIO = (1.00, 0.88)    # medido (prueba real, 0.35.3): en su puño, 0,48-0,93; la mano en el teclado da ~1,02
# Entre 1,00 y 1,15 (medio doblado) ni está estirado ni cerrado: no cuenta para ningún gesto. Es lo que tiene una
# mano relajada, la que escribe en el teclado o la que sostiene el móvil: por eso casi nunca dan un gesto.

# Índice de la pinza: doblado hacia el pulgar, pero sin llegar a cerrarse (un puño NO es una pinza).
INDEX_NOT_CURLED = (1.00, 1.10)
# Índice de los pulgares: en un puño con el pulgar fuera, el índice rodea la base del pulgar y no se cierra del todo
# (medido en una prueba real, 0.35.3: 0,95-1,18, cuando los otros tres dan 0,5-0,9). Vale cerrado o sin estirar, pero en este
# segundo caso con su punta LEJOS de la del pulgar (prueba real: 0,91-2,96 palmas): si están cerca es una pinza, no un pulgar.
INDEX_WRAPPED = (1.20, 1.05)
TIPS_FAR = (0.32, 0.45)  # 0.37.1: en el 👍 de una persona real las puntas quedan a 0,48-0,51 (la pinza se toca: < 0,25)

# Pulgar «recto y largo» (palma, pulgares, pulgar_menique): lo contrario de uno doblado o metido en el puño.
THUMB_STRAIGHTNESS = (0.75, 0.90)  # distancia base→punta / largo real de sus dos falanges (prueba real: 0,90-1,00)
THUMB_LENGTH = (0.30, 0.45)        # distancia base→punta / palma. Medido (prueba real, 0.35.2): palma abierta 0,52-0,56,
                                   # pulgar abajo 0,56-1,0; con (0,40; 0,65) la palma real se quedaba en 0,56

# Pulgar «recogido» (puño, índice arriba, dos dedos): la punta queda DELANTE de la palma o de los dedos, no a un
# lado ni más allá de los nudillos. Se mide en el sistema de la propia mano (a lo largo: de la muñeca hacia los
# nudillos; de lado: hacia el lado del índice), en palmas.
THUMB_ACROSS_FOLDED = (0.35, 0.25)
THUMB_ALONG_FOLDED = (1.10, 1.00)
# Pulgar «hacia fuera, de lado» (pulgar_menique): la punta bien separada del lado del índice. Sin esto, un pulgar
# cruzado delante del puño (que también es bastante recto y largo) contaba a medias como fuera.
THUMB_ACROSS_SIDEWAYS = (0.45, 0.65)
# Pulgar ARRIBA de verdad: su punta queda por encima de los nudillos, a lo largo de la mano (prueba real, 0.37.1: 👍 1,04-1,08;
# su puño 0,78-0,89). Sin esto, un puño con el pulgar algo suelto daba «pulgar arriba» 0,5, empatado con «puño»:
# y uno es «Sí» y el otro «No».
THUMB_ABOVE_KNUCKLES = (0.95, 1.05)
# Pulgar CORTO (☝️): al señalar, una persona real deja el pulgar al lado, no delante (de lado 0,77-0,88), pero corto (0,21-0,37
# palmas). En una «L» el pulgar está estirado y largo (≥ 0,5): esa no es «índice arriba».
THUMB_SHORT = (0.42, 0.36)

# Puntas del pulgar y del índice tocándose (ok, pinza): su distancia, en palmas. Y separadas (palma abierta).
TIPS_TOUCHING = (0.25, 0.17)  # 0.37.1: el 👌 de una persona real deja las puntas a 0,17 (con (0,22; 0,12) se quedaba en 0,5)
TIPS_APART = (0.25, 0.40)

# Ángulo (grados) con la vertical: del pulgar (pulgares), del índice (índice arriba) y del eje de la mano
# (palma, puño, ok, pinza, dos dedos). Hasta ~30° de inclinación cuenta como del todo recto (nadie sostiene la mano
# a plomo); a 40° o más, ya no. Pedir la mano LEVANTADA es lo que evita leer como gesto una mano apoyada en la
# mesa o en el teclado.
THUMB_VERTICAL_DEG = (45.0, 35.0)  # medido: un pulgar abajo real queda a 25-35° de la vertical
INDEX_VERTICAL_DEG = (40.0, 30.0)
PALM_RAISED_DEG = (40.0, 30.0)

MIN_PALM_SIZE = 1e-3  # palma más pequeña que esto (en la escala que sea): puntos amontonados, no una mano


def _margin(value: float, threshold: float, saturation: float) -> float:
    """0 en el umbral o antes de él, 1 en la saturación o más allá; lineal entre medias."""
    return min(1.0, max(0.0, (value - threshold) / (saturation - threshold)))


def _angle_from(vector: tuple[float, float], up: bool) -> float:
    """Grados entre el vector y la vertical hacia arriba (`up`) o hacia abajo. En imagen, arriba es -Y."""
    dx, dy = vector
    return math.degrees(math.atan2(abs(dx), -dy if up else dy))


@dataclass(frozen=True)
class HandFeatures:
    """Las medidas de una mano con las que se decide la postura. Ninguna es una coordenada: no dicen DÓNDE está
    la mano ni dibujan su forma, así que se pueden escribir en el registro para calibrar."""

    fingers: tuple[float, float, float, float]  # muñeca→punta / muñeca→PIP: índice, corazón, anular, meñique
    thumb_straightness: float   # base→punta del pulgar / largo de sus falanges (1 = recto del todo)
    thumb_length: float         # base→punta del pulgar, en palmas
    thumb_along: float          # punta del pulgar a lo largo de la mano (muñeca = 0, nudillo del corazón = 1)
    thumb_across: float         # punta del pulgar de lado, en palmas (positivo: hacia el lado del índice)
    tips_gap: float             # punta del pulgar ↔ punta del índice, en palmas
    hand_tilt: float            # grados entre el eje de la mano (muñeca → nudillos) y «arriba»
    index_tilt: float           # grados entre el índice (nudillo → punta) y «arriba»
    thumb_up_tilt: float        # grados entre el pulgar (base → punta) y «arriba»
    thumb_down_tilt: float      # … y «abajo»


def features(hand: HandLandmarks) -> HandFeatures | None:
    """Las medidas de la mano, o None si los puntos no forman una mano (todos amontonados, dedos de largo 0)."""
    palm = hand.distance(WRIST, MIDDLE_MCP)
    if palm < MIN_PALM_SIZE:
        return None

    ratios = []
    for pip, tip in LONG_FINGERS:
        reach = hand.distance(WRIST, pip)
        if reach <= 0.0:
            return None
        ratios.append(hand.distance(WRIST, tip) / reach)

    phalanges = hand.distance(THUMB_MCP, THUMB_IP) + hand.distance(THUMB_IP, THUMB_TIP)
    if phalanges <= 0.0:
        return None
    thumb_reach = hand.distance(THUMB_MCP, THUMB_TIP)

    # Sistema de la mano: «a lo largo» = de la muñeca al nudillo del corazón; «de lado» = su perpendicular,
    # girada para que el nudillo del índice quede en el lado positivo. Así una mano izquierda y una derecha (o
    # la misma en espejo) dan los mismos números.
    ux, uy = hand.vector(WRIST, MIDDLE_MCP)
    ux, uy = ux / palm, uy / palm
    nx, ny = -uy, ux
    sx, sy = hand.vector(PINKY_MCP, INDEX_MCP)
    side = 1.0 if sx * nx + sy * ny >= 0.0 else -1.0
    tx, ty = hand.vector(WRIST, THUMB_TIP)

    thumb_vector = hand.vector(THUMB_MCP, THUMB_TIP)
    return HandFeatures(
        fingers=(ratios[0], ratios[1], ratios[2], ratios[3]),
        thumb_straightness=thumb_reach / phalanges,
        thumb_length=thumb_reach / palm,
        thumb_along=(tx * ux + ty * uy) / palm,
        thumb_across=side * (tx * nx + ty * ny) / palm,
        tips_gap=hand.distance(THUMB_TIP, INDEX_TIP) / palm,
        hand_tilt=_angle_from(hand.vector(WRIST, MIDDLE_MCP), up=True),
        index_tilt=_angle_from(hand.vector(INDEX_MCP, INDEX_TIP), up=True),
        thumb_up_tilt=_angle_from(thumb_vector, up=True),
        thumb_down_tilt=_angle_from(thumb_vector, up=False),
    )


def posture_scores(f: HandFeatures) -> dict[Gesture, float]:
    """La seguridad (0-1) de CADA postura con esas medidas, sin contar la del detector de manos.

    Se separa de `classify` para poder ver en los tests (y al calibrar) no solo cuál gana, sino por cuánto.
    """
    out = [_margin(r, *EXTENDED_RATIO) for r in f.fingers]   # índice, corazón, anular, meñique
    out[0] = _margin(f.fingers[0], *INDEX_EXTENDED_RATIO)
    curled = [_margin(r, *CURLED_RATIO) for r in f.fingers]
    index, middle, ring, pinky = range(4)

    thumb_out = min(_margin(f.thumb_straightness, *THUMB_STRAIGHTNESS), _margin(f.thumb_length, *THUMB_LENGTH))
    thumb_folded = min(_margin(f.thumb_across, *THUMB_ACROSS_FOLDED), _margin(f.thumb_along, *THUMB_ALONG_FOLDED))
    touching = _margin(f.tips_gap, *TIPS_TOUCHING)
    raised = _margin(f.hand_tilt, *PALM_RAISED_DEG)
    # Puño de los pulgares: corazón, anular y meñique cerrados, el índice sin estirar (rodea el pulgar) y el pulgar
    # recto, largo y FUERA del puño (no cruzado delante: con el umbral de largo medido, uno cruzado también llega).
    index_wrapped = max(curled[index], min(_margin(f.fingers[index], *INDEX_WRAPPED), _margin(f.tips_gap, *TIPS_FAR)))
    thumb_fist = min(index_wrapped, curled[middle], curled[ring], curled[pinky], thumb_out, 1.0 - thumb_folded)

    return {
        # Palma abierta: todo estirado, pulgar sin tocar el índice, y la mano levantada.
        Gesture.PALM: min(*out, thumb_out, _margin(f.tips_gap, *TIPS_APART), raised),
        # Pulgares: puño con los cuatro dedos cerrados y el pulgar recto, largo y casi vertical. Si hay cualquier
        # otro dedo fuera («L», señalar), no es un «sí».
        Gesture.THUMB_UP: min(thumb_fist, _margin(f.thumb_up_tilt, *THUMB_VERTICAL_DEG),
                              _margin(f.thumb_along, *THUMB_ABOVE_KNUCKLES)),
        Gesture.THUMB_DOWN: min(thumb_fist, _margin(f.thumb_down_tilt, *THUMB_VERTICAL_DEG)),
        # Puño: los cuatro cerrados y el pulgar recogido delante (no hacia arriba: eso es un pulgar arriba).
        Gesture.FIST: min(*curled, thumb_folded, raised),
        # Índice arriba: solo el índice estirado, y apuntando hacia arriba (no señalando a un lado).
        # El pulgar, recogido delante o corto a un lado (como lo hace una persona real); y sin tocar el índice (eso es pinza).
        Gesture.INDEX_UP: min(out[index], curled[middle], curled[ring], curled[pinky],
                              max(thumb_folded, _margin(f.thumb_length, *THUMB_SHORT)), 1.0 - touching,
                              _margin(f.index_tilt, *INDEX_VERTICAL_DEG)),
        # OK: pulgar e índice formando un círculo y los otros tres estirados.
        Gesture.OK: min(touching, out[middle], out[ring], out[pinky], raised),
        # Pinza: pulgar e índice juntos y los otros tres cerrados. El índice tiene que estar doblado, no cerrado:
        # en un puño la punta del pulgar también queda cerca de la del índice.
        Gesture.PINCH: min(touching, _margin(f.fingers[index], *INDEX_NOT_CURLED),
                           curled[middle], curled[ring], curled[pinky], raised),
        # Dos dedos (la «V»): índice y corazón estirados, anular y meñique cerrados, pulgar recogido.
        Gesture.TWO_FINGERS: min(out[index], out[middle], curled[ring], curled[pinky], thumb_folded, raised),
        # Pulgar y meñique (el «teléfono»): no pide orientación, porque se suele hacer de lado; la forma ya es
        # muy rara por casualidad.
        Gesture.THUMB_PINKY: min(thumb_out, _margin(f.thumb_across, *THUMB_ACROSS_SIDEWAYS), out[pinky],
                                 curled[index], curled[middle], curled[ring]),
    }


def classify(hand: HandLandmarks) -> GestureReading | None:
    """La postura que hace la mano, o None si no es ninguna de la lista (o es demasiado dudosa)."""
    f = features(hand)
    if f is None:
        return None
    best, score = max(posture_scores(f).items(), key=lambda item: item[1])
    score *= hand.confidence
    return GestureReading(best, score) if score > 0.0 else None
