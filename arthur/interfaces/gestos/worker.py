"""GestureWorker: lanza el proceso de la cámara dentro de bwrap y lee sus líneas (docs/gestos.md, paso 3).

Como el proceso de voz (voice/piper_worker.py), la parte que toca la cámara vive en OTRO proceso y ARTHUR solo
habla con él por tuberías. Aquí, además, ese proceso va en una burbuja de bubblewrap:

  - sin red (`--unshare-net`): aunque la librería de la cámara quisiera mandar algo fuera, no tiene por dónde;
  - muere con ARTHUR (`--die-with-parent`): si el servicio se para o se cae, la cámara se apaga con él;
  - todo el sistema en solo lectura, tu carpeta personal vacía (solo se ven el código de ARTHUR y la carpeta de
    los modelos, también en solo lectura), sin variables de entorno y, de dispositivos, solo la cámara.

De lo que manda, solo pasan las líneas de la lista blanca de `protocol.py`; el resto se cuenta y se tira sin
mirarlo (ni siquiera va al registro: podría ser cualquier cosa).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .protocol import MAX_LINE_BYTES, Message, decode

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # .../arthur (el paquete; el proceso lo necesita para importar)
CHILD = Path(__file__).with_name("camera_worker.py")
CAMERA_DEVICE = "/dev/video0"
SYSTEM_PYTHON = "/usr/bin/python3"  # el del sistema: python-opencv vendría de pacman (opción B), no del .venv
STOP_TIMEOUT = 3


class WorkerUnavailable(Exception):
    """No se puede lanzar el proceso de la cámara (p. ej. falta bwrap). Nunca se lanza sin burbuja."""


def models_dir() -> Path:
    """Sitio FIJO de los modelos de manos (docs/gestos.md: nada de rutas libres en la configuración)."""
    from ...core.config import data_dir

    return data_dir() / "gestos"


def sandbox_argv(fps: int, *, bwrap: str | None = None, home: Path | None = None,
                 models: Path | None = None) -> list[str]:
    """La orden completa: bwrap con la burbuja y, dentro, el proceso de la cámara."""
    bwrap = bwrap or shutil.which("bwrap")
    if bwrap is None:
        raise WorkerUnavailable("bubblewrap (bwrap) no está instalado: sin burbuja no se enciende la cámara")
    home = (home or Path.home()).resolve()
    models = models or models_dir()
    return [
        bwrap,
        "--ro-bind", "/", "/",
        "--dev", "/dev",                               # un /dev nuevo y mínimo (null, zero, random…)…
        "--dev-bind-try", CAMERA_DEVICE, CAMERA_DEVICE,  # …más la cámara, y ningún otro dispositivo
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",                             # sin D-Bus, Wayland, PipeWire ni Hyprland
        "--tmpfs", str(home),                          # tu carpeta personal, vacía…
        "--ro-bind", str(PACKAGE_DIR), str(PACKAGE_DIR),  # …salvo el código de ARTHUR (solo lectura)
        "--ro-bind-try", str(models), str(models),     # …y los modelos de manos (solo lectura)
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--chdir", "/tmp",
        "--",
        SYSTEM_PYTHON, "-I", "-B", str(CHILD), "--fps", str(int(fps)), "--models", str(models),
    ]


class GestureWorker:
    def __init__(
        self,
        on_message: Callable[[Message], None],
        *,
        on_exit: Callable[[int | None], None] | None = None,
        fps: int = 8,
        command: list[str] | None = None,
    ) -> None:
        """`command` solo se cambia en los tests: un proceso FALSO que escribe líneas, sin cámara."""
        self.on_message = on_message
        self.on_exit = on_exit or (lambda code: None)  # el proceso terminó solo (no porque se le parase)
        self.fps = fps
        self.command = command
        self.ignored = 0  # líneas tiradas por no estar en la lista blanca (solo se cuenta)
        self._proc: subprocess.Popen | None = None
        self._stopping = threading.Event()  # uno nuevo por proceso: un lector viejo nunca confunde al nuevo
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            argv = self.command or sandbox_argv(self.fps)  # sin bwrap, WorkerUnavailable: no se lanza nada
            self._stopping = threading.Event()
            self.ignored = 0
            # `bwrap --die-with-parent` mata la burbuja cuando muere el HILO que la lanzó, no el proceso. Quien
            # llama a start() suele ser un hilo de vida corta (la petición del botón del Orb): la cámara moría a
            # los 7 ms con -9. Por eso lanza el proceso el hilo lector, que vive exactamente lo que vive el proceso.
            launched: list = []
            ready = threading.Event()
            threading.Thread(target=self._launch_and_read, args=(argv, self._stopping, launched, ready),
                             name="gestures-worker", daemon=True).start()
            ready.wait()
            if isinstance(launched[0], BaseException):
                raise launched[0]
            self._proc = launched[0]
        log.info("gestures: worker started")

    def _launch_and_read(self, argv: list[str], stopping: threading.Event, launched: list,
                         ready: threading.Event) -> None:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,     # al cerrarla, el proceso de la cámara sabe que tiene que terminar
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # sus errores podrían llevar cualquier cosa: no se leen
                env={},
            )
        except BaseException as e:  # OSError y compañía: start() la relanza en el hilo de quien llamó
            launched.append(e)
            ready.set()
            return
        launched.append(proc)
        ready.set()
        self._read_loop(proc, stopping)

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._stopping.set()
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log.info("gestures: worker stopped (%d líneas ignoradas)", self.ignored)

    def _read_loop(self, proc: subprocess.Popen, stopping: threading.Event) -> None:
        stdout = proc.stdout
        try:
            while line := stdout.readline(MAX_LINE_BYTES + 1):
                if not line.endswith(b"\n") and len(line) > MAX_LINE_BYTES:
                    self.ignored += 1
                    _skip_rest_of_line(stdout)  # una línea enorme (¿una imagen?): se tira entera sin guardarla
                    continue
                message = decode(line.rstrip(b"\r\n"))
                if message is None:
                    self.ignored += 1
                    continue
                try:
                    self.on_message(message)
                except Exception:
                    log.exception("Fallo procesando un mensaje del proceso de la cámara")
        except (ValueError, OSError):
            pass  # stop() ha cerrado la tubería
        finally:
            stdout.close()
        code = proc.wait()
        if not stopping.is_set():
            log.info("gestures: worker exited (código %s)", code)
            self.on_exit(code)


def _skip_rest_of_line(stdout) -> None:
    while chunk := stdout.readline(4096):
        if chunk.endswith(b"\n"):
            return
