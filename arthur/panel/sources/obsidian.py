"""Casillas `- [ ] Yo: …` de la bóveda de Obsidian: leerlas y cambiar su estado.

Solo se tocan ficheros `.md` dentro de la bóveda, sin carpetas ocultas (`.obsidian`, `.git`…) y solo
las casillas cuyo texto empieza por `Yo:`; las de otros (p. ej. `Claude:`) no son suyas.
Cambiar una casilla modifica solo el carácter de los corchetes: el resto del fichero queda igual.
"""

from __future__ import annotations

import re
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import SourceError

MAX_CHECKBOXES = 100
_CASILLA = re.compile(r"^(\s*[-*+] \[)([ xX])(\] )(Yo:.*)$")


class LineChanged(SourceError):
    """La línea ya no es la casilla que Marc vio (se editó el fichero entretanto)."""


@dataclass(frozen=True)
class Checkbox:
    file: str  # relativo a la bóveda, con «/»
    line: int  # desde 1
    text: str  # incluye «Yo: …»
    done: bool


def _lineas_casillas(contenido: str):
    """(número de línea desde 1, línea sin salto, coincidencia) de las casillas de Marc fuera de código."""
    valla = None  # carácter de la valla abierta («`» o «~») o None
    for n, linea in enumerate(contenido.split("\n"), start=1):
        limpia = linea.strip()
        if limpia.startswith(("```", "~~~")):
            if valla is None:
                valla = limpia[0]
            elif limpia[0] == valla:
                valla = None
            continue
        if valla is not None:
            continue
        m = _CASILLA.match(linea.rstrip("\r"))
        if m:
            yield n, linea, m


def _oculta(partes) -> bool:
    return any(p.startswith(".") for p in partes)


def valid_file(file: str) -> bool:
    """Solo rutas relativas a notas visibles, sin saltos de carpeta."""
    if not isinstance(file, str) or not file or "\x00" in file or "\\" in file:
        return False
    rel = Path(file)
    return (not rel.is_absolute() and not _oculta(rel.parts)
            and ".." not in rel.parts and rel.suffix == ".md"
            and rel.as_posix() == file)


class ObsidianVault:
    def __init__(self, root: Path, files: tuple[str, ...] = ("00-Inicio/Tareas.md",)) -> None:
        self.root = Path(root)
        if not files or any(not valid_file(file) for file in files):
            raise ValueError("Lista de notas de Obsidian no válida")
        self.files = files
        # El servidor atiende cada petición en su hilo: sin este cerrojo, dos casillas del mismo fichero
        # marcadas a la vez pasarían las dos la comprobación y la segunda pisaría a la primera.
        self._cerrojo = threading.Lock()

    def _raiz(self) -> Path:
        if not self.root.is_dir():
            raise SourceError("La carpeta de Obsidian no está")
        return self.root.resolve()

    def checkboxes(self) -> list[Checkbox]:
        raiz = self._raiz()
        encontradas: list[Checkbox] = []
        for rel in self.files:
            ruta = raiz / rel
            real = ruta.resolve()
            # La lista de ajustes nunca da acceso a enlaces hacia carpetas ocultas o externas.
            if (not real.is_relative_to(raiz) or _oculta(real.relative_to(raiz).parts)
                    or not real.is_file()):
                continue
            try:
                contenido = real.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for n, _linea, m in _lineas_casillas(contenido):
                encontradas.append(Checkbox(rel, n, m.group(4).rstrip(), m.group(2).lower() == "x"))
                if len(encontradas) >= MAX_CHECKBOXES:
                    return encontradas
        return encontradas

    def _resolver(self, file: str) -> tuple[Path, Path]:
        raiz = self._raiz()
        if not valid_file(file):
            raise SourceError("Fichero no válido")
        if file not in self.files:
            raise SourceError("Fichero no incluido en los ajustes")
        rel = Path(file)
        real = (raiz / rel).resolve()
        if not real.is_relative_to(raiz) or _oculta(real.relative_to(raiz).parts):
            raise SourceError("Fichero fuera de la bóveda")
        if not real.is_file():
            raise SourceError("El fichero no existe")
        return raiz, real

    def toggle(self, file: str, line: int, text: str) -> Checkbox:
        with self._cerrojo:
            return self._toggle(file, line, text)

    def _toggle(self, file: str, line: int, text: str) -> Checkbox:
        _raiz, real = self._resolver(file)
        try:
            original_bytes = real.read_bytes()
            original = original_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise SourceError("No se puede leer el fichero") from e
        # Se comprueba que la línea sigue siendo la que Marc vio antes de tocar nada.
        for n, linea, m in _lineas_casillas(original):
            if n == line and m.group(4).rstrip() == text.rstrip():
                lineas = original.split("\n")
                done = m.group(2) == " "
                lineas[n - 1] = m.group(1) + ("x" if done else " ") + linea[m.end(2):]
                try:
                    self._escribir(real, "\n".join(lineas).encode("utf-8"), original_bytes)
                except OSError as exc:
                    raise SourceError("No se puede guardar la nota de Obsidian") from exc
                return Checkbox(Path(file).as_posix(), line, text.rstrip(), done)
        raise LineChanged("La línea ha cambiado")

    @staticmethod
    def _escribir(destino: Path, datos: bytes, esperado: bytes) -> None:
        # Fichero temporal en la misma carpeta + replace: nunca queda a medias; se conservan los permisos.
        modo = destino.stat().st_mode & 0o7777
        fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=".arthur-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(datos)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, modo)
            # Syncthing u Obsidian pueden haber editado la nota durante la preparación.
            if destino.read_bytes() != esperado:
                raise LineChanged("La nota ha cambiado")
            os.replace(tmp, destino)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
