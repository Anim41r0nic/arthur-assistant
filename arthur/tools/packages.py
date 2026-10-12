"""Buscar e instalar programas de los repositorios oficiales.

ARTHUR NO instala nada por su cuenta: abre una terminal con la orden
`sudo pacman -S <paquete>` escrita, y TÚ pones la contraseña y confirmas.
Solo repositorios oficiales: el AUR (scripts de terceros sin revisar) queda fuera.
"""

import re
import shutil

from .base import Param, Risk, Tool, ToolError
from .programs import Programs

# \Z y no $: «$» también acepta un salto de línea al final. Un nombre de paquete de Arch nunca empieza por «-» ni por «.»
# (así «--noconfirm» o «-r» no se pueden confundir con una opción de pacman).
PACKAGE = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]{0,79}\Z")
QUERY = re.compile(r"^[A-Za-z0-9 ._+-]{1,50}\Z")
MAX_RESULTS = 15
INFO_FIELDS = ("Repository", "Name", "Version", "Description", "URL", "Licenses", "Depends On",
               "Download Size", "Installed Size")


def parse_search(output: str) -> list[dict[str, str]]:
    results, seen = [], set()
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(" ") or "/" not in line:
            continue
        repo_name, _, rest = line.partition(" ")
        repo, name = repo_name.split("/", 1)
        if name in seen:
            continue  # el mismo paquete en otro repositorio (p. ej. versión optimizada de CachyOS)
        seen.add(name)
        description = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1].startswith(" ") else ""
        results.append({"repo": repo, "name": name, "version": rest.split(" ")[0],
                        "installed": "[installed" in rest, "description": description})
    return results


def parse_info(output: str) -> dict[str, str]:
    info, key = {}, None
    for line in output.splitlines():
        if not line.strip():
            break  # solo el primer bloque (el primer repositorio)
        if line.startswith(" ") and key:
            info[key] += "; " + line.strip()
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        info[key] = value.strip()
    return info


def terminal() -> list[str]:
    for name in ("alacritty", "kitty"):
        if shutil.which(name):
            return [name]
    raise ToolError("No encuentro ninguna terminal (alacritty o kitty) para abrir la instalación")


def build_package_tools(programs: Programs) -> list[Tool]:
    def pacman(*args: str) -> str:
        return programs.run(["env", "LANG=C", "pacman", *args], timeout=30)

    def check_name(name: str) -> None:
        if not PACKAGE.match(name):
            raise ToolError(f"Nombre de paquete no válido: {name!r}")

    def info(name: str) -> dict[str, str]:
        check_name(name)
        try:
            return parse_info(pacman("-Si", "--", name))  # «--»: lo que sigue es un nombre, nunca una opción
        except ToolError:
            raise ToolError(f"«{name}» no está en los repositorios oficiales. Si solo existe en el AUR, "
                            "ARTHUR no lo instala: son scripts de terceros sin revisar.") from None

    def installed(name: str) -> bool:
        try:
            pacman("-Q", "--", name)
            return True
        except ToolError:
            return False

    # --- consultas ----------------------------------------------------------
    def search_packages(args: dict) -> str:
        if not QUERY.match(args["query"]):
            raise ToolError("Busca con palabras sencillas (letras, números, espacios)")
        try:
            results = parse_search(pacman("-Ss", "--", *args["query"].split()))
        except ToolError:
            return f"No hay paquetes en los repositorios oficiales que encajen con «{args['query']}»"
        # No marcamos aquí lo instalado: confundía al modelo (libvlc instalado ≠ vlc instalado).
        lines = [f"{r['name']} {r['version']} — {r['description']}" for r in results[:MAX_RESULTS]]
        exact = [r["name"] for r in results if r["name"].lower() == args["query"].strip().lower()]
        if exact:  # el modelo pequeño a veces no lo veía entre los demás resultados
            lines.insert(0, f"Coincidencia exacta: {exact[0]} (para instalarlo: install_package con name={exact[0]})")
        if len(results) > MAX_RESULTS:
            lines.append(f"… y {len(results) - MAX_RESULTS} más; afina la búsqueda")
        lines.append("(Para instalar uno, llama a install_package con su nombre exacto: "
                     "comprueba él mismo si ya está instalado.)")
        return "\n".join(lines)

    def package_info(args: dict) -> str:
        data = info(args["name"])
        lines = [f"{k}: {data[k]}" for k in INFO_FIELDS if k in data]
        lines.append("Estado: ya instalado" if installed(args["name"]) else "Estado: no instalado")
        return "\n".join(lines)

    # --- instalar (lo haces tú en la terminal) -------------------------------
    def check_install(args: dict) -> None:
        info(args["name"])  # tiene que existir en los repositorios oficiales
        if installed(args["name"]):
            raise ToolError(f"{args['name']} ya está instalado")

    def install_package(args: dict) -> str:
        name = args["name"]
        check_install(args)
        # El nombre va como argumento aparte ("$1"), nunca pegado dentro del texto de la orden.
        script = ('echo "ARTHUR te ha preparado la instalación de: $1"; echo; '
                  'sudo pacman -S -- "$1"; echo; read -rp "Pulsa Enter para cerrar esta ventana" _')
        programs.launch(["uwsm", "app", "--", *terminal(), "--title", f"ARTHUR · instalar {name}",
                         "-e", "bash", "-c", script, "arthur-instalar", name])
        return (f"Te he abierto una terminal para instalar {name}. Pon tu contraseña y confirma con S; "
                "yo no puedo instalar nada sin ti.")

    return [
        Tool(
            name="search_packages",
            description="Busca programas en los repositorios oficiales. Busca EN INGLÉS (p. ej. video player).",
            risk=Risk.READ,
            params=(Param("query", str, "", max_length=50),),
            run=search_packages,
        ),
        Tool(
            name="install_package",
            description="Prepara la instalación de un paquete (el usuario confirma con su contraseña). Comprueba si ya está instalado.",
            risk=Risk.LOW,
            params=(Param("name", str, "nombre exacto", max_length=80),),
            check=check_install,
            run=install_package,
        ),
    ]
