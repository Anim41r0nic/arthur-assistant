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

En Windows (aparato Windows, docs/windows.md) no hay sockets Unix ni SO_PEERCRED. Allí el
buzón es una conexión a 127.0.0.1 (nunca a la red) en un puerto libre cualquiera, con una
LLAVE aleatoria nueva en cada arranque. Puerto y llave se apuntan en
%LOCALAPPDATA%\\arthur\\control.key, una carpeta que Windows solo deja leer a tu usuario: quien
no pueda leer ese fichero no sabe la llave, y sin llave no se atiende ninguna orden.
Qué buzón se usa lo dice la ruta: «control.key» → el de la llave; cualquier otra → socket Unix.
"""

import hmac
import json
import logging
import os
import secrets
import socket
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

MAX_REQUEST = 8192
KEYED_SUFFIX = ".key"


def default_socket_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "arthur" / f"control{KEYED_SUFFIX}"
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "arthur" / "control.sock"


def make_control_server(handlers: dict[str, Callable[[str], str]], path: Path | None = None):
    """El buzón que toca según la ruta: con llave (Windows) o socket Unix (Linux)."""
    path = path or default_socket_path()
    if path.suffix == KEYED_SUFFIX:
        return KeyedControlServer(handlers, path)
    return ControlServer(handlers, path)


def _dispatch(handlers: dict[str, Callable[[str], str]], data: bytes) -> str:
    """Común a los dos buzones: la orden (de la lista fija) y su texto."""
    if len(data) > MAX_REQUEST:
        return "error: petición demasiado larga"
    command, _, argument = data.decode("utf-8", errors="replace").strip().partition(" ")
    handler = handlers.get(command)
    if handler is None:
        return "error: orden desconocida"
    try:
        return handler(argument.strip())
    except Exception as e:
        log.exception("Fallo en la orden de control %r", command)
        return f"error: {e}"


def _receive(conn: socket.socket) -> bytes:
    data = b""
    try:
        while len(data) <= MAX_REQUEST and (chunk := conn.recv(4096)):
            data += chunk
    except OSError:
        pass  # el cliente no cerró su lado: usamos lo recibido
    return data


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
        return _dispatch(self.handlers, _receive(conn))


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
    if path.suffix == KEYED_SUFFIX:
        return send_keyed_command(command, path, timeout)
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


# --- el buzón con llave (Windows) ---------------------------------------------------------------------------------

class KeyedControlServer:
    """Buzón en 127.0.0.1 con llave. Cada petición empieza por la llave en su propia línea; la orden va detrás."""

    def __init__(self, handlers: dict[str, Callable[[str], str]], path: Path) -> None:
        self.handlers = handlers
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() and _keyed_alive(self.path):
            raise ControlUnavailable("Ya hay otro ARTHUR en marcha usando el atajo de teclado")
        self._key = secrets.token_urlsafe(32)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))  # solo este equipo; el puerto lo elige el sistema (uno libre)
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        _write_private(self.path, json.dumps({"port": self.port, "key": self._key}))
        self._thread = threading.Thread(target=self._serve, name="control", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("Buzón de control en 127.0.0.1:%s (llave en %s)", self.port, self.path)

    def stop(self) -> None:
        self._sock.close()
        try:
            if json.loads(self.path.read_text(encoding="utf-8")).get("key") == self._key:
                self.path.unlink()  # solo el NUESTRO: si otro ARTHUR lo ha reescrito, es suyo
        except (OSError, ValueError, AttributeError):
            pass

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_one, args=(conn,), daemon=True).start()

    def _serve_one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                answer = self._handle(_receive(conn))
                conn.settimeout(None)
                conn.sendall(answer.encode() + b"\n")
            except OSError:
                pass

    def _handle(self, data: bytes) -> str:
        key, _, rest = data.partition(b"\n")
        # compare_digest: cuánto tarda no dice cuántas letras de la llave se han acertado.
        if not hmac.compare_digest(key.strip(), self._key.encode()):
            log.warning("Conexión rechazada al buzón de control (sin la llave)")
            return "error: no autorizado"
        return _dispatch(self.handlers, rest)


def _write_private(path: Path, text: str) -> None:
    """Escribe el fichero de la llave de golpe (nadie lo ve a medias) y, donde existe, con permisos 600."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_keyed(path: Path) -> tuple[int, str]:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, key = info["port"], info["key"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ControlUnavailable("ARTHUR no está en marcha") from e
    if not isinstance(port, int) or not 0 < port < 65536 or not isinstance(key, str) or not key.isascii():
        raise ControlUnavailable("El fichero del buzón no es válido")
    return port, key


def send_keyed_command(command: str, path: Path, timeout: float = 3) -> str:
    port, key = _read_keyed(path)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(key.encode() + b"\n" + command.encode("utf-8"))
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
            return b"".join(chunks).decode(errors="replace").strip()
    except OSError as e:
        raise ControlUnavailable("ARTHUR no está en marcha") from e


def _keyed_alive(path: Path) -> bool:
    """¿Sigue vivo el ARTHUR que escribió este fichero? Solo si responde «ok» con SU llave: un puerto que ahora
    use otro programa (tras un cierre de golpe) no cuenta."""
    try:
        return send_keyed_command("status", path, timeout=1).startswith("ok ")
    except ControlUnavailable:
        return False
