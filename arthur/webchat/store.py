"""Contactos de la web pública: quien deja nombre, correo, teléfono (opcional) y necesidad en el chat, con su
consentimiento.

Aquí solo se VALIDAN: desde 0.63 el chat no los guarda, los entrega al registro de clientes (clientes_link.py), que
es quien aplica las reglas de duplicados y el borrado a los 365 días. Nunca se escribe el contenido del formulario en
logs ni en excepciones.
"""

from __future__ import annotations

import re
import unicodedata

MAX_NAME = 100
MAX_EMAIL = 200
MAX_NEED = 2000
MAX_PHONE = 30
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
# Lo que la gente escribe en un teléfono: un «+» delante como mucho, cifras y separadores (espacio, guion, punto,
# paréntesis). Letras o cualquier otra cosa: no es un teléfono.
_PHONE = re.compile(r"^\+?[0-9 ().\-]+$")
QUEUED_NOTICE = "Contacto nuevo en cola (clientes no responde)"


class ContactError(Exception):
    """Dato inválido; `field` dice cuál (name, email, phone, need o consent) sin repetir su contenido."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _lang(value: object) -> str:
    # Solo hablamos es/en/ca; cualquier otra cosa (o basura) cae en inglés.
    return value if value in ("es", "en", "ca") else "en"


def _clean(text: str, *, keep_newline: bool) -> str:
    # Los caracteres de control pueden romper listados; se quitan. En la necesidad se conserva \n,
    # y \r\n pasa a \n.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in text:
        if ch == "\n" and keep_newline:
            out.append(ch)
        elif ch in "\n\t":
            out.append(" ")
        elif unicodedata.category(ch) != "Cc":
            out.append(ch)
    return "".join(out).strip()


def _utf8(text: str) -> bool:
    # Un «\ud800» suelto es JSON válido pero no se puede escribir como UTF-8 (ni enviar a clientes ni dejar en la
    # cola): mejor un 400 con el campo que un fallo más adelante.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _phone(value: object) -> str:
    """El teléfono es opcional: vacío o ausente → "". Si viene, se comprueba y se deja tal cual lo escribió (sin
    espacios alrededor): quien lo normaliza y lo compara con otros es el registro de clientes."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ContactError("phone")
    value = value.strip()
    if not value:
        return ""
    if len(value) > MAX_PHONE or not _PHONE.match(value):
        raise ContactError("phone")
    digits = re.sub(r"[^0-9]", "", value)
    # Sin el prefijo internacional (+ o 00), un teléfono tiene de 9 a 15 cifras: «1234» no lo es, y uno nacional
    # de 9 cifras (600 11 22 33) sí. El registro de clientes le pone el 34 delante si no lleva prefijo.
    if not value.startswith("+") and digits.startswith("00"):
        digits = digits[2:]
    if not 9 <= len(digits) <= 15:
        raise ContactError("phone")
    return value


def validate_contact(body: dict) -> dict:
    def field(key: str, limit: int, *, keep_newline: bool = False) -> str:
        value = body.get(key)
        if not isinstance(value, str):
            raise ContactError(key)
        if not _utf8(value):
            raise ContactError(key)
        value = _clean(value, keep_newline=keep_newline)
        if not value or len(value) > limit:
            raise ContactError(key)
        return value

    name = " ".join(field("name", MAX_NAME * 4).split())  # espacios internos repetidos fuera
    if len(name) > MAX_NAME:
        raise ContactError("name")
    email = field("email", MAX_EMAIL)
    if not _EMAIL.match(email):
        raise ContactError("email")
    phone = _phone(body.get("phone"))
    need = field("need", MAX_NEED, keep_newline=True)
    # `is True` a propósito: "true", 1 o "on" no son un consentimiento explícito.
    if body.get("consent") is not True:
        raise ContactError("consent")
    return {"name": name, "email": email, "phone": phone, "need": need, "lang": _lang(body.get("lang"))}


def notice_text(client_id: int, nuevo: bool) -> str:
    # El aviso pasa por el núcleo y el servicio push del móvil: solo el número de cliente, nada de la visita.
    numero = f"C-{client_id:04d}"
    return f"Cliente nuevo {numero}" if nuevo else f"{numero} ha vuelto a escribir"
