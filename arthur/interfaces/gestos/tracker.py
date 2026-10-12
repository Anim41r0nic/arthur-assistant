"""MotionTracker: guarda la trayectoria de la muñeca de los últimos segundos y dice si hay un gesto de MOVIMIENTO.

Vive DENTRO del proceso de la cámara (en la burbuja bwrap): la trayectoria son puntos de la mano y nunca salen de
ahí. Por la tubería solo viaja el nombre del gesto (`saludo` o `mover_lateral`), como con las posturas.

Dos trabajos:
  - `feed(instante, mano)` → `GestureReading` de movimiento cuando la trayectoria reciente es un saludo o un
    barrido claro (`EMIT_SCORE`). Al darlo, esa trayectoria ya no cuenta para otro gesto (un mismo saludo no sale
    dos veces), pero se sigue sabiendo que la mano se mueve.
  - `moving`: si la muñeca se está desplazando ahora mismo. Mientras se mueve, la postura NO cuenta: saludar con
    la mano abierta es una «palma» que se mueve, y sin esto saltaría «calla» en vez del saludo.

La decisión de qué es un saludo la toma `motion.classify_motion` (función pura, con sus tests); aquí solo hay la
ventana de tiempo y el estado.
"""

from __future__ import annotations

from collections import deque

from .classify import GestureReading
from .landmarks import HandLandmarks
from .motion import MAX_GAP_S, TrackPoint, classify_motion, track_point

WINDOW_S = 2.5         # segundos de trayectoria que se miran (un saludo dura 0,3-4 s; uno normal, ~1-2 s)
EMIT_SCORE = 0.8       # seguridad mínima para mandar el gesto (la misma que `min_score` por defecto)
MOVING_WINDOW_S = 0.5  # para `moving`: cuánto se ha desplazado la muñeca en el último medio segundo…
MOVING_PALMS = 0.35    # …si es más que esto (en palmas), la mano se está moviendo (igual que SWING_PALMS)


class MotionTracker:
    def __init__(self) -> None:
        self._track: deque[TrackPoint] = deque()
        self._used_until = float("-inf")  # los puntos hasta aquí ya formaron un gesto: no se usan para otro
        self._settling = False  # tras un gesto: hasta que la mano se pare (o se pierda), es el MISMO movimiento

    def feed(self, timestamp: float, hand: HandLandmarks | None) -> GestureReading | None:
        """Añade un fotograma. Devuelve el gesto de movimiento si la trayectoria reciente lo forma."""
        if hand is None:
            if self._track and timestamp - self._track[-1].timestamp > MAX_GAP_S:
                self._track.clear()  # se perdió la mano demasiado rato: la trayectoria está rota
                self._settling = False
            return None
        if self._track and timestamp <= self._track[-1].timestamp:
            self._track.clear()  # un reloj que no avanza no es una trayectoria (no debería pasar)
        self._track.append(track_point(timestamp, hand))
        while self._track and timestamp - self._track[0].timestamp > WINDOW_S:
            self._track.popleft()
        if self._settling:
            # Un saludo largo son muchos «ida-vuelta-ida» seguidos: se cuenta UNA vez, hasta que la mano se para.
            if self.moving:
                self._used_until = timestamp
                return None
            self._settling = False
        reading = classify_motion([p for p in self._track if p.timestamp > self._used_until])
        if reading is None or reading.score < EMIT_SCORE:
            return None
        self._used_until = timestamp
        self._settling = True
        return GestureReading(reading.gesture, reading.score)

    @property
    def moving(self) -> bool:
        """¿La muñeca se ha desplazado más de `MOVING_PALMS` palmas en el último `MOVING_WINDOW_S`?"""
        if len(self._track) < 2:
            return False
        last = self._track[-1]
        recent = [p for p in self._track if last.timestamp - p.timestamp <= MOVING_WINDOW_S]
        palm = max(last.palm, 1e-6)
        xs = [p.x / palm for p in recent]
        ys = [p.y / palm for p in recent]
        return max(max(xs) - min(xs), max(ys) - min(ys)) > MOVING_PALMS
