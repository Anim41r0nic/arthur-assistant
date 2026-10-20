"""Ajustes del cerebro: <datos>/cerebro/ajustes.toml (presupuesto diario, carpetas de las bóvedas y zona).

Se lee con `tomllib`; el fichero por defecto se escribe a mano (la biblioteca estándar no escribe TOML). Las
carpetas por defecto son las del contenedor: compose monta las bóvedas de Syncthing en /vaults, solo lectura.
Los ajustes del Repaso van en su tabla `[repaso]` (asignaturas activas, hora del aviso y retención de FSRS). Esa tabla
sí se reescribe desde la página (POST /api/repaso/ajustes): `save_repaso` cambia solo ese trozo del fichero y deja
el resto (y los comentarios) como lo dejó Marc.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.config import data_dir

DEFAULT_BUDGET = 3000
MAX_BUDGET = 10_000  # la capa gratis entera de la cuenta: más no tiene sentido
DEFAULT_TEXT = (
    "# Neurons de Workers AI al día para el cerebro (la cuenta tiene 10 000 compartidos con el chat de la web).\n"
    f"presupuesto = {DEFAULT_BUDGET}\n"
    'zona = "Europe/Madrid"\n'
    "\n"
    "# Carpetas dentro del contenedor (solo lectura).\n"
    'boveda_cerebro = "/vaults/cerebro"\n'
    'boveda_negocio = "/vaults/business-lab"\n'
    'pdf = "/vaults/cerebro/PDF"\n'
)

# Las carpetas de este curso (si en Apuntes se llaman así; si no, se eligen en la página)
DEFAULT_SUBJECTS = ("MTP", "ETC", "Álgebra", "Cálculo")
DEFAULT_NOTICE = time(20, 0)
DEFAULT_RETENTION = 0.9
MIN_RETENTION, MAX_RETENTION = 0.7, 0.97  # por debajo se olvida demasiado; por encima, repaso cada pocos días
MAX_SUBJECTS = 30
MAX_SUBJECT = 100
HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
REPASO_HEADER = "[repaso]"


class CerebroError(Exception):
    pass


@dataclass(frozen=True)
class RepasoSettings:
    subjects: tuple[str, ...] = DEFAULT_SUBJECTS
    notice: time = DEFAULT_NOTICE
    retention: float = DEFAULT_RETENTION

    @property
    def notice_text(self) -> str:
        return self.notice.strftime("%H:%M")


@dataclass(frozen=True)
class Settings:
    budget: int
    zone: ZoneInfo
    vault_cerebro: Path
    vault_business: Path
    pdf: Path
    repaso: RepasoSettings = field(default_factory=RepasoSettings)


def cerebro_dir() -> Path:
    return data_dir() / "cerebro"


def settings_path() -> Path:
    return cerebro_dir() / "ajustes.toml"


def _path(data: dict, key: str, default: str) -> Path:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise CerebroError(f"Valor no válido en «{key}»: {value!r} (tiene que ser una carpeta)")
    return Path(value)


def subjects_value(value) -> tuple[str, ...]:
    """Las asignaturas activas, comprobadas: lista de nombres (sin repetir, en el orden dado)."""
    if not isinstance(value, list) or len(value) > MAX_SUBJECTS:
        raise CerebroError(f"«asignaturas» tiene que ser una lista de hasta {MAX_SUBJECTS} nombres")
    out: list[str] = []
    for v in value:
        name = " ".join(v.split()) if isinstance(v, str) else ""
        if not name or len(name) > MAX_SUBJECT or any(ord(c) < 32 for c in name):
            raise CerebroError(f"Cada asignatura tiene que ser un nombre de hasta {MAX_SUBJECT} caracteres")
        if name not in out:
            out.append(name)
    return tuple(out)


def notice_value(value) -> time:
    if not isinstance(value, str) or not HHMM.fullmatch(value):
        raise CerebroError("«aviso» tiene que ser una hora «HH:MM» (por ejemplo, 20:00)")
    return time(int(value[:2]), int(value[3:]))


def retention_value(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not MIN_RETENTION <= value <= MAX_RETENTION:
        raise CerebroError(f"«retencion» tiene que estar entre {MIN_RETENTION} y {MAX_RETENTION}")
    return float(value)


def _repaso(data: dict) -> RepasoSettings:
    table = data.get("repaso", {})
    if not isinstance(table, dict):
        raise CerebroError("«repaso» tiene que ser una tabla [repaso]")
    return RepasoSettings(subjects=subjects_value(table.get("asignaturas", list(DEFAULT_SUBJECTS))),
                          notice=notice_value(table.get("aviso", DEFAULT_NOTICE.strftime("%H:%M"))),
                          retention=retention_value(table.get("retencion", DEFAULT_RETENTION)))


def _repaso_block(r: RepasoSettings) -> str:
    # Las cadenas de JSON son cadenas básicas válidas de TOML (mismos escapes: \" \\ \uXXXX)
    names = ", ".join(json.dumps(n, ensure_ascii=False) for n in r.subjects)
    return (f"{REPASO_HEADER}\n"
            "# Asignaturas activas (carpetas de Apuntes), hora del aviso diario y retención de FSRS (0.9 = recordar el\n"
            "# 90 % el día que vuelve a salir). Se cambian desde la pestaña Repaso de Apuntes.\n"
            f"asignaturas = [{names}]\n"
            f'aviso = "{r.notice_text}"\n'
            f"retencion = {r.retention}\n")


def _write_default(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(DEFAULT_TEXT + "\n" + _repaso_block(RepasoSettings()), encoding="utf-8")


def save_repaso(path: Path, *, subjects: tuple[str, ...], notice: time, retention: float) -> RepasoSettings:
    """Reescribe solo la tabla [repaso] de ajustes.toml (va siempre al final: se quita la vieja y se añade la nueva).
    Se escribe en un fichero aparte y se cambia de golpe: un corte a medias no deja los ajustes ilegibles."""
    new = RepasoSettings(subjects=subjects_value(list(subjects)), notice=notice,
                         retention=retention_value(retention))
    if not path.exists():
        _write_default(path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    for line in lines:
        header = line.strip()
        if header.startswith("["):
            inside = header == REPASO_HEADER
        if not inside:
            kept.append(line)
    head = "".join(kept).rstrip("\n")
    text = (head + "\n\n" if head else "") + _repaso_block(new)
    tomllib.loads(text)  # si saliera algo ilegible, mejor fallar aquí que al arrancar
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return new


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    if not path.exists():
        _write_default(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise CerebroError(f"Ajustes ilegibles ({path.name}): {exc}") from exc
    budget = data.get("presupuesto", DEFAULT_BUDGET)
    if not isinstance(budget, int) or isinstance(budget, bool) or not 0 <= budget <= MAX_BUDGET:
        raise CerebroError(f"«presupuesto» tiene que ser un número entero entre 0 y {MAX_BUDGET}")
    # El resto del cerebro (días del Repaso, racha) cuenta en hora de Madrid: otra zona descuadraría los días.
    if data.get("zona", "Europe/Madrid") != "Europe/Madrid":
        raise CerebroError("«zona» debe ser Europe/Madrid en esta versión del cerebro")
    cerebro = _path(data, "boveda_cerebro", "/vaults/cerebro")
    return Settings(budget=budget, zone=ZoneInfo("Europe/Madrid"), vault_cerebro=cerebro,
                    vault_business=_path(data, "boveda_negocio", "/vaults/business-lab"),
                    pdf=_path(data, "pdf", str(cerebro / "PDF")), repaso=_repaso(data))
