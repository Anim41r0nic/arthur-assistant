"""Qué rutas puede leer o escribir ARTHUR.

Reglas, en este orden:
  1. La ruta debe ser absoluta (o empezar por ~). Las relativas son ambiguas.
  2. Se resuelve la ruta real (siguiendo enlaces simbólicos y los `..`), para
     que nadie pueda "escaparse" con trucos como ~/Proyectos/../.ssh.
  3. Debe estar dentro de una carpeta permitida (read_roots / write_roots).
  4. Nunca puede ser una ruta sensible (claves, contraseñas, tokens…),
     aunque esté dentro de una carpeta permitida.
  5. ARTHUR no puede escribir en su propio código.
"""

import fnmatch
from collections.abc import Iterable
from pathlib import Path

from ..core.config import SecurityConfig, data_dir, default_config_path, state_dir

# Carpetas que nunca se leen ni se escriben (relativas a tu carpeta personal).
SENSITIVE_HOME_DIRS = (
    ".ssh", ".gnupg", ".password-store", ".pki",
    ".local/share/keyrings", ".local/share/keyrings-backup",
    ".mozilla", ".config/chromium", ".config/google-chrome", ".config/BraveSoftware",
    ".config/gh", ".docker", ".aws", ".kube", ".azure", ".config/gcloud",
    ".claude", "Claves recuperacion",
)

# Nombres de fichero sensibles, estén donde estén.
SENSITIVE_NAMES = (
    ".env", ".env.*", "*.env", ".netrc", ".pgpass", ".git-credentials", ".claude.json",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.gpg", "*.asc",
)

PROJECT_DIR = Path(__file__).resolve().parents[2]


class PathDenied(Exception):
    pass


def _resolve_all(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    return tuple(Path(p).expanduser().resolve() for p in paths)


class PathPolicy:
    def __init__(
        self,
        read_roots: Iterable[str | Path],
        write_roots: Iterable[str | Path],
        home: Path | None = None,
    ) -> None:
        home = (home or Path.home()).resolve()
        self.read_roots = _resolve_all(read_roots)
        self.write_roots = _resolve_all(write_roots)
        # La configuración, el registro y los datos de ARTHUR también son intocables
        # con las herramientas de ficheros: así no puede desactivar su propia
        # seguridad, borrar sus huellas ni manipular su memoria por la puerta de atrás.
        self.sensitive_dirs = (
            *(home / d for d in SENSITIVE_HOME_DIRS),
            default_config_path().parent.resolve(),
            state_dir().resolve(),
            data_dir().resolve(),  # memoria, voces y modelos: solo a través de sus herramientas
        )
        self.no_write_dirs = (PROJECT_DIR,)

    @classmethod
    def from_config(cls, cfg: SecurityConfig) -> "PathPolicy":
        return cls(cfg.read_roots, cfg.write_roots)

    def check_read(self, raw: str) -> Path:
        return self._check(raw, self.read_roots, "leer")

    def check_write(self, raw: str) -> Path:
        path = self._check(raw, self.write_roots, "escribir")
        if any(path.is_relative_to(d) for d in self.no_write_dirs):
            raise PathDenied(f"No puedo modificar mi propio código: {path}")
        return path

    def is_sensitive(self, path: Path) -> bool:
        if any(path.is_relative_to(d) for d in self.sensitive_dirs):
            return True
        return any(fnmatch.fnmatch(part, pattern) for part in path.parts for pattern in SENSITIVE_NAMES)

    def _check(self, raw: str, roots: tuple[Path, ...], action: str) -> Path:
        if not raw or "\0" in raw:
            raise PathDenied("Ruta vacía o no válida")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise PathDenied(f"Usa una ruta absoluta o que empiece por ~/: {raw!r}")
        path = path.resolve()
        if self.is_sensitive(path):
            raise PathDenied(f"Ruta protegida, no puedo {action} aquí: {path}")
        if not any(path.is_relative_to(root) for root in roots):
            allowed = ", ".join(str(r) for r in roots) or "(ninguna)"
            raise PathDenied(f"No tengo permiso para {action} en {path}. Carpetas permitidas: {allowed}")
        return path
