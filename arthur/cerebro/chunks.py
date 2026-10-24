"""Documentos y trozos del cerebro: qué se indexa y cómo se corta.

Un `Doc` es una nota de Obsidian, un apunte o un PDF ya leído. `split` lo corta en trozos de unos 800
caracteres siguiendo los encabezados, porque a la hora de buscar y de citar sirve más «Cálculo › Límites»
que un pedazo cualquiera del fichero. Reglas que importan:

- Una fórmula `$$…$$` o un bloque de código nunca se parte: media fórmula no se entiende y KaTeX no la
  puede pintar. Si es más larga que el trozo, va entera en un trozo más grande.
- Cada trozo empieza con la última frase del anterior (dentro del mismo apartado), para que una idea que
  cae justo en el corte se encuentre desde los dos lados.
- Lo que parece una clave se tapa con `[OCULTO]` antes de guardar nada: el índice y los trozos que salen
  hacia Workers AI no deben llevar secretos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..security.audit import SECRET_PATTERNS

SEPARADOR = " › "
OCULTO = "[OCULTO]"


@dataclass(frozen=True)
class Doc:
    source: str  # obsidian:cerebro, obsidian:business-lab, apuntes o pdf
    ref: str  # ruta relativa dentro de la fuente o apunte:<id>
    title: str
    subject: str  # asignatura o primera carpeta («PDF» en los PDF)
    changed_at: str  # ISO con zona
    text: str
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class Chunk:
    n: int
    heading: str
    text: str


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
# Bloques que no se parten: fórmulas $$…$$ (aunque estén sin cerrar, hasta el final) y código entre vallas.
_ATOMIC = re.compile(
    r"(\$\$.*?(?:\$\$|\Z)|^[ \t]*(?:```|~~~)[^\n]*\n.*?(?:^[ \t]*(?:```|~~~)[ \t]*$|\Z))",
    re.DOTALL | re.MULTILINE,
)
# Final de frase: el separador se guarda para saber si había un salto de línea (listas) o un espacio.
_SENTENCE_END = re.compile(r"(?<=[.!?…])(\s+)")


def hide_secrets(text: str) -> str:
    """Tapa con [OCULTO] lo que parece una clave. Se tapa solo la clave y no el trozo entero: así el resto de
    la nota («la contraseña del wifi está en el router») se sigue encontrando."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(OCULTO, text)
    return text


def _sections(text: str) -> list[tuple[str, str]]:
    """Divide el Markdown por encabezados. Devuelve (ruta «A › B», cuerpo) en orden.

    Un `#` dentro de un bloque de código (un comentario de Python) o de una fórmula no es un encabezado:
    por eso se lleva la cuenta de si estamos dentro de una valla ``` o de un $$ abierto."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    in_fence = in_math = False

    def cerrar() -> None:
        sections.append((SEPARADOR.join(t for _, t in stack), "\n".join(body)))

    for line in text.splitlines():
        if not in_fence and not in_math:
            m = _HEADING.match(line)
            if m:
                cerrar()
                body = []
                level = len(m.group(1))
                # Un encabezado del mismo nivel o mayor cierra los apartados abiertos debajo de él.
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, m.group(2).strip()))
                continue
        if not in_math and _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.count("$$") % 2 == 1:
            in_math = not in_math
        body.append(line)
    cerrar()
    return sections


def _units(body: str) -> list[tuple[str, str, bool]]:
    """Piezas que no se pueden dividir más: (texto, separador antes, ¿se puede solapar?).

    Las frases sí se solapan; las fórmulas y el código no (repetir una fórmula entera en el trozo siguiente
    solo engordaría el índice)."""
    units: list[tuple[str, str, bool]] = []
    for i, part in enumerate(_ATOMIC.split(body)):
        if i % 2 == 1:  # lo que ha casado el patrón: un bloque entero
            if part.strip():
                units.append((part.strip("\n"), "\n\n", False))
            continue
        for para in re.split(r"\n[ \t]*\n", part):
            para = para.strip()
            if not para:
                continue
            pieces = _SENTENCE_END.split(para)
            sep = "\n\n"
            for j in range(0, len(pieces), 2):
                if pieces[j]:
                    units.append((pieces[j], sep, True))
                if j + 1 < len(pieces):
                    sep = "\n" if "\n" in pieces[j + 1] else " "
    return units


def _cut_long(text: str, size: int) -> list[str]:
    """Una «frase» sin puntos más larga que el trozo (una tabla, una lista sin puntos) se corta por palabras."""
    if len(text) <= size:
        return [text]
    out, current = [], ""
    for word in text.split(" "):
        if current and len(current) + 1 + len(word) > size:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        out.append(current)
    return out


def _pack(units: list[tuple[str, str, bool]], size: int) -> list[str]:
    pieces: list[str] = []
    current: list[tuple[str, str]] = []
    length = 0
    fresh = False  # ¿el trozo actual tiene algo más que la frase repetida del anterior?
    last_overlap: str | None = None

    def flush() -> None:
        nonlocal current, length, fresh
        if fresh:
            pieces.append("".join((sep if k else "") + t for k, (t, sep) in enumerate(current)))
        current, length, fresh = [], 0, False

    expanded: list[tuple[str, str, bool]] = []
    for text, sep, overlap in units:
        if overlap:
            for k, part in enumerate(_cut_long(text, size)):
                expanded.append((part, sep if k == 0 else " ", True))
        else:
            expanded.append((text, sep, False))

    for text, sep, overlap in expanded:
        extra = len(text) + (len(sep) if current else 0)
        if fresh and length + extra > size:
            flush()
            # La frase repetida solo entra si deja sitio de sobra: si no, el trozo nuevo sería casi igual.
            if last_overlap is not None and len(last_overlap) <= size // 2:
                current, length = [(last_overlap, "")], len(last_overlap)
            extra = len(text) + (len(sep) if current else 0)
        current.append((text, sep))
        length += extra
        fresh = True
        last_overlap = text if overlap else None
    flush()
    return pieces


def split(doc: Doc, size: int = 800) -> list[Chunk]:
    """Corta el documento en trozos de `size` caracteres como mucho (salvo una fórmula o código más largos)."""
    chunks: list[Chunk] = []
    for heading, body in _sections(doc.text):
        for text in _pack(_units(body), size):
            text = hide_secrets(text).strip()
            if text:
                chunks.append(Chunk(n=len(chunks), heading=hide_secrets(heading), text=text))
    return chunks
