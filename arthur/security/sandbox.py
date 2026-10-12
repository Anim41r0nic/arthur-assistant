"""Ejecución aislada con bubblewrap (bwrap).

bwrap crea una "burbuja" para el proceso usando funciones del kernel
(namespaces), sin necesidad de root. Dentro de la burbuja:
  - todo el sistema se ve en SOLO LECTURA;
  - tu carpeta personal está vacía, salvo las carpetas de lectura permitidas;
  - las rutas sensibles están tapadas;
  - no hay red ni variables de entorno (no se ven tokens ni contraseñas);
  - /run está vacío (no hay acceso a Docker, D-Bus, Wayland ni Hyprland);
  - si ARTHUR termina, el proceso muere con él.
"""

import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 20_000


class SandboxUnavailable(Exception):
    pass


@dataclass(frozen=True)
class CommandOutput:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


class Sandbox:
    def __init__(self, read_roots: Iterable[Path], hidden: Iterable[Path], home: Path | None = None) -> None:
        self.read_roots = tuple(read_roots)
        self.hidden = tuple(hidden)
        self.home = (home or Path.home()).resolve()

    def build_argv(self, argv: list[str]) -> list[str]:
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            # Nunca ejecutamos sin aislamiento: si no hay bwrap, no se ejecuta.
            raise SandboxUnavailable("bubblewrap (bwrap) no está instalado")
        args = [
            bwrap,
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--tmpfs", "/run",
            "--tmpfs", str(self.home),
        ]
        for root in self.read_roots:
            if root.is_dir():
                args += ["--ro-bind", str(root), str(root)]
        for path in self.hidden:
            if any(path.is_relative_to(r) for r in self.read_roots):
                if path.is_dir():
                    args += ["--tmpfs", str(path)]
                elif path.exists():
                    args += ["--ro-bind", "/dev/null", str(path)]
        args += [
            "--unshare-all",  # sin red y con sus propios procesos, usuarios, etc.
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--setenv", "PATH", "/usr/bin",
            "--setenv", "HOME", str(self.home),
            "--setenv", "LANG", "C.UTF-8",
            "--chdir", "/tmp",
            "--",
            *argv,
        ]
        return args

    def run(self, argv: list[str], timeout: int) -> CommandOutput:
        try:
            proc = subprocess.run(
                self.build_argv(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env={},
            )
        except subprocess.TimeoutExpired as e:
            return CommandOutput(None, _text(e.stdout)[:MAX_OUTPUT], _text(e.stderr)[:MAX_OUTPUT], timed_out=True)
        return CommandOutput(proc.returncode, proc.stdout[:MAX_OUTPUT], proc.stderr[:MAX_OUTPUT])


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else data
