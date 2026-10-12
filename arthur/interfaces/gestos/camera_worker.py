"""Proceso auxiliar de la cámara: corre DENTRO de bwrap (sin red) y solo escribe líneas de la lista blanca.

Lo lanza `worker.py`; nunca se ejecuta a mano ni fuera de la burbuja. Protocolo en `protocol.py`:
por la salida estándar solo salen `{"status": …}` y `{"gesture": …, "score": …}`; nunca imágenes ni puntos.
Termina cuando ARTHUR cierra su entrada estándar (o cuando ARTHUR muere: `--die-with-parent`).

El detector es el de `arthur/interfaces/camara/` (numpy + cv2 de pacman, por eso corre con el Python del sistema)
con los dos modelos de docs/gestos-modelos.md. Antes de cargarlos se comprueba su sha256: si falta un modelo, no
coincide la huella o no están numpy/cv2, `open_camera()` devuelve None, el proceso avisa con
`{"status": "no-detector"}` y termina SIN abrir la cámara.
"""

import argparse
import hashlib
import os
import sys
import threading
from pathlib import Path

# Con `python -I` no se añade la carpeta del script a sys.path: se pone a mano la que contiene el paquete arthur
# (montada en solo lectura dentro de la burbuja).
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from arthur.interfaces.gestos.protocol import encode_reading, encode_status
from arthur.interfaces.gestos.source import FrameSource, FrameSourceError, iter_readings
from arthur.interfaces.gestos.tracker import MotionTracker


# Los modelos de docs/gestos-modelos.md y su huella: un fichero cambiado no se carga (cv2 lo interpretaría).
PALM_MODEL = "palm_detection_mediapipe_2023feb.onnx"
HAND_MODEL = "handpose_estimation_mediapipe_2023feb.onnx"
MODEL_SHA256 = {
    PALM_MODEL: "78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c",
    HAND_MODEL: "db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c",
}


def verified_models(models: Path) -> tuple[Path, Path] | None:
    """Las rutas de los dos modelos si existen y su sha256 es el esperado; si no, None."""
    for name, expected in MODEL_SHA256.items():
        try:
            digest = hashlib.sha256((models / name).read_bytes()).hexdigest()
        except OSError:
            return None
        if digest != expected:
            return None
    return models / PALM_MODEL, models / HAND_MODEL


def open_camera(fps: int, models: Path) -> FrameSource | None:
    """La fuente real (cámara → detector → puntos de la mano), o None si no hay detector utilizable.

    No abre la cámara: eso lo hace `iter_readings` con `with`. `fps` no se usa: la fuente lleva su propio ritmo
    (3 fps en reposo, 6 con una mano a la vista; ver camara/camera.py).
    """
    paths = verified_models(models)
    if paths is None:
        return None
    try:
        from arthur.interfaces.camara.camera import CameraFrameSource
        from arthur.interfaces.camara.detector import HandDetector

        return CameraFrameSource(HandDetector(*paths))
    except Exception:  # sin numpy/cv2 o un modelo que cv2 no carga: lo mismo que no tener detector
        return None


def run(source: FrameSource | None, out) -> int:
    """Escribe una línea por fotograma. Devuelve el código de salida del proceso."""
    def send(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    if source is None:
        send(encode_status("no-detector"))
        return 3
    try:
        ready = False
        for _, reading in iter_readings(source, MotionTracker()):  # cierra la cámara pase lo que pase
            if not ready:
                send(encode_status("ready"))
                ready = True
            send(encode_reading(reading))
    except FrameSourceError:
        send(encode_status("no-camera"))
        return 2
    except BrokenPipeError:
        return 0  # ARTHUR ya no escucha
    except Exception:
        send(encode_status("error"))  # sin el texto del error: podría llevar cualquier cosa
        return 1
    return 0


def _exit_when_stdin_closes() -> None:
    sys.stdin.buffer.read()  # bloquea hasta que ARTHUR cierra la tubería
    os._exit(0)  # sin esperar a nada: la cámara se apaga al morir el proceso


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--models", type=Path, required=True)
    args = parser.parse_args(argv)
    source = open_camera(max(1, min(args.fps, 15)), args.models)
    if source is not None:
        threading.Thread(target=_exit_when_stdin_closes, daemon=True).start()
    return run(source, sys.stdout)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # no esperar al hilo que vigila la entrada (Python se quejaría al cerrar con él leyendo)
