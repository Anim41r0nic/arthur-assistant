"""Eventos de hoy (Calendar) y correos importantes sin leer (Gmail), solo lectura.

Cualquier fallo de Google o de red se convierte en SourceError con un texto corto: el panel enseña
«no disponible» en esa tarjeta y sigue con el resto, sin volcar detalles técnicos a la pantalla.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from arthur.integrations.google import CALENDAR_SCOPE, GMAIL_SCOPE, GoogleError, day_range

from . import SourceError

PANEL_SCOPES = [CALENDAR_SCOPE, GMAIL_SCOPE]
GMAIL_QUERY = "is:important is:unread newer_than:2d"
MAX_MAILS = 10
_MADRID = ZoneInfo("Europe/Madrid")


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime
    end: datetime
    all_day: bool


@dataclass(frozen=True)
class Mail:
    id: str
    sender: str
    subject: str
    link: str
    snippet: str = ""


def _momento(campo: dict) -> tuple[datetime, bool]:
    """(instante, día_entero). Los de día entero traen solo fecha: se fijan a las 00:00 de Madrid."""
    if campo.get("dateTime"):
        return datetime.fromisoformat(campo["dateTime"]), False
    return datetime.combine(date.fromisoformat(campo["date"]), time.min, tzinfo=_MADRID), True


def today_events(calendar, day: date) -> list[Event]:
    start, end = day_range(day)
    try:
        crudos = calendar.events(start, end)
    except (GoogleError, OSError) as e:
        raise SourceError("Calendario no disponible ahora") from e
    eventos = []
    try:
        for crudo in crudos:
            # En día entero Google da el final exclusivo (el día siguiente), justo lo que queremos.
            inicio, dia_entero = _momento(crudo["start"])
            fin, _ = _momento(crudo["end"])
            eventos.append(Event(crudo.get("summary") or "(sin título)", inicio, fin, dia_entero))
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        raise SourceError("Calendario no disponible ahora") from e
    return sorted(eventos, key=lambda e: e.start)


def _remitente(cabecera: str) -> str:
    nombre, direccion = parseaddr(cabecera)
    return nombre or direccion or cabecera


def important_mail(gmail) -> list[Mail]:
    try:
        crudos = gmail.list(GMAIL_QUERY, MAX_MAILS)
    except (GoogleError, OSError) as e:
        raise SourceError("Correo no disponible ahora") from e
    try:
        return [Mail(c["id"], _remitente(c.get("from", "")), c.get("subject") or "(sin asunto)",
                     f"https://mail.google.com/mail/u/0/#inbox/{c['id']}",
                     " ".join((c.get("snippet") or "").split())[:200]) for c in crudos]
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        raise SourceError("Correo no disponible ahora") from e
