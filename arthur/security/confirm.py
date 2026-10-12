"""Confirmaciones: preguntarte antes de hacer algo con riesgo.

Cada interfaz (terminal, voz, Orb) tendrá su forma de preguntar, pero todas
cumplen este mismo contrato. Ante la duda, la respuesta es NO.
"""

import unicodedata
from dataclasses import dataclass
from typing import Protocol

from ..tools.base import Risk

# Categorías de Unicode que no se dibujan o que cambian lo que se ve: control (\r, \n, ESC…), formato (inversión
# de texto, espacios de ancho cero) y separadores de línea o de párrafo.
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def visible_text(text: str) -> str:
    """El texto tal como debe verse en una confirmación: lo que no se dibuja se muestra escrito (\\r, \\x1b…).

    El resumen de una confirmación incluye texto que escribe el modelo (o un tercero, a través del modelo).
    En una terminal, un `\\r` o una secuencia de escape podrían pisar la pregunta y mostrar otra acción
    distinta de la que se va a ejecutar; un carácter de inversión de texto o de ancho cero, esconder parte.
    """
    return "".join(
        ch.encode("unicode_escape").decode("ascii") if unicodedata.category(ch) in INVISIBLE_CATEGORIES else ch
        for ch in text
    )


@dataclass(frozen=True)
class ConfirmationRequest:
    tool: str
    risk: Risk
    summary: str  # descripción legible: "Ejecutar en sandbox: ls -la /home/usuario/Proyectos"


class Confirmer(Protocol):
    def confirm(self, request: ConfirmationRequest) -> bool: ...


class DenyAll:
    """Para cuando no hay nadie a quien preguntar: todo lo que requiere confirmación se rechaza."""

    def confirm(self, request: ConfirmationRequest) -> bool:
        return False
