"""Ajustes del panel: <datos>/panel/ajustes.toml (zona, canal de aviso, horas del resumen, bóveda de Obsidian y,
desde 0.67.0, la revisión: hora de la revisión nocturna, hora de la foto del plan y prefijo de los bloques de estudio).

Se lee con `tomllib`; el fichero por defecto se escribe a mano (la biblioteca estándar no escribe TOML).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import PanelError, panel_dir

DAYS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
DEFAULT_TIMES = {"lunes": "08:30", "martes": "07:00", "miercoles": "08:30", "jueves": "10:00",
                 "viernes": "07:00", "sabado": "10:00", "domingo": "10:00"}
DEFAULT_TEXT = (
    'zona = "Europe/Madrid"\n'
    'canal = "aviso"\n'
    "\n"
    "[resumen]\n"
    + "".join(f'{d} = "{h}"\n' for d, h in DEFAULT_TIMES.items())
    + "\n[obsidian]\n"
    'carpeta = "/vault"\n'
    'ficheros = ["00-Inicio/Tareas.md"]\n'
    "\n[revision]\n"
    'hora = "21:30"               # revisión de la noche (el domingo, la de la semana)\n'
    'foto = "08:00"               # foto del plan: lo que toca hoy\n'
    'prefijo_estudio = "Estudio"  # los eventos del calendario que empiezan así son bloques de estudio\n'
)
REVISION_KEYS = ("hora", "foto", "prefijo_estudio")
_HORA = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Settings:
    times: dict[int, time]   # clave = date.weekday() (0 = lunes)
    zone: ZoneInfo
    channel: str
    vault: Path
    files: tuple[str, ...]
    revision_time: time = time(21, 30)
    photo_time: time = time(8, 0)
    study_prefix: str = "Estudio"


def settings_path() -> Path:
    return panel_dir() / "ajustes.toml"


def _parse_time(key: str, value: object) -> time:
    m = _HORA.match(value) if isinstance(value, str) else None
    if not m:
        raise PanelError(f"Hora no válida en «{key}»: {value!r} (usa HH:MM, p. ej. 07:30)")
    return time(int(m.group(1)), int(m.group(2)))


def _text(data: dict, key: str, default: str, where: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise PanelError(f"Valor no válido en «{where}{key}»: {value!r}")
    return value


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(DEFAULT_TEXT, encoding="utf-8")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PanelError(f"Ajustes ilegibles ({path.name}): {exc}") from exc
    resumen = data.get("resumen", {})
    obsidian = data.get("obsidian", {})
    revision = data.get("revision", {})   # los ficheros de antes de 0.67.0 no la tienen: valores por defecto
    if not isinstance(resumen, dict) or not isinstance(obsidian, dict) or not isinstance(revision, dict):
        raise PanelError("«resumen», «obsidian» y «revision» deben ser tablas")
    for key in revision:
        if key not in REVISION_KEYS:
            raise PanelError(f"Clave desconocida en «revision»: «{key}» (usa {', '.join(REVISION_KEYS)})")
    revision_time = _parse_time("revision.hora", revision.get("hora", "21:30"))
    photo_time = _parse_time("revision.foto", revision.get("foto", "08:00"))
    if photo_time >= revision_time:
        raise PanelError("«revision.foto» tiene que ser antes que «revision.hora»")
    prefix = _text(revision, "prefijo_estudio", "Estudio", "revision.")
    if not prefix.strip() or len(prefix) > 40:
        raise PanelError("«revision.prefijo_estudio» debe tener entre 1 y 40 letras")
    for key in resumen:
        if key not in DAYS:
            raise PanelError(f"Clave desconocida en «resumen»: «{key}» (usa {', '.join(DAYS)})")
    times = {i: _parse_time(d, resumen.get(d, DEFAULT_TIMES[d])) for i, d in enumerate(DAYS)}
    zona = _text(data, "zona", "Europe/Madrid", "")
    # El calendario y la página también usan Madrid; otra zona descoordinaría el día y el resumen.
    if zona != "Europe/Madrid":
        raise PanelError("«zona» debe ser Europe/Madrid en esta versión del panel")
    try:
        zone = ZoneInfo(zona)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise PanelError(f"Zona horaria desconocida en «zona»: {zona!r}") from exc
    files = obsidian.get("ficheros", ["00-Inicio/Tareas.md"])
    if not isinstance(files, list) or not all(isinstance(file, str) for file in files):
        raise PanelError("«obsidian.ficheros» debe ser una lista de rutas .md")
    from .sources.obsidian import valid_file
    if not files or len(set(files)) != len(files) or not all(valid_file(file) for file in files):
        raise PanelError("«obsidian.ficheros» contiene rutas no válidas o repetidas")
    return Settings(times=times, zone=zone, channel=_text(data, "canal", "aviso", ""),
                    vault=Path(_text(obsidian, "carpeta", "/vault", "obsidian.")), files=tuple(files),
                    revision_time=revision_time, photo_time=photo_time, study_prefix=prefix.strip())
