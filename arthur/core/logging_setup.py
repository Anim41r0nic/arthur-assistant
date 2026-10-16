"""Configuración del registro (logging).

- Consola: solo avisos y errores, para no ensuciar la conversación
  (con --debug se muestra todo).
- Fichero: ~/.local/state/arthur/arthur.log, con rotación (máx. 3 × 1 MB).

Privacidad: el contenido de lo que escribes solo se registra en nivel DEBUG.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig, state_dir

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Como RotatingFileHandler, pero cada fichero nace con permisos 600 (solo tu usuario).

    El registro cuenta qué herramientas se usan y qué falla (y con DEBUG, lo que dices): lo mismo que
    audit.jsonl y la memoria, que ya son 600. Con el comportamiento normal nacía con la máscara del
    proceso (644) y solo lo protegía la carpeta.
    """

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def setup_logging(cfg: LoggingConfig, *, debug: bool = False, service: bool = False) -> Path | None:
    """Configura el logger `arthur` y devuelve la ruta del fichero de log (si hay)."""
    root = logging.getLogger("arthur")
    root.handlers.clear()
    root.propagate = False
    root.setLevel(logging.DEBUG if debug else cfg.level)

    # Con pythonw.exe (el aparato Windows, lanzado por una tarea programada) no hay consola: sys.stderr es None
    # y todo va solo al fichero.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        # Como servicio, la "consola" es el registro de systemd (journal): ahí sí queremos lo normal (INFO).
        console.setLevel(logging.DEBUG if debug else logging.INFO if service else logging.WARNING)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)

    if not cfg.to_file:
        return None
    log_dir = state_dir()
    # 0o700: solo tu usuario puede entrar en la carpeta de logs.
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = log_dir / "arthur.log"
    for old_file in log_dir.glob("arthur.log*"):
        old_file.chmod(0o600)  # los creados por versiones anteriores (644) se cierran también
    file_handler = PrivateRotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)
    return path
