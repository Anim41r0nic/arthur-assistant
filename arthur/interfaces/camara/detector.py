"""El detector de manos REAL: imagen de la cámara → los 21 puntos de la mano (`HandLandmarks`).

Dos modelos ONNX de MediaPipe (ver docs/gestos-modelos.md) ejecutados con `cv2.dnn` (python-opencv de los
repositorios oficiales): primero uno busca la palma en la imagen entera; luego el otro, sobre un recorte de la mano
girado para que quede vertical, saca los 21 puntos. Escrito aquí a partir de cómo funcionan los modelos (la
referencia de OpenCV zoo, Apache-2.0, trae una tabla de 2.016 «anclas» a mano; aquí se generan y un test las
compara con esa tabla).

Este paquete (`arthur.interfaces.camara`) necesita numpy y cv2 y vive APARTE de `arthur.interfaces.gestos` a
propósito: ese otro paquete es solo biblioteca estándar (un test lo vigila). Solo lo usará el proceso auxiliar. Lo que entra es una imagen (en memoria) y lo que sale son puntos: la imagen no se guarda ni
se devuelve en ningún sitio.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from ..gestos.landmarks import POINT_COUNT, HandLandmarks

PALM_SIZE = 192            # el detector de palmas recibe 192x192
HAND_SIZE = 224            # el de puntos, 224x224
PALM_MIN_SCORE = 0.5       # seguridad mínima de «aquí hay una palma»
HAND_MIN_CONFIDENCE = 0.5  # seguridad mínima de «esto es una mano» (la devuelve el modelo de puntos)
ROI_SCALE = 2.6            # el recorte de la mano es 2,6 veces el lado mayor de la caja de la palma
ROI_SHIFT_Y = -0.5         # y se desplaza media caja hacia los dedos: la palma está en la parte baja de la mano
WRIST_POINT, MIDDLE_BASE_POINT = 0, 2  # de los 7 puntos que da el detector de palmas


def make_anchors() -> np.ndarray:
    """Los 2.016 puntos de referencia (x, y normalizados) sobre los que el detector de palmas da sus ajustes:
    una cuadrícula de 24x24 con 2 anclas por celda y otra de 12x12 con 6 (3 capas de 2 que caen en el mismo sitio)."""
    anchors = []
    for size, per_cell in ((24, 2), (12, 6)):
        for y in range(size):
            for x in range(size):
                anchors += [((x + 0.5) / size, (y + 0.5) / size)] * per_cell
    return np.array(anchors, dtype=np.float64)


ANCHORS = make_anchors()


class Palm:
    """Una palma encontrada: su caja y 7 puntos, en píxeles de la imagen original."""

    def __init__(self, center: tuple[float, float], size: tuple[float, float], points: np.ndarray, score: float):
        self.center, self.size, self.points, self.score = center, size, points, score


def decode_palm(regression: np.ndarray, scores: np.ndarray, image_size: tuple[int, int]) -> Palm | None:
    """Del resultado en bruto del detector (1x2016x18 y 1x2016x1) a la MEJOR palma, o None si no hay ninguna.

    La imagen se metió en un cuadrado de lado max(ancho, alto) con bandas negras a los lados; las coordenadas del
    modelo van de 0 a 1 en ese cuadrado, así que se multiplican por ese lado y se quita la banda.
    """
    width, height = image_size
    side = max(width, height)
    pad = np.array([(side - width) / 2, (side - height) / 2])
    raw = regression.reshape(-1, 18).astype(np.float64)
    logits = scores.reshape(-1).astype(np.float64)
    best = int(np.argmax(logits))
    score = 1.0 / (1.0 + math.exp(-float(logits[best])))
    if score < PALM_MIN_SCORE:
        return None
    anchor = ANCHORS[best]
    center = (raw[best, 0:2] / PALM_SIZE + anchor) * side - pad
    size = raw[best, 2:4] / PALM_SIZE * side
    points = (raw[best, 4:18].reshape(7, 2) / PALM_SIZE + anchor) * side - pad
    return Palm((float(center[0]), float(center[1])), (float(abs(size[0])), float(abs(size[1]))), points, score)


def roi_matrix(cx: float, cy: float, side: float, rotation: float) -> np.ndarray:
    """Matriz 2x3 del recorte: lleva un píxel (u, v) del recorte de 224x224 a la imagen original.

    El recorte es un cuadrado de lado `side` centrado en (cx, cy) y girado `rotation` radianes (0 = derecho)."""
    cos, sin = math.cos(rotation), math.sin(rotation)
    scale = side / HAND_SIZE
    # píxel (u, v) del recorte → (u - 112, v - 112) * scale → girado `rotation` → + centro
    return np.array([[cos * scale, -sin * scale, cx - (cos - sin) * scale * HAND_SIZE / 2],
                     [sin * scale, cos * scale, cy - (sin + cos) * scale * HAND_SIZE / 2]], dtype=np.float64)


def hand_rotation(wrist: np.ndarray, middle_base: np.ndarray) -> float:
    """Cuánto hay que girar para que la línea muñeca → base del corazón quede vertical con los dedos arriba."""
    return math.atan2(middle_base[0] - wrist[0], -(middle_base[1] - wrist[1]))


def hand_roi_matrix(palm: Palm) -> np.ndarray:
    """El recorte de la mano a partir de la PALMA detectada: girado hasta dejar la mano vertical, centrado un
    poco hacia los dedos y con 2,6 veces el tamaño de la palma."""
    rotation = hand_rotation(palm.points[WRIST_POINT], palm.points[MIDDLE_BASE_POINT])
    box_w, box_h = palm.size
    cx = palm.center[0] + (-ROI_SHIFT_Y * box_h) * math.sin(rotation)
    cy = palm.center[1] + (ROI_SHIFT_Y * box_h) * math.cos(rotation)
    return roi_matrix(cx, cy, max(box_w, box_h) * ROI_SCALE, rotation)


TRACK_SCALE = 2.0    # el recorte de seguimiento es 2 veces la caja de la mano ya encontrada
TRACK_SHIFT_Y = -0.1  # y se desplaza un poco hacia los dedos


def tracking_roi_matrix(points: np.ndarray) -> np.ndarray:
    """El recorte para el SIGUIENTE fotograma, a partir de los 21 puntos del anterior (así no hace falta volver
    a buscar la palma): misma orientación, centrado en la caja de la mano medida en su propio eje."""
    rotation = hand_rotation(points[0], points[9])  # muñeca → base del corazón (puntos 0 y 9 de la mano)
    cos, sin = math.cos(rotation), math.sin(rotation)
    # puntos en el eje de la mano (deshaciendo el giro): ahí la caja es la «natural» de la mano
    local = points @ np.array([[cos, sin], [-sin, cos]]).T
    low, high = local.min(axis=0), local.max(axis=0)
    size = high - low
    cx_l, cy_l = (low + high) / 2 + np.array([0.0, TRACK_SHIFT_Y * size[1]])
    cx = cx_l * cos - cy_l * sin
    cy = cx_l * sin + cy_l * cos
    return roi_matrix(cx, cy, float(max(size)) * TRACK_SCALE, rotation)


def map_points(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Aplica la matriz 2x3 a una lista de puntos (N, 2)."""
    return points @ matrix[:, :2].T + matrix[:, 2]


