"""Validación de comandos para run_command.

Los comandos llegan como lista de argumentos (["ls", "-la", "~/Proyectos"]),
NUNCA como texto para una shell. Sin shell no existen `;`, `&&`, `|`, `$(...)`
ni redirecciones: cada argumento es solo texto que recibe el programa.

Capas:
  1. Lista negra fija: programas que nunca se ejecutan, aunque alguien los
     añada por error a la lista permitida.
  2. Lista permitida (config): solo se ejecuta lo que está en ella.
  3. Cualquier argumento que parezca una ruta pasa por la política de rutas.
"""

import shutil
from pathlib import Path

from .paths import PathDenied, PathPolicy

MAX_ARGS = 32
MAX_ARG_LENGTH = 512

NEVER_ALLOWED = frozenset({
    # Obtener privilegios de administrador
    "sudo", "su", "doas", "pkexec", "run0",
    # Borrar o destruir datos y discos
    "rm", "rmdir", "shred", "dd", "wipefs", "fdisk", "sfdisk", "cfdisk", "parted", "mkswap", "truncate",
    # Apagar el sistema o controlar servicios y procesos
    "shutdown", "reboot", "poweroff", "halt", "systemctl", "kill", "killall", "pkill",
    # Cambiar permisos o mover ficheros
    "chmod", "chown", "chattr", "mv", "cp", "ln",
    # Shells e intérpretes: podrían ejecutar cualquier cosa
    "sh", "bash", "zsh", "fish", "dash", "python", "python3", "perl", "ruby", "node", "lua",
    # "Envoltorios" que lanzan otros programas (env rm…, xargs rm…, find -delete…)
    "env", "xargs", "find", "nice", "nohup", "setsid", "timeout", "stdbuf", "watch", "script",
    "strace", "gdb", "busybox",
    # Red, descargas y gestores de paquetes
    "curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "rsync",
    "pacman", "paru", "yay", "pip", "npm", "npx",
    # Herramientas con capacidad de ejecutar código o tocar el sistema
    "git", "docker", "podman", "crontab", "at", "hyprctl",
})


class CommandDenied(Exception):
    pass


class CommandPolicy:
    def __init__(self, allowlist: tuple[str, ...], paths: PathPolicy) -> None:
        self.allowlist = frozenset(allowlist)
        self.paths = paths

    def check(self, argv: list[str]) -> list[str]:
        """Valida el comando y devuelve los argumentos listos para ejecutar."""
        if not argv:
            raise CommandDenied("Comando vacío")
        if len(argv) > MAX_ARGS or any(len(a) > MAX_ARG_LENGTH or "\0" in a for a in argv):
            raise CommandDenied("Comando demasiado largo o con caracteres no válidos")

        program = argv[0]
        if "/" in program:
            raise CommandDenied("Indica solo el nombre del programa, sin ruta")
        if program in NEVER_ALLOWED or program.startswith("mkfs"):
            raise CommandDenied(f"'{program}' está bloqueado siempre por seguridad")
        if program not in self.allowlist:
            raise CommandDenied(
                f"'{program}' no está en la lista de programas permitidos ({', '.join(sorted(self.allowlist))})"
            )
        executable = shutil.which(program)
        if executable is None:
            raise CommandDenied(f"'{program}' no está instalado")

        args = []
        for arg in argv[1:]:
            if arg.startswith(("/", "~")) or "/" in arg:
                # Sin shell nadie expande ~, así que lo hacemos nosotros tras validar.
                try:
                    arg = str(self.paths.check_read(arg))
                except PathDenied as e:
                    raise CommandDenied(str(e)) from e
            args.append(arg)
        return [str(Path(executable).resolve()), *args]
