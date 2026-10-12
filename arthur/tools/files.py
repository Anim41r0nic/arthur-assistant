"""Herramientas de ficheros, siempre a través de la política de rutas."""

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from ..security.paths import PathDenied, PathPolicy
from .base import Param, Risk, Tool, ToolDenied, ToolError

MAX_LIST_ENTRIES = 200
MAX_WRITE_CHARS = 100_000


def _current_umask() -> int:
    """La máscara de permisos del proceso (normalmente 022 → ficheros 644)."""
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("Umask:"):
            return int(line.split()[1], 8)
    return 0o022


def build_file_tools(paths: PathPolicy) -> list[Tool]:
    def read_path(raw: str) -> Path:
        try:
            return paths.check_read(raw)
        except PathDenied as e:
            raise ToolDenied(str(e)) from e

    def write_path(raw: str) -> Path:
        try:
            return paths.check_write(raw)
        except PathDenied as e:
            raise ToolDenied(str(e)) from e

    # --- list_directory ---------------------------------------------------
    def list_directory(args: dict) -> str:
        path = read_path(args["path"])
        if not path.is_dir():
            raise ToolError(f"No es una carpeta: {path}")
        # Las entradas sensibles ni siquiera se muestran.
        entries = sorted(
            (e for e in path.iterdir() if not paths.is_sensitive(e.resolve())),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries[:MAX_LIST_ENTRIES]]
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"… y {len(entries) - MAX_LIST_ENTRIES} más")
        return f"{path} ({len(entries)} elementos)\n" + "\n".join(lines) if lines else f"{path} está vacía"

    # --- read_file --------------------------------------------------------
    def read_file(args: dict) -> str:
        path = read_path(args["path"])
        if not path.is_file():
            raise ToolError(f"No es un fichero: {path}")
        max_bytes = args["max_bytes"]
        with path.open("rb") as f:
            data = f.read(max_bytes + 1)
        if b"\0" in data:
            raise ToolError("Parece un fichero binario; solo leo ficheros de texto")
        text = data[:max_bytes].decode("utf-8", errors="replace")
        if len(data) > max_bytes:
            text += f"\n… (recortado a {max_bytes} bytes)"
        return text

    # --- write_file -------------------------------------------------------
    def check_write(args: dict) -> None:
        path = write_path(args["path"])
        if not path.parent.is_dir():
            raise ToolDenied(f"La carpeta {path.parent} no existe")
        if path.exists() and not path.is_file():
            raise ToolDenied(f"{path} existe y no es un fichero")
        if path.exists() and not args["overwrite"]:
            raise ToolDenied(f"{path} ya existe; para reemplazarlo hay que indicar overwrite=true")

    def summarize_write(args: dict) -> str:
        path = Path(args["path"]).expanduser()
        action = "Sobrescribir (con copia .bak)" if path.exists() else "Crear"
        return f"{action} {path} con {len(args['content'])} caracteres"

    def write_file(args: dict) -> str:
        check_write(args)  # se repite justo antes de escribir por si algo cambió
        path = write_path(args["path"])
        backup = None
        if path.exists():
            backup = path.with_name(f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}.bak")
            shutil.copy2(path, backup)
        # Escritura atómica: primero a un temporal y luego se renombra.
        # Si algo falla a mitad, el fichero original queda intacto.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(args["content"])
            # El temporal nace con permisos 600; le damos los que tocan:
            # los del fichero original, o los normales si es nuevo.
            if backup:
                shutil.copymode(backup, tmp)
            else:
                os.chmod(tmp, 0o666 & ~_current_umask())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        note = f" (copia de seguridad: {backup.name})" if backup else ""
        return f"Escritos {len(args['content'])} caracteres en {path}{note}"

    path_param = Param("path", str, "absoluta o con ~/", max_length=4096)
    return [
        Tool(
            name="list_directory",
            description="Lista una carpeta.",
            risk=Risk.READ,
            params=(path_param,),
            check=lambda a: read_path(a["path"]) and None,
            run=list_directory,
        ),
        Tool(
            name="read_file",
            description="Lee un fichero de texto.",
            risk=Risk.READ,
            params=(
                path_param,
                Param("max_bytes", int, "", required=False, default=20_000, minimum=1, maximum=200_000),
            ),
            check=lambda a: read_path(a["path"]) and None,
            run=read_file,
        ),
        Tool(
            name="write_file",
            description="Crea o reemplaza un fichero de texto.",
            risk=Risk.WRITE,
            params=(
                path_param,
                Param("content", str, "", max_length=MAX_WRITE_CHARS),
                Param("overwrite", bool, "reemplazar si existe", required=False, default=False),
            ),
            check=check_write,
            summarize=summarize_write,
            run=write_file,
        ),
    ]
