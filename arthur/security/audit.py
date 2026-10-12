"""Registro de auditoría: una línea JSON por cada acción que ARTHUR intenta.

Fichero: ~/.local/state/arthur/audit.jsonl (permisos 600: solo tu usuario).
Se guarda qué herramienta, con qué argumentos, qué se decidió y cómo acabó.
Los textos largos se recortan y lo que parece un secreto se tapa.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_VALUE_LENGTH = 200

SECRET_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{10,}",  # tokens de GitHub
        r"\bsk-[A-Za-z0-9_-]{10,}",                         # claves tipo sk-… (APIs de IA)
        r"\bAKIA[0-9A-Z]{16}\b",                            # claves de AWS
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\b(password|passwd|contraseña|token|secret|api[_-]?key)\s*[=:]\s*\S+",
    )
]


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            value = pattern.sub("[OCULTO]", value)
        if len(value) > MAX_VALUE_LENGTH:
            value = f"{value[:MAX_VALUE_LENGTH]}…(+{len(value) - MAX_VALUE_LENGTH} caracteres)"
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        """`path=None` desactiva el registro (útil en los tests)."""
        self.path = path

    def record(self, **fields: Any) -> None:
        if self.path is None:
            return
        entry = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), **redact(fields)}
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # O_APPEND: solo añadir al final. 0o600: solo tu usuario puede leerlo.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
