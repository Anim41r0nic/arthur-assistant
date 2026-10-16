"""Atajo de teclado de ARTHUR en Windows: alterna escuchar / parar (como scripts/arthur-ptt en Linux).

El instalador (scripts/windows/instalar-aparato.ps1) crea en el menú Inicio un acceso directo «ARTHUR - hablar» con
el atajo Ctrl+Alt+A que ejecuta esto con pythonw (sin ventana). Solo envía la orden fija "toggle-listen" al buzón
privado de ARTHUR (%LOCALAPPDATA%\\arthur\\control.key). Si ARTHUR no está en marcha, lo avisa con una notificación.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from arthur.api.control import ControlUnavailable, send_command  # noqa: E402
from arthur.integrations.win_escritorio import windows_notify  # noqa: E402

try:
    send_command("toggle-listen")
except ControlUnavailable as e:
    windows_notify(f"{e}. Se arranca solo al iniciar sesión (o vuelve a ejecutar el instalador).")
    sys.exit(1)
