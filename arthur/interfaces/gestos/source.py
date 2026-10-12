"""La «fuente de fotogramas»: de dónde salen las manos que se reconocen.

Hoy solo hay una fuente de mentira (`ListFrameSource`, para tests y simulaciones). La cámara real llegará en
el proceso auxiliar de docs/gestos.md (pasos 0, 1 y 3), con la persona delante, y será otra implementación de
`FrameSource`: cámara → detector de manos → `Frame`. Lo que cruza esta interfaz YA son puntos de la mano, no
imágenes: así lo que consume los fotogramas no puede guardar ni enseñar una foto aunque quiera.

Contrato de seguridad: una fuente se abre y se cierra SIEMPRE con `with` (o con `iter_readings`). Con una cámara,
dejarla abierta es dejarla encendida.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .classify import GestureReading, classify
from .landmarks import HandLandmarks


class FrameSourceError(Exception):
    """La fuente no se pudo abrir, se desconectó o se usó mal (leer sin abrir)."""


@dataclass(frozen=True)
class Frame:
    timestamp: float                # segundos, de un reloj monótono: sirve para medir cuánto dura un gesto
    hand: HandLandmarks | None      # None: en este fotograma no hay ninguna mano


class FrameSource(ABC):
    """Fuente de fotogramas. Implementa `open`, `read` y `close`; úsala con `with`."""

    @abstractmethod
    def open(self) -> None:
        """Prepara la fuente (con una cámara: la enciende). Lanza `FrameSourceError` si no puede."""

    @abstractmethod
    def read(self) -> Frame | None:
        """El siguiente fotograma, o None cuando ya no hay más. Bloquea hasta que lo haya."""

    @abstractmethod
    def close(self) -> None:
        """Libera la fuente (con una cámara: la apaga). Se puede llamar varias veces y sin haberla abierto."""

    def __enter__(self) -> FrameSource:
        try:
            self.open()
        except BaseException:
            self.close()  # un `open()` a medias no puede dejar la cámara encendida
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class ListFrameSource(FrameSource):
    """Fuente de mentira: devuelve, en orden, los fotogramas que le des. Para tests; no toca ningún dispositivo."""

    def __init__(self, frames: Iterable[Frame]) -> None:
        self._frames = list(frames)
        self._position = 0
        self.is_open = False

    def open(self) -> None:
        if self.is_open:
            raise FrameSourceError("La fuente ya está abierta")
        self._position = 0
        self.is_open = True

    def read(self) -> Frame | None:
        if not self.is_open:
            raise FrameSourceError("La fuente no está abierta")
        if self._position >= len(self._frames):
            return None
        frame = self._frames[self._position]
        self._position += 1
        return frame

    def close(self) -> None:
        self.is_open = False


def iter_readings(source: FrameSource, tracker=None) -> Iterator[tuple[float, GestureReading | None]]:
    """Abre la fuente y va dando (instante, gesto o None) por cada fotograma, hasta que se acaba.

    Con `tracker` (un `MotionTracker`), también los gestos de MOVIMIENTO: si la trayectoria de la muñeca forma un
    saludo o un barrido, ese fotograma da ese gesto; y mientras la mano se desplaza, la postura no cuenta (None).

    La fuente se cierra al terminar, si falla la lectura y si quien consume deja de pedir (basta con cerrar el
    generador: `contextlib.closing(iter_readings(f))`).
    """
    with source:
        while (frame := source.read()) is not None:
            if tracker is not None:
                motion = tracker.feed(frame.timestamp, frame.hand)
                if motion is not None:
                    yield frame.timestamp, motion
                    continue
                if tracker.moving:
                    yield frame.timestamp, None
                    continue
            yield frame.timestamp, (classify(frame.hand) if frame.hand is not None else None)
