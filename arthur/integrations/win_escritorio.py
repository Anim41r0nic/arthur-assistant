"""El escritorio de Windows para el aparato de ARTHUR (docs/windows.md, fase A). Solo biblioteca estándar.

- Teclas multimedia (volumen, play/pausa, siguiente…) con SendInput de user32, por ctypes: es lo mismo que pulsar
  las teclas del teclado, así que funciona con Spotify, el navegador o cualquier reproductor, sin instalar nada.
- Apps del menú Inicio: una lista CERRADA con los accesos directos (.lnk) que hay en las carpetas del menú Inicio.
  El modelo solo puede elegir un NOMBRE de esa lista; nunca llega texto suyo a un shell ni a una ruta.
- Notificaciones de Windows (también la de confirmar, con botones Sí/No) con PowerShell y Windows.UI.Notifications.
  El programa de PowerShell es FIJO: el texto del aviso le llega por la entrada estándar (en base64) y entra en la
  notificación con CreateTextNode, que lo escapa. Nunca se pega texto dentro del código.

Todo lo que toca Windows de verdad llega desde fuera (`send`, `launch`, `run`) para que los tests usen falsos.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"

# --- teclas multimedia ------------------------------------------------------------------------------------------
# Tamaños FIJOS de Windows (ver voice/win_audio.py): en Linux c_ulong mide 8 y en Windows 4.
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
LONG = ctypes.c_int32
ULONG_PTR = ctypes.c_size_t

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
MEDIA_KEYS = {VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP, VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK,
              VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE}
VOLUME_STEP = 2          # cada pulsación de volumen en Windows sube o baja un 2 %
MAX_PRESSES = 60         # más que suficiente para ir de 100 % a 0 % (50); un tope por si acaso


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD), ("time", DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT es el más grande: tiene que estar para que INPUT mida lo mismo que en Windows (SendInput lo comprueba).
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


def key_events(vk: int) -> list[INPUT]:
    """Pulsar y soltar una tecla multimedia (son «teclas extendidas»)."""
    down = INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_EXTENDEDKEY)))
    up = INPUT(type=INPUT_KEYBOARD,
               u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)))
    return [down, up]


def _user32_send(events: list[INPUT]) -> int:
    user32 = ctypes.WinDLL("user32")  # solo existe en Windows
    user32.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint32
    array = (INPUT * len(events))(*events)
    return user32.SendInput(len(events), array, ctypes.sizeof(INPUT))


class MediaKeysError(Exception):
    pass


class MediaKeys:
    """Pulsa teclas multimedia. Solo las de MEDIA_KEYS: ninguna otra tecla se puede mandar por aquí."""

    def __init__(self, send: Callable[[list[INPUT]], int] | None = None) -> None:
        """`send` solo se cambia en los tests (un teclado falso: el volumen de verdad no se toca)."""
        self._send = send or _user32_send

    def press(self, vk: int, times: int = 1) -> None:
        if vk not in MEDIA_KEYS:
            raise MediaKeysError(f"Tecla no permitida: {vk:#x}")
        times = max(0, min(MAX_PRESSES, times))
        if not times:
            return
        events = [e for _ in range(times) for e in key_events(vk)]
        sent = self._send(events)
        if sent != len(events):
            # SendInput devuelve cuántas ha mandado: 0 si Windows lo bloquea (p. ej. con la pantalla bloqueada).
            raise MediaKeysError("Windows no me ha dejado pulsar las teclas multimedia")

    def set_volume(self, percent: int) -> None:
        """Windows no deja leer ni poner el volumen exacto sin COM: se baja a 0 y se sube a pasos del 2 %."""
        self.press(VK_VOLUME_DOWN, 100 // VOLUME_STEP)
        self.press(VK_VOLUME_UP, round(max(0, min(100, percent)) / VOLUME_STEP))


# --- apps del menú Inicio ------------------------------------------------------------------------------------------
MAX_SHORTCUTS = 500
MAX_DEPTH = 4
# Ni desinstaladores ni herramientas de administración: abrir una app no debe poder romper nada.
BLOCKED_WORDS = ("uninstall", "desinstal", "unins", "regedit", "administrative tools", "herramientas administrativas",
                 "windows tools", "herramientas de windows", "recovery", "recuperaci", "reset", "restablecer")
# La carpeta «Inicio» (Startup) está dentro del menú Inicio, pero no son apps: es lo que arranca solo al entrar
# (entre otras cosas, el propio servicio de ARTHUR). Abrirlo por voz no tiene sentido y podría duplicar ARTHUR.
STARTUP_FOLDER = "startup"


def start_menu_dirs() -> list[Path]:
    """Las dos carpetas del menú Inicio: la tuya y la de todos los usuarios del PC."""
    dirs = []
    for var in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(var)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def app_id(name: str) -> str:
    """«Google Chrome» → «google-chrome»: un identificador estable, sin acentos ni símbolos."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:60]


