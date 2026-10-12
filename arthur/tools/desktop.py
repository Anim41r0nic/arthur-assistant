"""Escritorio: notificaciones, volumen, portapapeles y abrir enlaces."""

import re
import urllib.parse

from .base import Param, Risk, Tool, ToolDenied, ToolError
from .programs import Programs

SINK = "@DEFAULT_AUDIO_SINK@"  # la salida de audio activa (altavoces o auriculares)
MAX_CLIPBOARD_CHARS = 4_000
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f\s]")


def build_desktop_tools(programs: Programs) -> list[Tool]:
    # --- notificaciones ---------------------------------------------------
    def send_notification(args: dict) -> str:
        # "--" marca el fin de las opciones: un título que empiece por "-" no se toma como opción.
        programs.run(["notify-send", "--app-name=ARTHUR", "--", args["title"], args["body"]])
        return "Notificación enviada"

    # --- volumen ----------------------------------------------------------
    def get_volume(_args: dict) -> str:
        # wpctl responde algo como "Volume: 0.40 [MUTED]"
        out = programs.run(["wpctl", "get-volume", SINK]).strip()
        match = re.search(r"Volume:\s*([0-9.]+)", out)
        if not match:
            raise ToolError(f"No entiendo la respuesta de wpctl: {out!r}")
        percent = round(float(match[1]) * 100)
        return f"Volumen al {percent} %" + (" (silenciado)" if "MUTED" in out else "")

    def set_volume(args: dict) -> str:
        if args["percent"] is None and args["muted"] is None:
            return get_volume(args)  # sin nada que cambiar: solo consulta
        done = []
        if args["percent"] is not None:
            programs.run(["wpctl", "set-volume", SINK, f"{args['percent'] / 100:.2f}"])
            done.append(f"volumen al {args['percent']} %")
        if args["muted"] is not None:
            programs.run(["wpctl", "set-mute", SINK, "1" if args["muted"] else "0"])
            done.append("sonido silenciado" if args["muted"] else "sonido activado")
        return "Hecho: " + ", ".join(done)

    # --- portapapeles -----------------------------------------------------
    def read_clipboard(_args: dict) -> str:
        try:
            text = programs.run(["wl-paste", "--no-newline", "--type", "text"], timeout=5)
        except ToolError:
            return "El portapapeles está vacío o no contiene texto"
        if len(text) > MAX_CLIPBOARD_CHARS:
            text = text[:MAX_CLIPBOARD_CHARS] + f"\n… (recortado a {MAX_CLIPBOARD_CHARS} caracteres)"
        return text or "El portapapeles está vacío"

    def write_clipboard(args: dict) -> str:
        programs.feed(["wl-copy"], args["text"])
        return f"Copiados {len(args['text'])} caracteres al portapapeles"

    # --- enlaces ----------------------------------------------------------
    def check_url(args: dict) -> None:
        url = args["url"]
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:  # p. ej. «http://[::1»: Python no la entiende como dirección
            raise ToolDenied("Ese enlace no es una dirección web válida") from None
        # Solo webs normales: nada de file://, javascript:, apt:, ni enlaces que abran otros programas.
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ToolDenied("Solo abro enlaces web que empiecen por http:// o https://")
        if CONTROL_CHARS.search(url):
            raise ToolDenied("El enlace contiene espacios o caracteres de control")

    def open_url(args: dict) -> str:
        check_url(args)
        programs.launch(["xdg-open", args["url"]])
        return f"He abierto {args['url']} en el navegador"

    return [
        Tool(
            name="send_notification",
            description="Muestra una notificación de escritorio.",
            risk=Risk.LOW,
            params=(
                Param("title", str, "", max_length=100),
                Param("body", str, "", required=False, default="", max_length=1000),
            ),
            run=send_notification,
        ),
        Tool(
            name="volume",
            description="Consulta el volumen (sin argumentos) o lo cambia (0-100) y/o silencia.",
            risk=Risk.LOW,
            params=(
                Param("percent", int, "", required=False, minimum=0, maximum=100),
                Param("muted", bool, "", required=False),
            ),
            summarize=lambda a: f"Cambiar el volumen: {a}",
            run=set_volume,
        ),
        Tool(
            name="read_clipboard",
            description="Lee el portapapeles (solo si el usuario lo pide).",
            risk=Risk.PRIVATE,
            summarize=lambda a: "Leer el contenido del portapapeles (puede contener contraseñas u otros datos privados)",
            run=read_clipboard,
        ),
        Tool(
            name="write_clipboard",
            description="Copia un texto al portapapeles.",
            risk=Risk.WRITE,
            params=(Param("text", str, "", max_length=MAX_CLIPBOARD_CHARS),),
            summarize=lambda a: f"Copiar al portapapeles {len(a['text'])} caracteres: «{a['text'][:60]}»",
            run=write_clipboard,
        ),
        Tool(
            name="open_url",
            description="Abre una web (http/https) en el navegador.",
            risk=Risk.WRITE,
            params=(Param("url", str, "", max_length=2048),),
            check=check_url,
            summarize=lambda a: f"Abrir en el navegador: {a['url']}",
            run=open_url,
        ),
    ]
