"""Contactos y mensajes: ARTHUR PREPARA el correo o el WhatsApp y tú lo envías.

No hay contraseñas ni acceso a tus cuentas: ARTHUR abre en Firefox
  - Gmail con un correo nuevo ya escrito (destinatario, asunto y texto), o la respuesta a un correo
    recibido (para quien te lo escribió, con «Re: asunto»);
  - WhatsApp Web con el chat de esa persona y el mensaje ya escrito.
Tú lo revisas y pulsas «Enviar». ARTHUR nunca envía nada por su cuenta.
"""

import re
import urllib.parse
from email.utils import getaddresses

from ..integrations.google import GmailClient, GoogleError
from ..memory.store import EMAIL, Contact, MemoryStore, MemoryStoreError, normalize_phone
from .base import Param, Risk, Tool, ToolDenied, ToolError
from .gmail import MESSAGE_ID
from .programs import Programs

MAX_BODY = 3_000
MAX_SUBJECT = 200
REPLY_PREFIX = re.compile(r"^re\s*:", re.IGNORECASE)


def gmail_compose_url(to: str, subject: str, body: str) -> str:
    """Ventana de redactar de Gmail ya rellena. Solo to, su y body: nunca cc ni cco."""
    query = urllib.parse.urlencode(
        {"view": "cm", "fs": "1", "to": to, "su": subject, "body": body}, quote_via=urllib.parse.quote)
    return f"https://mail.google.com/mail/?{query}"


def sender_address(from_header: str) -> str:
    """La dirección del remitente de un correo, o error. El encabezado lo escribe un tercero: se desconfía."""
    if not from_header.strip() or not from_header.isprintable():  # sin saltos de línea ni caracteres de control
        raise ToolError("No puedo responder: ese correo no trae un remitente válido")
    found = [address.strip() for _, address in getaddresses([from_header]) if address]
    # Nunca a varios destinatarios: si el remitente no es una única dirección clara, no se responde.
    # La dirección debe aparecer TAL CUAL en la cabecera: el analizador de Python «arregla» algunas rarezas
    # (p. ej. «a@exam ple.com» → «a@example.com») y no se responde a una dirección que el remitente no escribió.
    if (len(found) != 1 or len(found[0]) > 254 or not EMAIL.match(found[0]) or not found[0].isprintable()
            or found[0] not in from_header):
        raise ToolError("No puedo responder: el remitente de ese correo no tiene una dirección de correo normal")
    return found[0]


def reply_subject(subject: str) -> str:
    """«Re: asunto», en una sola línea y con longitud máxima (el asunto original lo escribe un tercero)."""
    clean = "".join(ch for ch in " ".join(subject.split()) if ch.isprintable())
    if not clean:
        return "Re: (sin asunto)"
    return (clean if REPLY_PREFIX.match(clean) else f"Re: {clean}")[:MAX_SUBJECT]


