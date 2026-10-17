"""Calendario de Google: leer, crear, cambiar y borrar eventos.

«¿qué tengo hoy?» (leer), «apúntame dentista el jueves a las 5» (crear), «pasa el dentista al viernes»
(cambiar) y «borra el dentista» (borrar). Todo lo que escribe pide tu confirmación y nunca invita ni avisa
a nadie. Con un modelo pequeño, las fechas las COMPRUEBA la herramienta (fecha real, no pasada, a menos de
2 años) y en la confirmación se ve el título y el día de la semana, para pillar un error de cálculo.

Cambiar y borrar identifican el evento por el NÚMERO (#1, #2…) que enseña calendar_events (o que devuelve
create_event), nunca por un texto que escriba el modelo, y la herramienta comprueba que existe ANTES de
preguntarte. Un modelo pequeño inventaba ids ("1") o no sabía el id del evento recién creado: ahora la
herramienta guarda la tabla «número → id de Google» de la última lista mostrada y es ella quien traduce. Solo se tocan eventos
de tu calendario principal, sueltos (no repetidos), sin invitados y de tipo normal: lo demás se hace en
Google Calendar.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..brain.rules import MONTHS, WEEKDAYS
from ..integrations.google import EVENT_ID, CalendarClient, GoogleError, NotFound, day_range
from .base import Param, Risk, Tool, ToolError

TIMEZONE = ZoneInfo("Europe/Madrid")
MAX_AHEAD = timedelta(days=730)  # 2 años
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # date.fromisoformat solo aceptaría también «20261009» o «2026-W41-1»
NUMBER = re.compile(r"^#?(\d{1,4})$")  # «#2» o «2»; los ids de Google tienen 5 o más caracteres: no se solapan
CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_day(text: str, today: date) -> date:
    t = text.strip().lower()
    if t in ("", "hoy"):
        return today
    if t in ("mañana", "manana"):
        return today + timedelta(days=1)
    if t in ("pasado mañana", "pasado manana"):
        return today + timedelta(days=2)
    names = [w.replace("é", "e").replace("á", "a") for w in WEEKDAYS]
    plain = t.replace("é", "e").replace("á", "a").removeprefix("el ").removeprefix("este ")
    if plain in names:  # el próximo lunes, martes… (hoy incluido)
        return today + timedelta(days=(names.index(plain) - today.weekday()) % 7)
    try:
        return date.fromisoformat(t)
    except ValueError:
        raise ToolError(f"No entiendo el día «{text}». Usa hoy, mañana, un día de la semana o AAAA-MM-DD") from None


def describe_event(event: dict, number: int | None = None) -> str:
    start, end = event["start"], event["end"]
    if "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"]).astimezone()
        e = datetime.fromisoformat(end["dateTime"]).astimezone()
        when = f"{s:%H:%M}–{e:%H:%M}"
    else:
        when = "todo el día"
    text = f"{when} {event.get('summary') or '(sin título)'}"
    if event.get("location"):
        text += f" · {event['location']}"
    if event.get("_calendar"):
        text += f" [{event['_calendar']}]"
    if number is not None:
        text += f" #{number}"  # solo los que update_event / delete_event pueden tocar
    return text


def why_not_editable(event: dict) -> str:
    """Vacío si ARTHUR puede cambiar o borrar este evento; si no, por qué no (en cristiano)."""
    if event.get("_primary") is False:  # (en un evento suelto leído de «primary» no viene marcado)
        return "no está en tu calendario principal: hazlo en Google Calendar"
    if not EVENT_ID.match(str(event.get("id", ""))):
        return "no tiene un identificador válido"
    if event.get("recurringEventId") or event.get("recurrence"):
        return "es un evento que se repite: hazlo en Google Calendar"
    if any(not a.get("self") for a in event.get("attendees", [])):
        return "tiene invitados: hazlo en Google Calendar (así nadie recibe avisos por error)"
    if event.get("eventType", "default") != "default":
        return "no es un evento normal (cumpleaños, ausencia, ubicación…)"
    return ""


def plan_from_event(event: dict) -> EventPlan:
    """Lo que Google guarda → el mismo formato que usamos al crear (hora de Madrid)."""
    title = " ".join(str(event.get("summary") or "(sin título)").split())[:200]
    location = " ".join(str(event.get("location") or "").split())[:200]
    start, end = event.get("start", {}), event.get("end", {})
    try:
        if "dateTime" in start:
            s = datetime.fromisoformat(start["dateTime"]).astimezone(TIMEZONE)
            e = datetime.fromisoformat(end["dateTime"]).astimezone(TIMEZONE)
            if e.date() != s.date() or e <= s:
                raise ToolError("ese evento pasa de medianoche o dura varios días: hazlo en Google Calendar")
            return EventPlan(title, s.date(), s.time().replace(second=0, microsecond=0),
                             e.time().replace(second=0, microsecond=0), location)
        day = date.fromisoformat(start["date"])
        if date.fromisoformat(end["date"]) != day + timedelta(days=1):
            raise ToolError("ese evento dura varios días: hazlo en Google Calendar")
        return EventPlan(title, day, None, None, location)
    except (KeyError, ValueError):
        raise ToolError("no entiendo las fechas de ese evento: hazlo en Google Calendar") from None


@dataclass(frozen=True)
class EventPlan:
    title: str
    day: date
    start: time | None  # None = todo el día
    end: time | None
    location: str

    def when(self, year: int | None = None) -> str:
        """«jueves 8/10 de 17:00 a 18:00» (con el año solo si no es el actual)."""
        text = f"{WEEKDAYS[self.day.weekday()]} {self.day.day}/{self.day.month}"
        if year is not None and self.day.year != year:
            text += f"/{self.day.year}"
        if self.start is None:
            return f"{text} (todo el día)"
        return f"{text} de {self.start:%H:%M} a {self.end:%H:%M}"

    def text(self, year: int | None = None) -> str:
        place = f" (lugar: {self.location})" if self.location else ""
        return f"{self.title}, {self.when(year)}{place}"

    def body(self) -> dict:
        """Lo único que se envía a Google: título, lugar y fechas (sin invitados ni nada más)."""
        event: dict = {"summary": self.title}
        if self.location:
            event["location"] = self.location
        if self.start is None:
            # En un evento de todo el día, Google cuenta el día de fin como exclusivo.
            event["start"] = {"date": self.day.isoformat()}
            event["end"] = {"date": (self.day + timedelta(days=1)).isoformat()}
        else:
            for key, clock in (("start", self.start), ("end", self.end)):
                event[key] = {"dateTime": datetime.combine(self.day, clock).isoformat(),
                              "timeZone": TIMEZONE.key}
        return event


def _clean_text(value: str, what: str, *, required: bool) -> str:
    value = value.strip()
    if required and not value:
        raise ToolError(f"Falta {what}")
    if not value.isprintable():  # sin saltos de línea ni caracteres de control
        raise ToolError(f"{what.capitalize()} contiene caracteres no válidos")
    return value


def _clock(value: str, what: str) -> time:
    match = CLOCK.match(value.strip())
    if not match:
        raise ToolError(f"{what.capitalize()} no válida: {value!r} (formato HH:MM)")
    return time(int(match[1]), int(match[2]))


def parse_event(args: dict) -> EventPlan:
    """Comprueba la FORMA de los datos (fecha real, horas coherentes). El «cuándo» lo ve check_window."""
    title = _clean_text(args["title"], "el título", required=True)
    location = _clean_text(args["location"], "el lugar", required=False)
    text = args["date"].strip()
    try:
        if not DATE.match(text):
            raise ValueError
        day = date.fromisoformat(text)
    except ValueError:
        raise ToolError(f"Fecha no válida: {args['date']!r}. Debe ser una fecha real en formato AAAA-MM-DD") from None
    if args["all_day"]:
        if args["start"].strip() or args["end"].strip():
            raise ToolError("Un evento de todo el día no lleva hora: quita start y end, o quita all_day")
        return EventPlan(title, day, None, None, location)
    if not args["start"].strip():
        raise ToolError("Falta la hora de inicio: pregúntasela al usuario (o usa all_day si es de todo el día)")
    start = _clock(args["start"], "la hora de inicio")
    if args["end"].strip():
        end = _clock(args["end"], "la hora de fin")
    else:
        end = (datetime.combine(day, start) + timedelta(hours=1)).time()  # por defecto, 1 hora
        if end <= start:  # pasa de medianoche
            raise ToolError("Empieza tan tarde que 1 hora llegaría al día siguiente: indica la hora de fin (máx. 23:59)")
    if end <= start:
        raise ToolError(f"La hora de fin ({end:%H:%M}) debe ser posterior a la de inicio ({start:%H:%M}); "
                        "un evento no puede pasar de medianoche")
    return EventPlan(title, day, start, end, location)


def check_window(plan: EventPlan, now: datetime) -> None:
    """Nada en el pasado y como mucho a 2 años vista (hora de Madrid)."""
    now = now.astimezone(TIMEZONE)
    if plan.start is None:
        if plan.day < now.date():
            raise ToolError(f"El {plan.day:%d/%m/%Y} ya ha pasado")
    else:
        moment = datetime.combine(plan.day, plan.start, tzinfo=TIMEZONE)
        # Si la hora no existe (el día que se adelantan los relojes), Madrid la corrige sola: se rechaza.
        if moment.astimezone(ZoneInfo("UTC")).astimezone(TIMEZONE).time() != plan.start:
            raise ToolError(f"A las {plan.start:%H:%M} del {plan.day:%d/%m/%Y} no existe por el cambio de hora")
        if moment <= now:
            raise ToolError(f"El {plan.day:%d/%m/%Y} a las {plan.start:%H:%M} ya ha pasado")
    if plan.day > now.date() + MAX_AHEAD:
        raise ToolError("Solo puedo apuntar eventos de aquí a 2 años como mucho")


def merged_plan(old: EventPlan, args: dict) -> EventPlan:
    """El evento tal como quedaría: lo que ya tiene + lo que pides cambiar (con las mismas reglas que al crear).

    Solo cambia lo que indicas. Si cambias la hora de inicio sin decir la de fin, se conserva la duración.
    Un evento de todo el día sigue siéndolo (para pasarlo a horas: bórralo y crea uno nuevo).
    """
    date_text = args["date"].strip() or old.day.isoformat()
    if old.start is None:
        if args["start"].strip() or args["end"].strip():
            raise ToolError("Es un evento de todo el día y no tiene hora. Para ponerle hora, bórralo y crea uno nuevo")
        raw = {"all_day": True, "start": "", "end": ""}
    else:
        start_text = args["start"].strip() or f"{old.start:%H:%M}"
        end_text = args["end"].strip()
        if not end_text:
            if args["start"].strip():
                moved = (datetime.combine(old.day, _clock(start_text, "la hora de inicio"))
                         + (datetime.combine(old.day, old.end) - datetime.combine(old.day, old.start)))
                if moved.date() != old.day:
                    raise ToolError("Con esa hora de inicio el evento pasaría de medianoche: indica también la hora de fin")
                end_text = f"{moved:%H:%M}"
            else:
                end_text = f"{old.end:%H:%M}"
        raw = {"all_day": False, "start": start_text, "end": end_text}
    # El título y el lugar solo se validan si los cambias (uno ya guardado en Google no se discute).
    plan = parse_event({"title": args["title"] or "x", "date": date_text, "location": args["location"], **raw})
    return replace(plan, title=args["title"].strip() or old.title,
                   location=plan.location or old.location)


def build_calendar_tools(
    client: CalendarClient,
    today: Callable[[], date] = date.today,
    now: Callable[[], datetime] = lambda: datetime.now(TIMEZONE),
) -> list[Tool]:
    # Tabla «número → id de Google» de la ÚLTIMA lista mostrada (y de lo recién creado). El modelo solo
    # ve y usa números; el id real no sale de aquí. Cada calendar_events la renueva. (Una por conjunto de
    # herramientas, no global: dos conjuntos —p. ej. en tests— no se pisan.)
    numbers: dict[int, str] = {}
    table_lock = threading.Lock()

    def register(event_id: str) -> int:
        with table_lock:
            if (found := next((n for n, i in numbers.items() if i == event_id), None)) is not None:
                return found
            number = max(numbers, default=0) + 1
            numbers[number] = event_id
            return number

    def resolve(ref: str) -> str:
        """El id real de Google a partir de lo que pase el modelo: un número de la tabla o un id válido."""
        ref = ref.strip()
        if match := NUMBER.match(ref):
            with table_lock:
                event_id = numbers.get(int(match[1]))
            if event_id is None:
                raise ToolError("Llama primero a calendar_events y usa el número #N de la lista")
            return event_id
        if EVENT_ID.match(ref):
            return ref
        raise ToolError("Identificador de evento no válido. Llama primero a calendar_events y usa el número #N de la lista")

    def calendar_events(args: dict) -> str:
        first = parse_day(args["day"], today())
        start, end = day_range(first, args["days"])
        with table_lock:
            numbers.clear()  # los números de la lista anterior ya no valen (aunque esta falle)
        try:
            events = client.events(start, end)
        except GoogleError as e:
            raise ToolError(str(e)) from e
        if not events:
            span = "ese día" if args["days"] == 1 else f"esos {args['days']} días"
            return f"No tienes nada en el calendario {span}"
        lines, current = [], None
        for event in events:
            day = (event["start"].get("dateTime") or event["start"]["date"])[:10]
            if day != current:
                current = day
                d = date.fromisoformat(day)
                lines.append(f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} de {MONTHS[d.month - 1]}:")
            number = None if why_not_editable(event) else register(event["id"])
            lines.append(f"  {describe_event(event, number)}")
        return "\n".join(lines)

    def check_create(args: dict) -> None:
        check_window(parse_event(args), now())

    def create_event(args: dict) -> str:
        plan = parse_event(args)
        check_window(plan, now())  # otra vez: puede haber pasado un rato hasta que confirmaste
        try:
            created = client.create_event(plan.body())
        except GoogleError as e:
            raise ToolError(str(e)) from e
        if not created.get("id"):
            raise ToolError("Google no ha confirmado que el evento se haya creado: mira tu calendario")
        done = f"He apuntado en tu calendario: {plan.text(now().year)}"
        if not why_not_editable(created):  # un id raro no se registra: se apunta igual, sin número
            done += f" (#{register(created['id'])})"
        return done

    # Eventos que se acaban de enseñar al pedir confirmación: lo que confirmas es EXACTAMENTE esto
    # (la versión «etag» viaja a Google, que rechaza el cambio si el evento se ha modificado mientras tanto).
    seen: dict[str, dict] = {}
    # Qué id real se resolvió para lo que escribió el modelo, FIJADO al comprobar: si calendar_events renovara
    # la tabla mientras decides, «#1» podría pasar a ser otro evento; lo que confirmas sigue siendo lo mismo.
    pinned: dict[str, str] = {}

    def pin(ref: str) -> str:
        event_id = resolve(ref)
        pinned[ref] = event_id
        return event_id

    def pinned_id(ref: str) -> str:
        event_id = pinned.get(ref)
        if event_id is None:
            raise ToolError("No tengo el evento que confirmaste: vuelve a pedirlo")
        return event_id

    def load(event_id: str) -> tuple[dict, EventPlan]:
        """El evento (ya existente y editable) y su plan, o ToolError con el motivo."""
        try:
            event = client.get_event(event_id)
        except NotFound:
            raise ToolError("No encuentro ese evento en tu calendario principal. "
                            "Llama a calendar_events y usa el número #N de la lista") from None
        except GoogleError as e:
            raise ToolError(str(e)) from e
        if reason := why_not_editable(event):
            raise ToolError(f"No lo toco: {reason}")
        try:
            return event, plan_from_event(event)
        except ToolError as e:
            raise ToolError(f"No lo toco: {e}") from None

    def check_update(args: dict) -> None:
        event_id = pin(args["id"])
        if not any(args[k].strip() for k in ("title", "date", "start", "end", "location")):
            raise ToolError("No has indicado qué cambiar")
        event, old = load(event_id)
        new = merged_plan(old, args)
        if new == old:
            raise ToolError("El evento ya está así: no hay nada que cambiar")
        check_window(new, now())
        seen[event_id] = event

    def summarize_update(args: dict) -> str:
        event = seen.get(pinned.get(args["id"], ""))
        if not event:
            return "Cambiar un evento de tu calendario"
        old = plan_from_event(event)
        return (f"Cambiar en tu calendario: {old.text(now().year)}  →  "
                f"{merged_plan(old, args).text(now().year)}")

    def update_event(args: dict) -> str:
        event_id = pinned_id(args["id"])
        event = seen.pop(event_id, None)
        pinned.pop(args["id"], None)
        if event is None:
            raise ToolError("No tengo el evento que confirmaste: vuelve a pedirlo")
        old = plan_from_event(event)
        new = merged_plan(old, args)
        check_window(new, now())  # otra vez: puede haber pasado un rato hasta que confirmaste
        sent = new.body()
        changes = {}
        if new.title != old.title:
            changes["summary"] = new.title
        if new.location != old.location:
            changes["location"] = new.location
        if (new.day, new.start, new.end) != (old.day, old.start, old.end):
            changes["start"], changes["end"] = sent["start"], sent["end"]
        try:
            client.patch_event(event_id, changes, event.get("etag", ""))
        except GoogleError as e:
            raise ToolError(str(e)) from e
        return f"He cambiado en tu calendario: {old.text(now().year)}  →  {new.text(now().year)}"

    def check_delete(args: dict) -> None:
        event_id = pin(args["id"])
        event, _ = load(event_id)
        seen[event_id] = event

    def summarize_delete(args: dict) -> str:
        event = seen.get(pinned.get(args["id"], ""))
        if not event:
            return "BORRAR un evento de tu calendario"
        return (f"BORRAR de tu calendario: {plan_from_event(event).text(now().year)}. "
                "ARTHUR no puede deshacerlo")

    def delete_event(args: dict) -> str:
        event_id = pinned_id(args["id"])
        event = seen.pop(event_id, None)
        pinned.pop(args["id"], None)
        if event is None:
            raise ToolError("No tengo el evento que confirmaste: vuelve a pedirlo")
        try:
            client.delete_event(event_id, event.get("etag", ""))
        except GoogleError as e:
            raise ToolError(str(e)) from e
        with table_lock:  # ese número ya no apunta a nada
            for number in [n for n, i in numbers.items() if i == event_id]:
                del numbers[number]
        return f"He borrado de tu calendario: {plan_from_event(event).text(now().year)}"

    return [
        Tool(
            name="calendar_events",
            description="Agenda del usuario (Google Calendar). Los editables llevan #N.",
            risk=Risk.READ,
            params=(
                Param("day", str, "hoy, mañana, lunes… o AAAA-MM-DD", required=False, default="hoy", max_length=20),
                Param("days", int, "cuántos días (7 = semana)", required=False, default=1, minimum=1, maximum=14),
            ),
            run=calendar_events,
            direct_reply=True,
        ),
        Tool(
            name="create_event",
            description="Apunta un evento en el calendario (no invita a nadie). Fecha exacta AAAA-MM-DD.",
            risk=Risk.WRITE,
            params=(
                Param("title", str, "", max_length=200),
                Param("date", str, "AAAA-MM-DD", max_length=10),
                Param("start", str, "HH:MM", required=False, default="", max_length=5),
                Param("end", str, "HH:MM, por defecto 1 h después", required=False, default="", max_length=5),
                Param("all_day", bool, "", required=False, default=False),
                Param("location", str, "", required=False, default="", max_length=200),
            ),
            check=check_create,
            # Con el día de la semana a la vista: si el modelo se equivoca al calcular la fecha, se nota.
            summarize=lambda a: f"Crear en tu calendario: {parse_event(a).text(now().year)}",
            run=create_event,
        ),
        Tool(
            name="update_event",
            description="Cambia un evento (id = su número #N de calendar_events). Indica solo lo que cambia.",
            risk=Risk.WRITE,
            params=(
                Param("id", str, "", max_length=100),
                Param("title", str, "", required=False, default="", max_length=200),
                Param("date", str, "AAAA-MM-DD", required=False, default="", max_length=10),
                Param("start", str, "HH:MM", required=False, default="", max_length=5),
                Param("end", str, "HH:MM", required=False, default="", max_length=5),
                Param("location", str, "", required=False, default="", max_length=200),
            ),
            check=check_update,
            summarize=summarize_update,
            run=update_event,
        ),
        Tool(
            name="delete_event",
            description="Borra un evento (id = su número #N de calendar_events).",
            risk=Risk.DANGEROUS,  # no se puede deshacer desde ARTHUR: confirma siempre, aunque confirm_writes sea false
            params=(Param("id", str, "", max_length=100),),
            check=check_delete,
            summarize=summarize_delete,
            run=delete_event,
        ),
    ]
