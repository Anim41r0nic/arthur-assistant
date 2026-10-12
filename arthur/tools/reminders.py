"""Recordatorios y temporizadores: «recuérdame en 20 minutos…», «despiértame a las 8».

Se guardan en la base de datos de ARTHUR (sobreviven a reinicios). Cuando llega la hora,
el servicio suena, muestra una notificación y te lo dice (ver core/reminders.py).
"""

import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from ..memory.store import MemoryStore, MemoryStoreError
from .base import Param, Risk, Tool, ToolError

MAX_AHEAD = timedelta(days=366)
HOUR = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


def when_text(due: datetime, now: datetime) -> str:
    """«hoy a las 18:30 (en 20 min)», «mañana a las 8:00», «el 12/10 a las 9:00»."""
    days = (due.date() - now.date()).days
    day = {0: "hoy", 1: "mañana", 2: "pasado mañana"}.get(days, f"el {due:%d/%m}")
    text = f"{day} a las {due:%H:%M}"
    left = due - now
    if timedelta(0) < left < timedelta(hours=6):
        minutes = round(left.total_seconds() / 60)
        text += f" (en {minutes // 60} h {minutes % 60} min)" if minutes >= 60 else f" (en {max(1, minutes)} min)"
    return text


def due_time(args: dict, now: datetime) -> datetime:
    """La hora del aviso a partir de `minutes` (dentro de X minutos) o de `at` + `date` opcional."""
    minutes, at, day = args["minutes"], args["at"].strip(), args["date"].strip()
    if bool(minutes) == bool(at):
        raise ToolError("Indica o bien minutes (dentro de cuántos minutos) o bien at (a qué hora), uno solo")
    if minutes:
        if day:
            raise ToolError("date solo se usa con at")
        due = now + timedelta(minutes=minutes)
    else:
        match = HOUR.match(at)
        if not match:
            raise ToolError(f"Hora no válida: {at!r} (formato HH:MM)")
        clock = time(int(match[1]), int(match[2]))
        if day:
            try:
                due = datetime.combine(date.fromisoformat(day), clock)
            except ValueError:
                raise ToolError(f"Fecha no válida: {day!r} (formato AAAA-MM-DD)") from None
            if due <= now:
                raise ToolError(f"El {due:%d/%m/%Y} a las {due:%H:%M} ya ha pasado")
        else:
            due = datetime.combine(now.date(), clock)
            if due <= now:
                due += timedelta(days=1)  # «a las 8» dicho por la noche = mañana a las 8
    if due - now > MAX_AHEAD:
        raise ToolError("Solo puedo recordarte cosas de aquí a un año como mucho")
    return due


def build_reminder_tools(store: MemoryStore, *, now: Callable[[], datetime] = datetime.now) -> list[Tool]:
    def check(args: dict) -> None:
        due_time(args, now())

    def set_reminder(args: dict) -> str:
        current = now()
        try:
            reminder = store.add_reminder(args["text"], due_time(args, current))
        except MemoryStoreError as e:
            raise ToolError(str(e)) from e
        return f"Recordatorio #{reminder.id} para {when_text(reminder.due_at, current)}: {reminder.text}"

    def list_reminders(args: dict) -> str:
        current = now()
        items = store.list_reminders()
        if not items:
            return "No tienes recordatorios pendientes"
        return "\n".join(f"#{r.id} {when_text(r.due_at, current)}: {r.text}" for r in items)

    def cancel_reminder(args: dict) -> str:
        if not store.delete_reminder(args["id"]):
            raise ToolError(f"No hay ningún recordatorio #{args['id']}")
        return f"He quitado el recordatorio #{args['id']}"

    return [
        Tool(
            name="set_reminder",
            description="Recordatorio o temporizador. Usa minutes (dentro de X min) O at (hora HH:MM; "
                        "date AAAA-MM-DD si no es hoy/mañana).",
            risk=Risk.LOW,
            params=(
                Param("text", str, "qué recordar, p. ej. «sacar la ropa» o «temporizador de 10 minutos»",
                      max_length=200),
                Param("minutes", int, "", required=False, default=0, minimum=0, maximum=525_600),
                Param("at", str, "", required=False, default="", max_length=5),
                Param("date", str, "", required=False, default="", max_length=10),
            ),
            check=check,
            run=set_reminder,
        ),
        Tool(
            name="list_reminders",
            description="Recordatorios y temporizadores pendientes.",
            risk=Risk.READ,
            params=(),
            run=list_reminders,
        ),
        Tool(
            name="cancel_reminder",
            description="Quita un recordatorio por su #id.",
            risk=Risk.LOW,
            params=(Param("id", int, "", minimum=1),),
            run=cancel_reminder,
        ),
    ]