def build_messaging_tools(store: MemoryStore, programs: Programs, gmail: GmailClient | None = None) -> list[Tool]:
    def contact(name: str) -> Contact:
        found = store.find_contacts(name)
        exact = [c for c in found if c.name.lower() == name.strip().lower()]
        if exact:
            return exact[0]
        if len(found) == 1:
            return found[0]
        if not found:
            raise ToolError(f"No tengo a «{name}» en tus contactos. Puedes decirme: «guarda a {name} con el número …»")
        raise ToolError(f"Hay varios contactos que encajan con «{name}»: {', '.join(c.name for c in found)}")

    def email_address(to: str) -> str:
        to = to.strip()
        if EMAIL.match(to):
            return to
        c = contact(to)
        if not c.email:
            raise ToolError(f"No tengo el correo de {c.name}")
        return c.email

    def phone_number(to: str) -> tuple[str, str]:
        try:
            number = normalize_phone(to)
            return number, f"+{number}"
        except MemoryStoreError:
            c = contact(to)
            if not c.phone:
                raise ToolError(f"No tengo el teléfono de {c.name}") from None
            return c.phone, c.name

    def open_in_browser(url: str) -> None:
        programs.launch(["xdg-open", url])

    # --- contactos ----------------------------------------------------------
    def check_save(args: dict) -> None:
        if not args["phone"] and not args["email"]:
            raise ToolError("Indica al menos un teléfono o un correo")
        try:
            if args["phone"]:
                normalize_phone(args["phone"])
        except MemoryStoreError as e:
            raise ToolDenied(str(e)) from e
        if args["email"] and not EMAIL.match(args["email"].strip()):
            raise ToolDenied(f"Correo no válido: {args['email']}")

    def save_contact(args: dict) -> str:
        try:
            c = store.add_contact(args["name"], args["phone"] or None, args["email"] or None)
        except MemoryStoreError as e:
            raise ToolError(str(e)) from e
        return f"Contacto guardado (#{c.id}): {c.name} · tel. {c.phone or '—'} · {c.email or '—'}"

    def list_contacts(args: dict) -> str:
        found = store.find_contacts(args["query"])
        if not found:
            return "No tengo contactos guardados" + (f" que encajen con «{args['query']}»" if args["query"] else "")
        return "\n".join(f"#{c.id} {c.name} · tel. {c.phone or '—'} · {c.email or '—'}" for c in found)

    def forget_contact(args: dict) -> str:
        if not store.delete_contact(args["id"]):
            raise ToolError(f"No hay ningún contacto #{args['id']}")
        return f"He borrado el contacto #{args['id']}"

    # --- preparar mensajes --------------------------------------------------
    def compose_email(args: dict) -> str:
        to = email_address(args["to"])
        open_in_browser(gmail_compose_url(to, args["subject"], args["body"]))
        return f"He abierto en Gmail un correo para {to}, ya escrito. Revísalo y pulsa «Enviar» si está bien."

    def check_reply(args: dict) -> None:
        if not MESSAGE_ID.match(args["id"]):
            raise ToolError("Identificador de correo no válido (usa el [id …] de gmail_inbox)")
        if not args["body"].strip():
            raise ToolError("Falta el texto de la respuesta")

    def reply_email(args: dict) -> str:
        # Solo se piden las cabeceras De y Asunto (permiso de solo lectura que ya existe): el texto del correo
        # no se lee, así que no puede llegar al modelo ni colarse en la respuesta.
        try:
            original = gmail.sender_and_subject(args["id"])
        except GoogleError as e:
            raise ToolError(str(e)) from e
        to = sender_address(original["from"])  # siempre el remitente original, nunca otra dirección
        open_in_browser(gmail_compose_url(to, reply_subject(original["subject"]), args["body"]))
        return (f"He abierto en Gmail la respuesta para {to}, ya escrita. Revísala y pulsa «Enviar» si está bien. "
                "Se abre como correo nuevo con el asunto «Re: …»: puede que Gmail no la junte con la conversación.")

    def compose_whatsapp(args: dict) -> str:
        number, who = phone_number(args["to"])
        query = urllib.parse.urlencode({"phone": number, "text": args["text"]}, quote_via=urllib.parse.quote)
        open_in_browser(f"https://web.whatsapp.com/send?{query}")
        return f"He abierto WhatsApp Web con el mensaje para {who} ya escrito. Revísalo y pulsa enviar."

    reply_tools = [] if gmail is None else [
        Tool(
            name="reply_email",
            description="Prepara en Gmail la respuesta a un correo (id de gmail_inbox), solo para su remitente; "
                        "NO la envía (la envía el usuario).",
            risk=Risk.LOW,
            params=(
                Param("id", str, "", max_length=32),
                Param("body", str, "", max_length=MAX_BODY),
            ),
            check=check_reply,
            run=reply_email,
        ),
    ]

    return [
        Tool(
            name="save_contact",
            description="Guarda o completa un contacto (teléfono y/o correo).",
            risk=Risk.WRITE,
            params=(
                Param("name", str, "", max_length=80),
                Param("phone", str, "", required=False, default="", max_length=30),
                Param("email", str, "", required=False, default="", max_length=254),
            ),
            check=check_save,
            summarize=lambda a: f"Guardar el contacto {a['name']}: tel. {a['phone'] or '—'} · {a['email'] or '—'}",
            run=save_contact,
        ),
        Tool(
            name="list_contacts",
            description="Busca contactos guardados.",
            risk=Risk.READ,
            params=(Param("query", str, "", required=False, default="", max_length=80),),
            run=list_contacts,
        ),
        Tool(
            name="forget_contact",
            description="Borra un contacto por su #id.",
            risk=Risk.WRITE,
            params=(Param("id", int, "", minimum=1),),
            summarize=lambda a: f"Borrar el contacto #{a['id']}",
            run=forget_contact,
        ),
        Tool(
            name="compose_email",
            description="Prepara un correo en Gmail; NO lo envía (lo envía el usuario).",
            risk=Risk.LOW,
            params=(
                Param("to", str, "correo o nombre de contacto", max_length=254),
                Param("subject", str, "", max_length=200),
                Param("body", str, "", max_length=MAX_BODY),
            ),
            check=lambda a: email_address(a["to"]) and None,
            run=compose_email,
        ),
        Tool(
            name="compose_whatsapp",
            description="Prepara un WhatsApp; NO lo envía (lo envía el usuario).",
            risk=Risk.LOW,
            params=(
                Param("to", str, "contacto o teléfono", max_length=80),
                Param("text", str, "", max_length=MAX_BODY),
            ),
            check=lambda a: phone_number(a["to"]) and None,
            run=compose_whatsapp,
        ),
        *reply_tools,
    ]
