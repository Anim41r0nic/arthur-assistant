"""Gestos con la cámara: la parte que se puede escribir y probar SIN cámara (docs/gestos.md).

- `Gesture` (en `names.py`): la LISTA BLANCA de nombres de gesto, en un único sitio; `from_wire` la aplica.
- `HandLandmarks`: los 21 puntos de una mano (el único dato que entra al reconocedor).
- `classify`: puntos → postura (`palma`, `pulgar_arriba`, `pulgar_abajo`, `puno`, `indice_arriba`, `circulo` (👌), `pinza`,
  `dos_dedos`, `pulgar_menique`) o nada. `features` da las medidas en que se basa (para calibrar). Función pura.
- `classify_motion`: trayectoria de la muñeca → `saludo` o `mover_lateral` o nada. Función pura.
- `FrameSource`: la interfaz de «fuente de fotogramas»; `ListFrameSource`, una de mentira para tests.
- `protocol` / `worker` / `camera_worker`: el proceso de la cámara (en bwrap, sin red) y su tubería con lista blanca.

Todavía NO hay detector de manos (pasos 0 y 1 de docs/gestos.md): el proceso de la cámara avisa y termina sin
abrirla. Solo biblioteca estándar.
"""

from .classify import GestureReading, HandFeatures, classify, features, posture_scores
from .landmarks import HandLandmarks
from .motion import MotionReading, TrackPoint, classify_motion, motion_scores, track_point
from .names import MOTIONS, POSTURES, Gesture, from_wire
from .source import Frame, FrameSource, FrameSourceError, ListFrameSource, iter_readings

__all__ = [
    "MOTIONS", "POSTURES", "Frame", "FrameSource", "FrameSourceError", "Gesture", "GestureReading",
    "HandFeatures", "HandLandmarks", "ListFrameSource", "MotionReading", "TrackPoint", "classify",
    "classify_motion", "features", "from_wire", "iter_readings", "motion_scores", "posture_scores", "track_point",
]
