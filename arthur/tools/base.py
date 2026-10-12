"""Piezas comunes de todas las herramientas.

Cada herramienta declara:
  - nombre y descripción (lo que leerá el LLM en la Fase 4);
  - sus parámetros, con tipo y límites (se validan ANTES de ejecutar);
  - su nivel de riesgo: READ, WRITE o DANGEROUS;
  - opcionalmente, una comprobación de seguridad propia (`check`).
"""

import enum
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


class Risk(enum.StrEnum):
    READ = "read"            # solo consulta, no cambia nada
    LOW = "low"              # acción trivial y reversible (notificar, volumen, abrir una app de confianza)
    WRITE = "write"          # crea o modifica algo
    PRIVATE = "private"      # lee datos que pueden ser privados (p. ej. el portapapeles)
    DANGEROUS = "dangerous"  # ejecuta programas o puede tener efectos difíciles de deshacer


class ToolArgumentError(Exception):
    pass


class ToolDenied(Exception):
    """Lo lanza una herramienta cuando su comprobación de seguridad dice que no."""


class ToolError(Exception):
    """Fallo esperado y explicable (p. ej. "no es un fichero"): no es un bug."""


@dataclass(frozen=True)
class Param:
    name: str
    type: type  # str, int, bool o list (lista de textos)
    description: str
    required: bool = True
    default: Any = None
    max_length: int | None = None  # para textos y listas
    minimum: int | None = None
    maximum: int | None = None

    def json_schema(self) -> dict[str, Any]:
        names = {str: "string", int: "integer", bool: "boolean", list: "array"}
        schema: dict[str, Any] = {"type": names[self.type]}
        if self.description:  # sin descripción si el nombre ya lo dice todo (menos texto para el modelo)
            schema["description"] = self.description
        if self.type is list:
            schema["items"] = {"type": "string"}
        return schema


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool: str
    status: Literal["ok", "denied", "cancelled", "error"]
    output: str

    def as_text(self) -> str:
        prefix = {"ok": "", "denied": "[DENEGADO] ", "cancelled": "[CANCELADO] ", "error": "[ERROR] "}
        return prefix[self.status] + self.output


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    risk: Risk
    run: Callable[[dict[str, Any]], str]
    params: tuple[Param, ...] = ()
    check: Callable[[dict[str, Any]], None] | None = None
    summarize: Callable[[dict[str, Any]], str] | None = None
    # Para herramientas cuyo riesgo depende de los argumentos (p. ej. qué app se abre).
    # Solo puede SUBIR o BAJAR el riesgo dentro de lo que la herramienta decida; la política sigue mandando.
    risk_for: Callable[[dict[str, Any]], Risk] | None = None
    # La salida ya es una respuesta legible (p. ej. la agenda): se te da tal cual, sin una segunda pasada del
    # modelo que solo la repetiría con otras palabras (~15 s de generación en este portátil).
    direct_reply: bool = False

    def effective_risk(self, arguments: dict[str, Any]) -> Risk:
        return self.risk_for(arguments) if self.risk_for else self.risk

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        known = {p.name for p in self.params}
        unknown = arguments.keys() - known
        if unknown:
            raise ToolArgumentError(f"Argumentos desconocidos: {', '.join(sorted(unknown))}")
        result = {}
        for p in self.params:
            if p.name not in arguments:
                if p.required:
                    raise ToolArgumentError(f"Falta el argumento obligatorio '{p.name}'")
                result[p.name] = p.default
                continue
            value = arguments[p.name]
            if type(value) is not p.type:
                raise ToolArgumentError(f"'{p.name}' debe ser de tipo {p.type.__name__}")
            if p.type is list and not all(type(v) is str for v in value):
                raise ToolArgumentError(f"'{p.name}' debe ser una lista de textos")
            if p.max_length is not None and len(value) > p.max_length:
                raise ToolArgumentError(f"'{p.name}' es demasiado largo (máximo {p.max_length})")
            if p.minimum is not None and value < p.minimum:
                raise ToolArgumentError(f"'{p.name}' debe ser como mínimo {p.minimum}")
            if p.maximum is not None and value > p.maximum:
                raise ToolArgumentError(f"'{p.name}' debe ser como máximo {p.maximum}")
            result[p.name] = value
        return result

    def summary(self, arguments: dict[str, Any]) -> str:
        if self.summarize:
            return self.summarize(arguments)
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in arguments.items())})"

    def json_schema(self) -> dict[str, Any]:
        """Descripción de la herramienta en el formato que entienden los LLM."""
        # Lo justo: cada carácter cuesta tiempo de procesado en el modelo local.
        # (Los argumentos los valida ARTHUR igualmente en validate().)
        schema: dict[str, Any] = {"type": "object", "properties": {p.name: p.json_schema() for p in self.params}}
        required = [p.name for p in self.params if p.required]
        if required:
            schema["required"] = required
        return {"name": self.name, "description": self.description, "input_schema": schema}
