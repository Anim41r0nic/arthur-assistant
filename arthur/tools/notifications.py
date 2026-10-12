"""Herramienta para preguntar por los mensajes que te han llegado (notificaciones)."""

import time

from ..integrations.notifications import NotificationWatcher
from .base import Param, Risk, Tool, ToolError

SOURCE_LABELS = {"whatsapp": "WhatsApp", "gmail": "Gmail", "web": "una web (Firefox)", "otra": "otra app"}


def ago(timestamp: float) -> str:
    minutes = int((time.time() - timestamp) // 60)
    if minutes < 1:
        return "ahora mismo"
    if minutes < 60:
        return f"hace {minutes} min"
    return f"hace {minutes // 60} h {minutes % 60} min"


def build_notification_tools(watcher: NotificationWatcher) -> list[Tool]:
    def recent_messages(args: dict) -> str:
        source = None if args["source"] == "todas" else args["source"]
        items = watcher.latest(source, limit=15)
        if not items:
            return "No me ha llegado ningún aviso" + (f" de {SOURCE_LABELS.get(source, source)}" if source else "") + \
                " desde que estoy en marcha"
        lines = []
        for n in reversed(items):
            line = f"{n.summary or n.app} ({SOURCE_LABELS[n.source]}, {ago(n.received)})"
            if args["include_text"] and n.body:
                text = n.body.replace("\n\nweb.whatsapp.com", "").strip()
                line += f": «{text[:300]}»"
            lines.append(line)
        return "\n".join(lines)

    return [
        Tool(
            name="recent_messages",
            # «Si no dice de dónde, todas»: el valor por defecto no viaja al servidor (fase S7) y el modelo, sin
            # saberlo, preguntaba «¿WhatsApp, Gmail o todas?» (prueba real, 0.57.0).
            description="Quién le ha escrito (avisos del escritorio: WhatsApp Web…). Si no dice de dónde, todas.",
            risk=Risk.READ,
            params=(
                Param("source", str, "whatsapp, gmail, web o todas (si no lo dice: todas)", required=False, default="todas", max_length=10),
                Param("include_text", bool, "también el texto (privado)", required=False, default=False),
            ),
            check=_check_source,
            risk_for=lambda a: Risk.PRIVATE if a["include_text"] else Risk.READ,
            summarize=lambda a: "Leer el texto de tus mensajes recientes (es privado)",
            run=recent_messages,
        ),
    ]


def _check_source(args: dict) -> None:
    if args["source"] not in ("whatsapp", "gmail", "web", "todas"):
        raise ToolError("source debe ser whatsapp, gmail, web o todas")
