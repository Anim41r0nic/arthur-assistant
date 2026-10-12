"""Escucha las notificaciones de escritorio que ya te llegan (p. ej. de WhatsApp Web).

Las apps envían sus notificaciones al servicio org.freedesktop.Notifications
(en tu escritorio, Noctalia) por D-Bus. `dbus-monitor` (paquete oficial dbus)
nos deja ver esas llamadas sin tocar tus cuentas ni tus apps.

Privacidad:
  - Solo se guardan en RAM las últimas notificaciones (nunca en disco ni en el registro).
  - Por defecto, ARTHUR solo te dice QUIÉN te ha escrito; el texto, solo si lo pides.
"""

import logging
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MATCH_RULE = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
MAX_KEPT = 50
STRING_LINE = re.compile(r'^\s*string "(.*)$')


@dataclass(frozen=True)
class Notification:
    app: str
    summary: str
    body: str
    source: str  # "whatsapp", "gmail" u "otra"
    received: float = field(default_factory=time.time)
    meta: tuple[str, ...] = ()  # datos técnicos (icono, pistas) para diagnosticar; nunca título ni texto


BROWSERS = ("firefox", "chromium", "google chrome", "brave", "librewolf")


def classify(app: str, summary: str, body: str) -> str:
    text = f"{app} {summary} {body}".lower()
    if "whatsapp" in text:
        return "whatsapp"
    if "mail.google.com" in text or "gmail" in text:
        return "gmail"
    # Firefox no dice de qué web viene un aviso (comprobado: solo manda "firefox").
    # En la práctica, en este equipo son los mensajes de WhatsApp Web.
    if app.lower() in BROWSERS:
        return "web"
    return "otra"


def parse_monitor_output(lines) -> list[tuple[str, str, str]]:
    """Convierte la salida de dbus-monitor en (app, título, texto) por cada llamada Notify.

    En cada llamada, los argumentos llegan en orden: app_name (string), replaces_id (uint32),
    app_icon (string), summary (string), body (string), … Los textos pueden ocupar varias líneas.
    """
    results, strings, current = [], None, None
    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("method call ") and "member=Notify" in line:
            if strings is not None and len(strings) >= 4:
                results.append((strings[0], strings[2], strings[3]))
            strings, current = [], None
            continue
        if line.startswith(("method call ", "signal ", "method return ", "error ")):
            if strings is not None and len(strings) >= 4:
                results.append((strings[0], strings[2], strings[3]))
            strings, current = None, None
            continue
        if strings is None:
            continue
        if current is not None:  # continuación de un texto de varias líneas
            if line.endswith('"'):
                strings.append(current + "\n" + line[:-1])
                current = None
            else:
                current += "\n" + line
            continue
        match = STRING_LINE.match(line)
        if match and len(strings) < 4:
            value = match.group(1)
            if value.endswith('"'):
                strings.append(value[:-1])
            else:
                current = value
    if strings is not None and len(strings) >= 4:
        results.append((strings[0], strings[2], strings[3]))
    return results


class NotificationWatcher:
    def __init__(
        self,
        on_notification: Callable[[Notification], None],
        *,
        ignore_apps: tuple[str, ...] = ("ARTHUR",),
        command: list[str] | None = None,
    ) -> None:
        """`command` solo se cambia en los tests."""
        self.on_notification = on_notification
        self.ignore_apps = {a.lower() for a in ignore_apps}
        self.command = command or ["dbus-monitor", "--session", MATCH_RULE]
        self.recent: deque[Notification] = deque(maxlen=MAX_KEPT)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._proc = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL, text=True, errors="replace")
        threading.Thread(target=self._read, name="notifications", daemon=True).start()
        log.info("Escuchando notificaciones de escritorio")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc.stdout.close()

    def _read(self) -> None:
        proc = self._proc
        block: list[str] = []
        for line in proc.stdout:
            # Cada bloque empieza por una cabecera; procesamos el anterior al llegar el siguiente.
            if line.startswith(("method call ", "signal ", "method return ", "error ")) and block:
                self._handle_safely(block)
                block = []
            block.append(line)
            if line.strip() == "int32 -1" or line.strip().startswith("int32 "):  # último argumento de Notify
                self._handle_safely(block)
                block = []
        if block:
            self._handle_safely(block)
        if self._proc is proc:  # stop() lo pone a None: si sigue ahí, dbus-monitor ha terminado por su cuenta
            log.warning("dbus-monitor ha terminado: ya no recibo avisos de mensajes (reinicia ARTHUR para recuperarlos)")

    def _handle_safely(self, block: list[str]) -> None:
        # Una notificación rara no puede matar el hilo: sin esto, el primer fallo dejaba a ARTHUR sin oír
        # ningún aviso más (sin decir nada) hasta reiniciarlo.
        try:
            self._handle(block)
        except Exception as e:
            # Solo el tipo de error en el registro normal: el mensaje de una excepción podría llevar texto de
            # la notificación (privado). El detalle completo, solo con DEBUG.
            log.error("No he podido interpretar una notificación (%s)", type(e).__name__)
            log.debug("Detalle del fallo", exc_info=True)

    def _handle(self, block: list[str]) -> None:
        meta = technical_lines(block)
        for app, summary, body in parse_monitor_output(block):
            if app.lower() in self.ignore_apps:
                continue
            note = Notification(app, summary.strip(), body.strip(), classify(app, summary, body), meta=meta)
            with self._lock:
                self.recent.append(note)
            try:
                self.on_notification(note)
            except Exception:
                log.exception("Fallo procesando una notificación")

    def latest(self, source: str | None = None, limit: int = 10) -> list[Notification]:
        with self._lock:
            items = [n for n in self.recent if source in (None, n.source)]
        return items[-limit:]


def technical_lines(block: list[str]) -> tuple[str, ...]:
    """Icono y "pistas" de la notificación, para diagnosticar de qué web viene.

    NUNCA incluye el título ni el texto: se saltan los cuatro primeros textos de la llamada
    Notify (app, icono, título, texto) completos, aunque ocupen varias líneas, y los datos de imagen.
    """
    out: list[str] = []
    in_notify, strings_done, in_string, skip_image = False, 0, False, False
    for line in block:
        stripped = line.strip()
        if line.startswith(("method call ", "signal ", "method return ", "error ")):
            in_notify = line.startswith("method call ") and "member=Notify" in line
            strings_done, in_string = 0, False
            continue
        if not in_notify:
            continue
        if strings_done < 4:
            if in_string:  # continuación de un texto de varias líneas
                if line.rstrip("\n").endswith('"'):
                    in_string = False
                    strings_done += 1
                continue
            if stripped.startswith('string "'):
                value = stripped[len('string "'):]
                if strings_done == 1 and value.endswith('"'):
                    out.append(f"icono={value[:-1][:120]}")  # el icono (2.º texto) sí sirve
                if value.endswith('"') and len(value) >= 1:
                    strings_done += 1
                else:
                    in_string = True
            continue
        if '"image-data"' in stripped or '"icon_data"' in stripped:
            skip_image = True
        if skip_image:
            if stripped == ")":
                skip_image = False
            continue
        if stripped.startswith(("string ", "variant", "boolean", "int32", "uint32", "byte")):
            out.append(stripped[:120])
    return tuple(out[:40])