@dataclass(frozen=True)
class StartApp:
    id: str
    name: str
    path: Path  # el .lnk: sale SIEMPRE de la búsqueda en el menú Inicio, nunca de lo que diga el modelo


class StartMenuCatalog:
    """La lista cerrada de apps que se pueden abrir: los accesos directos del menú Inicio."""

    def __init__(self, roots: list[Path] | None = None) -> None:
        self.roots = roots if roots is not None else start_menu_dirs()

    def apps(self) -> list[StartApp]:
        found: dict[str, StartApp] = {}
        for root in self.roots:
            for path in self._walk(root, 0):
                name = path.stem
                # TODAS las carpetas desde la raíz, no solo la última: un acceso en una subcarpeta de «Administrative
                # Tools» sigue siendo una herramienta de administración.
                folders = [part.lower() for part in path.relative_to(root).parts[:-1]]
                if folders and folders[0] == STARTUP_FOLDER:
                    continue
                lowered = " ".join([*folders, name.lower()])
                if any(word in lowered for word in BLOCKED_WORDS):
                    continue
                ident = app_id(name)
                if ident and ident not in found:  # la del usuario manda sobre la de todos (va primero)
                    found[ident] = StartApp(ident, name, path)
            if len(found) >= MAX_SHORTCUTS:
                break
        return sorted(found.values(), key=lambda a: a.name.lower())

    def _walk(self, folder: Path, depth: int):
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue  # nada que lleve fuera del menú Inicio
            if entry.is_dir() and depth < MAX_DEPTH:
                yield from self._walk(entry, depth + 1)
            elif entry.is_file() and entry.suffix.lower() == ".lnk":
                yield entry

    def find(self, name: str) -> StartApp | None:
        """Por id exacto o, si solo hay UNA que lo contiene, esa."""
        wanted = app_id(name)
        if not wanted:
            return None
        apps = self.apps()
        for app in apps:
            if app.id == wanted:
                return app
        matches = [a for a in apps if wanted in a.id]
        return matches[0] if len(matches) == 1 else None


def start_file(path: Path) -> None:
    """Abre el acceso directo como si le hicieras doble clic (ShellExecute). Sin shell ni argumentos."""
    os.startfile(path)  # solo existe en Windows


# --- notificaciones (PowerShell + Windows.UI.Notifications) ---------------------------------------------------------
# Siempre Windows PowerShell 5.1 (viene en todos los Windows 10/11): PowerShell 7 ya no carga las clases WinRT.
POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
# El «remitente» de las notificaciones: el propio PowerShell (Windows exige uno registrado; este viene de serie).
POWERSHELL_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
CREATE_NO_WINDOW = 0x08000000  # que no parpadee una ventana negra
MAX_SUMMARY = 300

# Las plantillas son FIJAS. Los textos (título y resumen) se añaden después como nodos de texto (CreateTextNode),
# que escapan solos los < > & " del texto: un resumen con XML o comillas no puede cambiar la notificación.
# El script va en ASCII puro (S&#237; = Sí): así no depende de la codificación de la consola.
_COMMON = r"""
$ErrorActionPreference = 'Stop'
function Leer { $l = [Console]::In.ReadLine(); if (-not $l) { return '' }; [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($l)) }
$titulo = Leer
$texto = Leer
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($plantilla)
$nodos = $xml.GetElementsByTagName('text')
$null = $nodos.Item(0).AppendChild($xml.CreateTextNode($titulo))
$null = $nodos.Item(1).AppendChild($xml.CreateTextNode($texto))
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$notificador = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__APP_ID__')
"""

