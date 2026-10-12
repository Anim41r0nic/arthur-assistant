"""Catálogo de herramientas disponibles. Solo existe lo que se registra aquí."""

import re
import threading
from typing import Any

from .base import Tool

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # En el servidor, los aparatos dan de alta y de baja sus herramientas desde otros hilos (fase S3).
        self._lock = threading.Lock()

    def register(self, tool: Tool) -> None:
        if not NAME_PATTERN.match(tool.name):
            raise ValueError(f"Nombre de herramienta no válido: {tool.name!r}")
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"Herramienta duplicada: {tool.name}")
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> Tool | None:
        with self._lock:
            return self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        with self._lock:
            return self._tools.get(name)

    def all(self) -> list[Tool]:
        with self._lock:
            return sorted(self._tools.values(), key=lambda t: t.name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.json_schema() for t in self.all()]
