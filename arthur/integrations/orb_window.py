"""El Orb a pantalla completa al decir «Hey Arthur».

Al oír la palabra de activación:
  1. Busca la ventana del Orb (Firefox con el título «ARTHUR»). Si no hay, abre una
     ventana NUEVA de Firefox solo para el Orb y espera a que aparezca.
  2. La trae a tu escritorio actual, le da el foco y la pone a pantalla completa.
Al terminar la conversación la quita de en medio: la manda a un escritorio especial
oculto («special:arthur») y devuelve el foco a la ventana que estabas usando.

Solo usa las plantillas fijas de HyprlandController (nunca Lua con texto libre).
"""

import logging
import time
from collections.abc import Callable

from ..tools.base import ToolError
from .hyprland import ADDRESS, HyprlandActionError, HyprlandReader, HyprlandUnavailable

log = logging.getLogger(__name__)

TITLE = "ARTHUR"
APPEAR_TIMEOUT = 8.0  # segundos máximos esperando a que Firefox abra la ventana


def is_orb_window(client: dict) -> bool:
    title = str(client.get("title", ""))
    return (str(client.get("class", "")).lower() == "firefox"
            and (title == TITLE or title.startswith(f"{TITLE} — ") or title.startswith(f"{TITLE} - ")))


class OrbWindow:
    def __init__(self, controller, reader: HyprlandReader, *, url: Callable[[], str | None],
                 launch: Callable[[list[str]], None], sleep: Callable[[float], None] = time.sleep) -> None:
        self.controller = controller
        self.reader = reader
        self.url = url
        self.launch = launch
        self.sleep = sleep
        self._previous: str | None = None  # la ventana que usabas antes de llamar a ARTHUR

    def find(self) -> str | None:
        for client in self.reader.windows():
            address = str(client.get("address", ""))
            if is_orb_window(client) and ADDRESS.match(address):
                return address
        return None

    def show(self) -> None:
        """Trae el Orb a pantalla completa. Si algo falla, solo se registra: la conversación sigue."""
        try:
            active = self.reader.query("j/activewindow") or {}
            workspace = (self.reader.query("j/activeworkspace") or {}).get("id")
            address = self.find()
            if address is None:
                url = self.url()
                if not url or not url.startswith("http://127.0.0.1:"):
                    log.warning("No abro el Orb: no está activo")
                    return
                self.launch(["firefox", "--new-window", url])
                address = self._wait_for_window()
                if address is None:
                    log.warning("La ventana del Orb no ha aparecido")
                    return
            previous = str(active.get("address", ""))
            if previous != address and ADDRESS.match(previous):
                self._previous = previous
            if isinstance(workspace, int) and 1 <= workspace <= 10:
                self.controller.move_window(address, workspace, follow=True)
            self.controller.focus_window(address)
            self.controller.set_fullscreen(address, True)
        except (OSError, ValueError, HyprlandActionError, HyprlandUnavailable, ToolError) as e:
            log.warning("No he podido mostrar el Orb: %s", e)

    def hide(self) -> None:
        """Quita el Orb de en medio (escritorio oculto) y devuelve el foco a tu ventana."""
        try:
            address = self.find()
            if address:
                self.controller.set_fullscreen(address, False)
                self.controller.hide_window(address)
            previous, self._previous = self._previous, None
            if previous:
                self.controller.focus_window(previous)
        except (OSError, ValueError, HyprlandActionError, HyprlandUnavailable, ToolError) as e:
            log.warning("No he podido ocultar el Orb: %s", e)

    def _wait_for_window(self) -> str | None:
        waited = 0.0
        while waited < APPEAR_TIMEOUT:
            if address := self.find():
                return address
            self.sleep(0.25)
            waited += 0.25
        return None