NOTIFY_SCRIPT = (r"""
$plantilla = '<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>'
""" + _COMMON + r"""
$notificador.Show($toast)
""").replace("__APP_ID__", POWERSHELL_APP_ID)

CONFIRM_SCRIPT = (r"""
param([int]$Segundos = 55)
$plantilla = '<toast scenario="reminder"><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual><actions><action content="No" arguments="no" activationType="foreground"/><action content="S&#237;" arguments="si" activationType="foreground"/></actions></toast>'
""" + _COMMON + r"""
$null = Register-ObjectEvent -InputObject $toast -EventName Activated -SourceIdentifier arthur_pulsado
$null = Register-ObjectEvent -InputObject $toast -EventName Dismissed -SourceIdentifier arthur_cerrado
$null = Register-ObjectEvent -InputObject $toast -EventName Failed -SourceIdentifier arthur_fallo
$notificador.Show($toast)
$respuesta = 'no'
$evento = Wait-Event -Timeout $Segundos
if ($evento -and $evento.SourceIdentifier -eq 'arthur_pulsado') {
    try {
        $datos = [Windows.UI.Notifications.ToastActivatedEventArgs]($evento.SourceArgs[1])
        if ($datos.Arguments -eq 'si') { $respuesta = 'si' }
    } catch { $respuesta = 'no' }
}
if (-not $evento) { try { $notificador.Hide($toast) } catch { } }
[Console]::Out.WriteLine($respuesta)
""").replace("__APP_ID__", POWERSHELL_APP_ID)


def encoded_command(script: str) -> str:
    """-EncodedCommand: el script entero en base64 (UTF-16LE), sin comillas que se puedan romper por el camino."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def confirm_script(seconds: int) -> str:
    """-EncodedCommand no admite parámetros: el script va en un bloque que se llama con los segundos (un número)."""
    return "& {" + CONFIRM_SCRIPT + "} -Segundos " + str(int(seconds))


def clean_text(text: str, limit: int) -> str:
    """Sin caracteres de control (saltos de línea incluidos) y con un tope de largo."""
    return "".join(ch if ch.isprintable() else " " for ch in text)[:limit].strip()


def toast_input(title: str, text: str) -> str:
    """Lo que se le pasa al script por la entrada estándar: una línea en base64 por texto (UTF-8). Así ni la
    codificación de la consola ni las comillas o saltos de línea del texto le afectan."""
    def encode(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"{encode(clean_text(title, 100))}\n{encode(clean_text(text, MAX_SUMMARY))}\n"


def _run_powershell(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, check=False,
                          creationflags=CREATE_NO_WINDOW if WINDOWS else 0)


def windows_notify(text: str, title: str = "ARTHUR", *, run=None) -> None:
    """Aviso breve de Windows. Si falla, no pasa nada (como desktop_notify en Linux)."""
    run = run or _run_powershell
    try:
        run([*POWERSHELL, "-EncodedCommand", encoded_command(NOTIFY_SCRIPT)], toast_input(title, text), 20)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("No he podido mostrar la notificación: %s", e)


def windows_confirm(summary: str, *, timeout: float = 55, run=None) -> bool:
    """Confirmación con una notificación de Windows con botones «No» y «Sí». Solo el botón «Sí» confirma;
    cerrarla, no contestar a tiempo o cualquier fallo (sin PowerShell, notificaciones desactivadas…) es NO."""
    run = run or _run_powershell
    seconds = max(5, int(timeout))
    try:
        proc = run([*POWERSHELL, "-EncodedCommand", encoded_command(confirm_script(seconds))],
                   toast_input("ARTHUR · ¿Confirmas?", summary), seconds + 10)
    except subprocess.TimeoutExpired:
        return False
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("No puedo preguntar la confirmación (%s): cuenta como NO", e)
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "si"
