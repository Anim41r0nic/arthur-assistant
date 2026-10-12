"""La fuente de fotogramas REAL: la cámara → el detector de manos → `Frame` (puntos, nunca imágenes).

Implementa `FrameSource` (source.py): se abre y se cierra SIEMPRE con `with`; cerrar la fuente apaga la cámara.
La imagen vive solo dentro de `read()`, en una variable local: no se guarda, no se devuelve y no se escribe en
ningún sitio. Lo que sale es `Frame(timestamp, mano o None)`.

Gasto (medido en una prueba real, 2026-10-08): capturar a 640x480 cuesta 4,4 % de un núcleo a 3 fps y 11,4 % a 8 fps, y
el detector de manos ~56 ms de CPU por fotograma con las dos pasadas. Para no pasar del 10 % en reposo:
- Ritmo: 3 fps sin mano; en cuanto ve una mano sube a 6 fps, y vuelve al reposo cuando lleva unos segundos sin verla.
- Puerta de movimiento: sin mano a la vista, el detector SOLO se ejecuta si la imagen ha cambiado (una mano que
  entra en el encuadre la cambia); una habitación quieta no gasta nada más que capturar. Los fotogramas que se
  saltan salen como «sin mano».

Necesita numpy y cv2: lo usa solo el proceso auxiliar de la cámara, no el núcleo de ARTHUR.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .detector import HandDetector
from ..gestos.source import Frame, FrameSource, FrameSourceError

import numpy as np

IDLE_FPS = 3
ACTIVE_FPS = 6
ACTIVE_FOR = 3.0  # segundos que sigue en ritmo alto tras ver una mano por última vez
MOTION_SIZE = (64, 48)   # la imagen se reduce a esto (en gris) para compararla con la anterior: casi no cuesta
MOTION_PIXEL = 12        # un punto «cambió» si varía más de esto (de 255)
MOTION_FRACTION = 0.01   # ... y hay movimiento si cambió al menos el 1 % de los puntos


class CameraFrameSource(FrameSource):
    def __init__(self, detector: HandDetector, *, device: int = 0, width: int = 640, height: int = 480,
                 capture_factory: Callable[[], object] | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        """`capture_factory`, `clock` y `sleep` solo se cambian en los tests."""
        self.detector = detector
        self._device, self._size = device, (width, height)
        self._factory = capture_factory or self._real_capture
        self._clock, self._sleep = clock, sleep
        self._capture = None
        self._last_read = 0.0
        self._last_hand = -ACTIVE_FOR
        self._previous: np.ndarray | None = None
        self.analyzed = self.skipped = 0  # cuántos fotogramas pasaron por el detector y cuántos se saltó la puerta

    def _real_capture(self):
        import cv2

        capture = cv2.VideoCapture(self._device, cv2.CAP_V4L2)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._size[0])
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._size[1])
        return capture

    def open(self) -> None:
        if self._capture is not None:
            raise FrameSourceError("La cámara ya está abierta")
        capture = self._factory()
        if not capture.isOpened():
            capture.release()
            raise FrameSourceError("No he podido abrir la cámara")
        self._capture = capture

    def read(self) -> Frame | None:
        if self._capture is None:
            raise FrameSourceError("La cámara no está abierta")
        fps = ACTIVE_FPS if self._clock() - self._last_hand < ACTIVE_FOR else IDLE_FPS
        wait = self._last_read + 1 / fps - self._clock()
        if wait > 0:
            self._sleep(wait)
        ok, image = self._capture.read()
        self._last_read = self._clock()
        if not ok:
            return None  # la cámara se ha desconectado o la han cerrado: no hay más fotogramas
        moving = self._moved(image)
        if not moving and self._last_read - self._last_hand >= ACTIVE_FOR:
            self.skipped += 1  # habitación quieta y sin mano a la vista: nada que detectar
            return Frame(self._last_read, None)
        self.analyzed += 1
        hand = self.detector.detect(image)
        if hand is not None:
            self._last_hand = self._last_read
        return Frame(self._last_read, hand)

    def _moved(self, image) -> bool:
        """¿Ha cambiado la imagen respecto a la anterior? (se compara en pequeño y en gris)"""
        import cv2

        small = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), MOTION_SIZE, interpolation=cv2.INTER_AREA)
        previous, self._previous = self._previous, small
        if previous is None:
            return True  # el primer fotograma no se puede comparar: se mira
        changed = np.count_nonzero(cv2.absdiff(small, previous) > MOTION_PIXEL)
        return changed >= MOTION_FRACTION * small.size

    def close(self) -> None:
        self._previous = None
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()
