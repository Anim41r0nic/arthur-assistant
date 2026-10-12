"""Contrato común de todos los "cerebros".

Cualquier cerebro (reglas, un LLM local, un LLM gratuito en la nube) implementa
el mismo método `reply`, así el núcleo no cambia cuando cambiamos de cerebro.

El cerebro recibe la conversación y responde con texto y, opcionalmente, con
llamadas a herramientas. El cerebro NUNCA ejecuta nada: solo lo propone.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from ..tools.base import ToolCall


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCall, ...] = ()  # solo en mensajes del asistente
    tool_call_id: str | None = None        # solo en resultados de herramientas
    timestamp: datetime | None = None      # cuándo lo escribió el usuario
    images: tuple[str, ...] = ()           # fotos (JPEG/PNG en base64) que van con el mensaje del usuario


class BrainError(Exception):
    """Fallo esperado del cerebro (p. ej. Ollama apagado). Su mensaje se le muestra al usuario."""


@dataclass(frozen=True)
class BrainReply:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()


class Brain(Protocol):
    name: str
    label: str  # descripción corta para la interfaz, p. ej. "ollama · qwen3:4b"

    def reply(self, conversation: Sequence[Message]) -> BrainReply: ...
