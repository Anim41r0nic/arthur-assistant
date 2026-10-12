"""Catálogo de aplicaciones instaladas, a partir de sus ficheros .desktop.

Cada app gráfica instala un fichero como /usr/share/applications/firefox.desktop
con su nombre, icono y cómo se lanza. Es lo mismo que usa tu lanzador de apps.
Su identificador es el nombre del fichero sin ".desktop" (p. ej. "firefox").

ARTHUR nunca ejecuta la línea Exec= de estos ficheros: se la pasa a uwsm, que la
lanza como lo hacen tus atajos de teclado. La leemos solo para detectar apps que
piden permisos de administrador.
"""

import configparser
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Las categorías estándar vienen en inglés; añadimos palabras en español para poder
# buscar «programar», «navegador», «juegos»…
CATEGORY_WORDS = {
    "Development": "desarrollo programacion programar codigo",
    "IDE": "entorno de desarrollo programar",
    "TextEditor": "editor de texto",
    "WebBrowser": "navegador internet web",
    "Network": "internet red",
    "Office": "oficina documentos",
    "Graphics": "graficos imagenes dibujo",
    "AudioVideo": "multimedia audio video",
    "Audio": "audio sonido musica",
    "Video": "video peliculas",
    "Game": "juegos",
    "Education": "educacion",
    "Science": "ciencia",
    "Utility": "utilidades",
    "System": "sistema",
    "Settings": "ajustes configuracion",
    "TerminalEmulator": "terminal consola",
    "FileManager": "archivos ficheros explorador",
    "Calculator": "calculadora",
}

PRIVILEGE_PATTERN = re.compile(r"(^|[\s/])(pkexec|sudo|gksu|gksudo|kdesu|doas|run0)(\s|$)")


@dataclass(frozen=True)
class DesktopApp:
    id: str             # "firefox"
    name: str           # "Firefox"
    generic_name: str   # "Navegador web"
    keywords: tuple[str, ...]  # incluye las categorías y su traducción
    privileged: bool    # su Exec usa pkexec/sudo…
    terminal: bool      # se abre dentro de una terminal
    path: Path


def default_dirs() -> list[Path]:
    """Carpetas estándar de .desktop, de más prioritaria a menos (la tuya primero)."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    data_dirs = [Path(d) for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    flatpak = [data_home / "flatpak/exports/share", Path("/var/lib/flatpak/exports/share")]
    return [d / "applications" for d in (data_home, *flatpak, *data_dirs)]


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").strip()


def _localized(section: configparser.SectionProxy, key: str) -> str:
    for variant in (f"{key}[es_ES]", f"{key}[es]", key):
        if section.get(variant):
            return section[variant].strip()
    return ""


def parse_desktop_file(path: Path, app_id: str) -> DesktopApp | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # respetar mayúsculas: "Name" no es "name"
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError, OSError):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    entry = parser["Desktop Entry"]
    if entry.get("Type") != "Application":
        return None
    if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
        return None  # entradas que tu lanzador tampoco muestra
    name = _localized(entry, "Name")
    if not name:
        return None
    return DesktopApp(
        id=app_id,
        name=name,
        generic_name=_localized(entry, "GenericName"),
        keywords=tuple(
            k for k in (
                *_localized(entry, "Keywords").split(";"),
                *(f"{c} {CATEGORY_WORDS.get(c, '')}" for c in entry.get("Categories", "").split(";") if c),
            ) if k
        ),
        privileged=bool(PRIVILEGE_PATTERN.search(entry.get("Exec", ""))),
        terminal=entry.get("Terminal", "").lower() == "true",
        path=path,
    )


class AppCatalog:
    def __init__(self, dirs: Iterable[Path] | None = None) -> None:
        self.dirs = list(dirs) if dirs is not None else default_dirs()

    def load(self) -> dict[str, DesktopApp]:
        """Se relee en cada consulta: así ve las apps que instales sin reiniciar ARTHUR."""
        apps: dict[str, DesktopApp] = {}
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.desktop")):
                app_id = str(path.relative_to(directory).with_suffix("")).replace("/", "-")
                if app_id in apps:
                    continue  # una carpeta más prioritaria ya la definió
                app = parse_desktop_file(path, app_id)
                if app:
                    apps[app_id] = app
        return apps

    def search(self, query: str) -> list[DesktopApp]:
        """Coincidencia exacta (id o nombre) primero; si no hay, coincidencias parciales."""
        apps = self.load().values()
        q = normalize(query)
        if not q:
            return sorted(apps, key=lambda a: normalize(a.name))
        exact = [a for a in apps if q in (normalize(a.id), normalize(a.name))]
        if exact:
            return exact
        partial = [
            a for a in apps
            if any(q in normalize(field) for field in (a.id, a.name, a.generic_name, *a.keywords))
        ]
        return sorted(partial, key=lambda a: normalize(a.name))