class HandDetector:
    """imagen BGR de la cámara → `HandLandmarks` de la mano mejor vista, o None si no hay ninguna.

    Recuerda la mano del fotograma anterior: mientras se siga viendo, recorta por ahí y se ahorra buscar la palma
    (la pasada más cara); si la pierde, vuelve a buscarla en la imagen entera."""

    def __init__(self, palm_model: Path, hand_model: Path, *, threads: int = 1) -> None:
        # Por defecto OpenCV usa un hilo por núcleo (12) que dan vueltas esperando: una pasada de 10 ms de reloj
        # gastaba 110 ms de CPU. Con un hilo, 35 ms (palma) y 21 ms (mano) de CPU de verdad.
        cv2.setNumThreads(threads)
        # readNet lee un fichero ONNX local: no abre red ni ejecuta código
        self._palm = cv2.dnn.readNet(str(palm_model))
        self._hand = cv2.dnn.readNet(str(hand_model))
        self._tracked: np.ndarray | None = None  # puntos de la mano del fotograma anterior (en píxeles)

    def detect(self, image: np.ndarray) -> HandLandmarks | None:
        height, width = image.shape[:2]
        if self._tracked is not None:
            found = self._landmarks(image, tracking_roi_matrix(self._tracked))
            if found is not None:
                self._tracked = found[0]
                return found[1]
            self._tracked = None  # la perdió: busca la palma de nuevo en este mismo fotograma
        palm = self._find_palm(image, (width, height))
        if palm is None:
            return None
        found = self._landmarks(image, hand_roi_matrix(palm))
        if found is None:
            return None
        self._tracked = found[0]
        return found[1]

    def forget(self) -> None:
        """Olvida la mano anterior (p. ej. al reanudar tras una pausa en la que la imagen ha cambiado mucho)."""
        self._tracked = None

    def _landmarks(self, image: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, HandLandmarks] | None:
        crop = cv2.warpAffine(image, matrix, (HAND_SIZE, HAND_SIZE), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        blob = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis] / 255.0  # NHWC
        self._hand.setInput(blob)
        landmarks, confidence, *_ = self._hand.forward(self._hand.getUnconnectedOutLayersNames())
        confidence = float(confidence.reshape(-1)[0])
        if not math.isfinite(confidence) or confidence < HAND_MIN_CONFIDENCE:
            return None
        points = map_points(matrix, landmarks.reshape(-1, 3)[:, :2].astype(np.float64))
        if points.shape[0] != POINT_COUNT or not np.isfinite(points).all():
            return None
        return points, HandLandmarks(tuple((float(x), float(y)) for x, y in points), min(1.0, confidence))
    def _find_palm(self, image: np.ndarray, image_size: tuple[int, int]) -> Palm | None:
        width, height = image_size
        side = max(width, height)
        square = cv2.copyMakeBorder(image, (side - height) // 2, side - height - (side - height) // 2,
                                    (side - width) // 2, side - width - (side - width) // 2,
                                    cv2.BORDER_CONSTANT, value=(0, 0, 0))
        small = cv2.resize(square, (PALM_SIZE, PALM_SIZE), interpolation=cv2.INTER_AREA)
        blob = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis] / 255.0  # NHWC
        self._palm.setInput(blob)
        outputs = self._palm.forward(self._palm.getUnconnectedOutLayersNames())
        # se distinguen por su forma, no por el orden: 18 valores por ancla = ajustes; 1 = seguridad
        regression = next(o for o in outputs if o.shape[-1] == 18)
        scores = next(o for o in outputs if o.shape[-1] == 1)
        return decode_palm(regression, scores, image_size)
