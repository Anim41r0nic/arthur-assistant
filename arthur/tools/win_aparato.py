"""Las «manos» de un aparato Windows (perfil "windows"; docs/windows.md, fase A).

Mismos nombres que en el aparato Linux (`list_applications`, `open_application`, `volume`, `media`): así el ARTHUR
del servidor las usa igual, venga la petición del portátil o del PC con Windows.

Nada PELIGROSO: ni órdenes, ni instalar, ni ficheros. Solo:
  - abrir apps del menú Inicio (una lista CERRADA de accesos directos; el modelo elige un nombre, nunca una ruta);
  - volumen y música con las teclas multimedia (lo mismo que pulsarlas en el teclado).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..integrations.win_escritorio import (
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV_TRACK,
    VK_MEDIA_STOP,
    VK_VOLUME_DOWN,
    VK_VOLUME_MUTE,
    VK_VOLUME_UP,
    MediaKeys,
    MediaKeysError,
    StartMenuCatalog,
    start_file,
)
from .base import Param, Risk, Tool, ToolDenied, ToolError

MAX_LISTED = 40
# Las teclas multimedia de Windows no distinguen play de pausa: las dos son «play/pausa».
MEDIA_ACTIONS = {
    "play": (VK_MEDIA_PLAY_PAUSE, 1, "He pulsado play/pausa"),
    "pause": (VK_MEDIA_PLAY_PAUSE, 1, "He pulsado play/pausa"),
    "toggle": (VK_MEDIA_PLAY_PAUSE, 1, "He pulsado play/pausa"),
    "next": (VK_MEDIA_NEXT_TRACK, 1, "Siguiente canción"),
    "previous": (VK_MEDIA_PREV_TRACK, 1, "Canción anterior"),
    "stop": (VK_MEDIA_STOP, 1, "Parado"),
    "volume_up": (VK_VOLUME_UP, 5, "Volumen subido"),        # 5 pulsaciones = +10 %
    "volume_down": (VK_VOLUME_DOWN, 5, "Volumen bajado"),
}


def build_windows_tools(catalog: StartMenuCatalog, keys: MediaKeys, trusted_apps=(), *,
                        launch: Callable[[Path], None] = start_file) -> list[Tool]:
    """`catalog`, `keys` y `launch` se cambian en los tests por falsos: nunca tocan el escritorio real."""
    trusted = set(trusted_apps)

    def resolve(name: str):
        app = catalog.find(name)
        if app is None:
            raise ToolDenied(f"No encuentro «{name}» en el menú Inicio (prueba con list_applications)")
        return app

    # --- apps -----------------------------------------------------------------------------------------------------
    def list_applications(args: dict) -> str:
        query = (args["query"] or "").strip().lower()
        apps = [a for a in catalog.apps() if not query or query in a.name.lower() or query in a.id]
        if not apps:
            return "No encuentro ninguna app así en el menú Inicio"
        lines = [f"{a.name} [{a.id}]" for a in apps[:MAX_LISTED]]
        if len(apps) > MAX_LISTED:
            lines.append(f"… y {len(apps) - MAX_LISTED} más; afina la búsqueda")
        return "\n".join(lines)

    def open_risk(args: dict) -> Risk:
        app = catalog.find(args["name"])
        return Risk.LOW if app is not None and app.id in trusted else Risk.WRITE

    def open_application(args: dict) -> str:
        app = resolve(args["name"])
        try:
            launch(app.path)
        except OSError as e:
            raise ToolError(f"No he podido abrir {app.name}: {e}") from e
        return f"He abierto {app.name}"

    # --- volumen y música -------------------------------------------------------------------------------------------
    def press(vk: int, times: int = 1) -> None:
        try:
            keys.press(vk, times)
        except MediaKeysError as e:
            raise ToolError(str(e)) from e

    def volume(args: dict) -> str:
        percent, muted = args["percent"], args["muted"]
        if percent is None and muted is None:
            # Sin COM no se puede leer el volumen: se dice con honestidad en vez de inventarlo.
            return "En Windows no puedo leer el volumen; mira el icono del altavoz. Sí puedo cambiarlo (0-100)."
        done = []
        if percent is not None:
            try:
                keys.set_volume(percent)
            except MediaKeysError as e:
                raise ToolError(str(e)) from e
            done.append(f"volumen al {percent} % (aprox.)")
        if muted is True:
            press(VK_VOLUME_MUTE)  # la tecla de silencio va y viene: si ya estaba en silencio, lo quita
            done.append("he pulsado silenciar")
        elif muted is False and percent is None:
            press(VK_VOLUME_UP)    # subir el volumen siempre quita el silencio en Windows…
            press(VK_VOLUME_DOWN)  # …y bajarlo después lo deja como estaba
            done.append("sonido activado")
        return "Hecho: " + ", ".join(done)

    def check_media(args: dict) -> None:
        if args["action"] not in MEDIA_ACTIONS:
            raise ToolDenied(f"Acción desconocida: {args['action']!r} (usa {', '.join(MEDIA_ACTIONS)})")

    def media(args: dict) -> str:
        vk, times, done = MEDIA_ACTIONS[args["action"]]
        press(vk, times)
        return done

    return [
        Tool(
            name="list_applications",
            description="Busca apps del menú Inicio por nombre.",
            risk=Risk.READ,
            params=(Param("query", str, "", required=False, default="", max_length=100),),
            run=list_applications,
        ),
        Tool(
            name="open_application",
            description="Abre una app del menú Inicio.",
            risk=Risk.WRITE,
            params=(Param("name", str, "p. ej. firefox", max_length=100),),
            check=lambda a: resolve(a["name"]) and None,
            risk_for=open_risk,
            summarize=lambda a: f"Abrir la aplicación {resolve(a['name']).name}",
            run=open_application,
        ),
        Tool(
            name="volume",
            description="Cambia el volumen (0-100) y/o silencia.",
            risk=Risk.LOW,
            params=(
                Param("percent", int, "", required=False, minimum=0, maximum=100),
                Param("muted", bool, "", required=False),
            ),
            summarize=lambda a: f"Cambiar el volumen: {a}",
            run=volume,
        ),
        Tool(
            name="media",
            description="Música/vídeo que suena: play, pause, toggle, next, previous, stop, "
                        "volume_up/volume_down (teclas multimedia).",
            risk=Risk.LOW,
            params=(Param("action", str, "", max_length=12),),
            check=check_media,
            run=media,
        ),
    ]
