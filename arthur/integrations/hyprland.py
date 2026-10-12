"""Hyprland por su socket IPC: consultas (HyprlandReader) y acciones fijas (HyprlandController).

Hyprland escucha en $XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock.
Le mandamos una petición de texto (p. ej. "j/clients") y responde en JSON.

HyprlandReader solo hace consultas. Las acciones van en HyprlandController, que
tiene métodos FIJOS (cambiar de escritorio, enfocar, mover, cerrar) y nunca
acepta código ni texto libre.

Por qué tanto cuidado: en Hyprland 0.56 las acciones son código Lua que se
ejecuta dentro del compositor (que puede lanzar cualquier programa), y si el
selector de ventana no encuentra la ventana, Hyprland actúa sobre la ventana
ACTIVA. Por eso cada acción sobre una ventana va en un único bloque Lua que
primero la busca por su dirección exacta y, si no existe, se detiene sin hacer
nada (error "ARTHUR:no-window").
"""

import json
import os
import re
import socket
from pathlib import Path
from typing import Any

# Únicas peticiones permitidas: todas son consultas.
READ_REQUESTS = frozenset({"j/clients", "j/activewindow", "j/workspaces", "j/activeworkspace", "j/monitors"})


class HyprlandUnavailable(Exception):
    pass


def default_socket_path() -> Path | None:
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not signature or not runtime:
        return None
    return Path(runtime) / "hypr" / signature / ".socket.sock"


class HyprlandReader:
    def __init__(self, socket_path: Path | None = None, timeout: float = 2.0) -> None:
        self.socket_path = socket_path or default_socket_path()
        self.timeout = timeout

    def query(self, request: str) -> Any:
        if request not in READ_REQUESTS:
            raise ValueError(f"Petición no permitida a Hyprland: {request!r}")
        if self.socket_path is None or not self.socket_path.exists():
            raise HyprlandUnavailable("No estoy dentro de una sesión de Hyprland")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect(str(self.socket_path))
            s.sendall(request.encode())
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
        return json.loads(b"".join(chunks) or b"null")

    def windows(self) -> list[dict[str, Any]]:
        return self.query("j/clients") or []


ADDRESS = re.compile(r"^0x[0-9a-f]{1,16}\Z")  # \Z y no $: «$» también acepta un salto de línea al final
NO_WINDOW = "ARTHUR:no-window"
MAX_WORKSPACE = 10


class HyprlandActionError(Exception):
    pass


class WindowGone(HyprlandActionError):
    """La ventana ya no existe: Hyprland no ha hecho nada."""


class HyprlandController:
    """Acciones sobre Hyprland, cada una con una plantilla Lua fija y valores validados."""

    def __init__(self, socket_path: Path | None = None, timeout: float = 2.0) -> None:
        self.socket_path = socket_path or default_socket_path()
        self.timeout = timeout

    # --- plantillas ---------------------------------------------------------

    def switch_workspace(self, number: int) -> None:
        self._eval(f'hl.dispatch(hl.dsp.focus({{ workspace = "{_workspace(number)}" }}))')

    def focus_window(self, address: str) -> None:
        self._eval_on_window(address, "hl.dispatch(hl.dsp.focus({ window = w }))")

    def move_window(self, address: str, workspace: int, follow: bool = False) -> None:
        self._eval_on_window(
            address,
            f'hl.dispatch(hl.dsp.window.move({{ window = w, workspace = "{_workspace(workspace)}", '
            f'follow = {"true" if follow else "false"} }}))',
        )

    def close_window(self, address: str) -> None:
        # close = petición educada de cierre (como pulsar la X). Nunca usamos kill.
        self._eval_on_window(address, "hl.dispatch(hl.dsp.window.close({ window = w }))")

    def set_fullscreen(self, address: str, enabled: bool) -> None:
        action = "set" if enabled else "unset"
        self._eval_on_window(address, f'hl.dispatch(hl.dsp.window.fullscreen({{ window = w, mode = "fullscreen", '
                                      f'action = "{action}" }}))')

    def hide_window(self, address: str) -> None:
        """Manda la ventana a un escritorio especial oculto, sin seguirla (para el Orb)."""
        self._eval_on_window(address, 'hl.dispatch(hl.dsp.window.move({ window = w, workspace = "special:arthur", '
                                      'follow = false }))')

    # --- detalles -----------------------------------------------------------

    def _eval_on_window(self, address: str, action: str) -> None:
        if not ADDRESS.match(address):
            raise HyprlandActionError(f"Dirección de ventana no válida: {address!r}")
        # Buscar y actuar en el MISMO bloque: si la ventana ya no existe, no se toca nada
        # (y nunca se acaba actuando sobre la ventana activa por error).
        self._eval(
            f'local w = hl.get_window("address:{address}") '
            f'if w == nil then error("{NO_WINDOW}", 0) end '
            f"{action}"
        )

    def _eval(self, code: str) -> None:
        if self.socket_path is None or not self.socket_path.exists():
            raise HyprlandUnavailable("No estoy dentro de una sesión de Hyprland")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect(str(self.socket_path))
            s.sendall(f"/eval {code}".encode())
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
        answer = b"".join(chunks).decode(errors="replace").strip()
        if answer == "ok":
            return
        if NO_WINDOW in answer:
            raise WindowGone("Esa ventana ya no existe")
        raise HyprlandActionError(f"Hyprland ha respondido: {answer[:200]}")


def _workspace(number: int) -> int:
    if type(number) is not int or not 1 <= number <= MAX_WORKSPACE:
        raise HyprlandActionError(f"El escritorio debe ser un número del 1 al {MAX_WORKSPACE}")
    return number


def own_process_tree() -> set[int]:
    """PIDs de ARTHUR y de todos sus "antepasados" (p. ej. la terminal donde se ejecuta)."""
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])  # 4.º campo: el PID del padre
        except (OSError, ValueError, IndexError):
            break
    return pids
