"""Buzón de control local: así el atajo de teclado le dice a ARTHUR "escucha / para".

Es un socket Unix (un "fichero" especial para comunicar programas del mismo equipo):
  $XDG_RUNTIME_DIR/arthur/control.sock

Seguridad:
  - La carpeta tiene permisos 700 y el socket 600: solo tu usuario puede usarlo.
  - Además, al conectarse alguien, el kernel nos dice su usuario (SO_PEERCRED) y
    rechazamos a cualquiera que no seas tú.
  - Solo entiende órdenes de una lista fija. Una de ellas, "ask", lleva texto: es
    como escribirle a ARTHUR en la terminal (y lo que necesite confirmación se
    confirma en el Orb). Solo puede usarla tu propio usuario.
"""

import logging
import os
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

MAX_REQUEST = 8192


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "arthur" / "control.sock"


class ControlUnavailable(Exception):
    pass


class ControlServer:
    def __init__(self, handlers: dict[str, Callable[[str], str]], path: Path | None = None) -> None:
        """`handlers`: orden → función que recibe el texto que va detrás de la orden (o "")."""
        self.handlers = handlers
        self.path = path or default_socket_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            if _is_alive(self.path):
                raise ControlUnavailable("Ya hay otro ARTHUR en marcha usando el atajo de teclado")
            self.path.unlink()  # restos de un ARTHUR que se cerró de golpe
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # el socket nace ya con permisos 600
        try:
            self._sock.bind(str(self.path))
        finally:
            os.umask(old_umask)
        self._sock.listen(4)
        self._thread = threading.Thread(target=self._serve, name="control", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("Buzón de control en %s", self.path)

    def stop(self) -> None:
        self._sock.close()
        self.path.unlink(missing_ok=True)

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # el buzón se ha cerrado
            # Cada conexión en su hilo: una pregunta larga ("ask") no bloquea el atajo.
            threading.Thread(target=self._serve_one, args=(conn,), daemon=True).start()

    def _serve_one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                answer = self._handle(conn)
                conn.settimeout(None)
                conn.sendall(answer.encode() + b"\n")
            except OSError:
                pass

    def _handle(self, conn: socket.socket) -> str:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        if uid != os.getuid():
            log.warning("Conexión rechazada al buzón de control (usuario %s)", uid)
            return "error: no autorizado"
        data = b""
        try:
            while len(data) <= MAX_REQUEST and (chunk := conn.recv(4096)):
                data += chunk
        except OSError:
            pass  # el cliente no cerró su lado: usamos lo recibido
        if len(data) > MAX_REQUEST:
            return "error: petición demasiado larga"
        command, _, argument = data.decode("utf-8", errors="replace").strip().partition(" ")
        handler = self.handlers.get(command)
        if handler is None:
            return "error: orden desconocida"
        try:
            return handler(argument.strip())
        except Exception as e:
            log.exception("Fallo en la orden de control %r", command)
            return f"error: {e}"


def _is_alive(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(path))
        return True
    except OSError:
        return False


def send_command(command: str, path: Path | None = None, timeout: float = 3) -> str:
    """Cliente: envía una orden a ARTHUR y devuelve su respuesta."""
    path = path or default_socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(command.encode("utf-8"))
            s.shutdown(socket.SHUT_WR)  # "ya he terminado de hablar"
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
            return b"".join(chunks).decode(errors="replace").strip()
    except OSError as e:
        raise ControlUnavailable("ARTHUR no está en marcha") from e
