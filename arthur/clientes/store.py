"""Almacén de clientes: <datos>/clientes.sqlite3 (fichero 600).

  clientes   una ficha por persona: número (C-0001), nombre, correo y/o teléfono, origen, estado, notas privadas
  mensajes   lo que ha escrito cada una (o lo que Marc apunta de una llamada o un WhatsApp), con su fecha

Reglas de duplicados (docs/clientes.md):
  - El correo se compara sin espacios y en minúsculas; el teléfono, solo por sus cifras y con +34 si no lleva
    prefijo («+34 600 11 22 33» = «600-112-233»).
  - Si coincide el correo O el teléfono, el mensaje va a esa ficha (y se completa el dato que faltaba).
  - Si el correo es de una ficha y el teléfono de OTRA, no se fusionan: va a la del correo y queda marcada como
    `posible_duplicado` de la otra, para que decida Marc.

Las fechas se guardan en UTC con el mismo formato: así ordenar y comparar textos es ordenar y comparar fechas
(con la hora de Madrid, el cambio +01/+02 lo estropearía una hora al año). La página las enseña en hora de Madrid.

Nunca se escribe en excepciones ni en registros lo que ha escrito nadie: los errores solo dicen QUÉ campo falla.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ESTADOS = ("nuevo", "hablando", "presupuesto", "cliente", "descartado")
ORIGENES = ("web", "telefono", "whatsapp", "persona", "otro")
IDIOMAS = ("es", "en", "ca")
MAX_NAME = 100
MAX_EMAIL = 200
MAX_TEXT = 2000
MAX_NOTES = 4000
MAX_PHONE_INPUT = 30
# El mismo patrón que webchat/store.py: lo que el chat acepta, aquí también vale. Copiado (no importado) para que
# este servicio no dependa de los cambios del chat.
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
# Lo que alguien escribe en un teléfono además de cifras: espacios, guiones, puntos y paréntesis. Una letra o un «+»
# en medio no son un teléfono: mejor un error que guardar un número que no es.
_PHONE = re.compile(r"^\+?[0-9 ()./-]+$")
_NUMERO = re.compile(r"^c-?0*(\d{1,9})$")


class ClientError(Exception):
    """Dato inválido; `field` dice cuál (name, email, phone, text, origin, status, notes, contact, duplicado…)
    sin repetir su contenido: el mensaje puede acabar en un registro."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def numero(client_id: int) -> str:
    return f"C-{client_id:04d}"


def normalize_email(value: str) -> str:
    """«  Ana@Example.COM » → «ana@example.com»; vacío → «». Lanza ClientError("email") si no es un correo."""
    email = value.strip().lower()
    # Un carácter de control (\x00…) no cabe en un correo de verdad y podría romper la página.
    if email and (len(email) > MAX_EMAIL or not _EMAIL.match(email)
                  or any(unicodedata.category(ch) == "Cc" for ch in email)):
        raise ClientError("email")
    return email


def normalize_phone(value: str) -> str:
    """Solo las cifras, con el prefijo del país delante: «+34 600 11 22 33», «0034600112233» y «600-112-233» dan
    «34600112233». Sin prefijo (no empieza por + ni 00) se entiende España. Vacío → «»."""
    if len(value) > MAX_PHONE_INPUT:
        raise ClientError("phone")
    text = value.strip()
    if not text:
        return ""
    if not _PHONE.match(text):
        raise ClientError("phone")
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        typed = full = digits            # «+44 7911…»: el prefijo ya va en las cifras
    elif digits.startswith("00"):
        typed = full = digits[2:]        # «0034…» es lo mismo que «+34…»
    elif len(digits) == 11 and digits.startswith("34") and digits[2] in "6789":
        # «34600112233» sin «+»: es un número español con su prefijo (los españoles tienen 9 cifras y empiezan
        # por 6-9). Sin esto quedaba «3434600112233», otro cliente distinto de «+34 600 11 22 33».
        typed = full = digits
    else:
        typed, full = digits, "34" + digits
    # Al menos 9 cifras escritas (un móvil español) y como mucho 15 en total (el máximo internacional).
    if len(typed) < 9 or len(full) > 15:
        raise ClientError("phone")
    return full


def _clean(text: str, *, keep_newline: bool) -> str:
    # Los caracteres de control pueden romper la página o un listado; se quitan. En los textos largos se
    # conserva el salto de línea (\r\n pasa a \n); en los cortos, todo espacio raro pasa a uno normal.
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


def _name(value: object) -> str:
    # Espacios de sobra fuera antes de medir; un texto absurdamente largo ni se limpia.
    if not isinstance(value, str) or len(value) > MAX_NAME * 4:
        raise ClientError("name")
    name = " ".join(_clean(value, keep_newline=False).split())
    if not name or len(name) > MAX_NAME:
        raise ClientError("name")
    return name


def _long_text(value: object, limit: int, field: str, *, required: bool) -> str:
    if not isinstance(value, str) or len(value) > limit * 2:
        raise ClientError(field)
    text = _clean(value, keep_newline=True)
    if len(text) > limit or (required and not text):
        raise ClientError(field)
    return text


def _email(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ClientError("email")
    return normalize_email(value)


def _phone(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ClientError("phone")
    return normalize_phone(value)


def _origin(value: object) -> str:
    if value not in ORIGENES:
        raise ClientError("origin")
    return value


class ClientStore:
    def __init__(self, path: Path | str, now: Callable[[], datetime]) -> None:
        self._now = now
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Se crea ya con 600 (no se crea abierto y se cierra después): contiene correos y teléfonos ajenos.
            os.close(os.open(target, os.O_CREAT | os.O_RDWR, 0o600))
            os.chmod(target, 0o600)
        # Una conexión compartida entre los hilos del servidor web; el candado las ordena. Las transacciones
        # BEGIN IMMEDIATE ordenan además a otros procesos (una orden de la terminal con el servicio en marcha).
        self._lock = threading.Lock()
        self._db = sqlite3.connect(target, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")  # sin esto SQLite no borra los mensajes en cascada
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                telefono TEXT NOT NULL DEFAULT '',
                origen TEXT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'nuevo',
                notas TEXT NOT NULL DEFAULT '',
                alta TEXT NOT NULL,
                ultimo_contacto TEXT NOT NULL,
                posible_duplicado INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
                sin_leer INTEGER NOT NULL DEFAULT 0);
            -- Únicos solo cuando hay dato: muchas fichas pueden no tener teléfono (o correo).
            CREATE UNIQUE INDEX IF NOT EXISTS clientes_email ON clientes(email) WHERE email <> '';
            CREATE UNIQUE INDEX IF NOT EXISTS clientes_telefono ON clientes(telefono) WHERE telefono <> '';
            CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                fecha TEXT NOT NULL,
                origen TEXT NOT NULL,
                idioma TEXT NOT NULL DEFAULT '',
                nombre TEXT NOT NULL,
                texto TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS mensajes_cliente ON mensajes(cliente);
        """)

    # --- fechas ---
    def _stamp(self, moment: datetime | None = None) -> str:
        moment = moment or self._now()
        if moment.tzinfo is None:
            # Sin zona no se sabe qué hora es de verdad; mejor fallar en los tests que guardar fechas ambiguas.
            raise ValueError("el reloj de clientes debe dar fechas con zona horaria")
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")

    # --- transacciones ---
    def _write(self, fn):
        """Ejecuta fn() dentro de BEGIN IMMEDIATE: o se guarda todo o nada, y nadie más escribe a la vez."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = fn()
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return result

    def _find(self, column: str, value: str) -> int | None:
        if not value:
            return None
        row = self._db.execute(f"SELECT id FROM clientes WHERE {column} = ?", (value,)).fetchone()
        return int(row["id"]) if row else None

    # --- altas ---
    def record_contact(self, name: str, email: str, phone: str, text: str, lang: str,
                       origin: str = "web") -> tuple[int, bool]:
        """Un mensaje de alguien (de la web, normalmente). Devuelve (id, nuevo): nuevo=False si ya tenía ficha."""
        return self._upsert(name, email, phone, _long_text(text, MAX_TEXT, "text", required=True),
                            lang if lang in IDIOMAS else "", origin, unread=True)

    def create(self, name: str, email: str, phone: str, origin: str, text: str = "") -> int:
        """Alta a mano (llamada, WhatsApp, en persona). Mismas reglas: si ya existe, devuelve esa ficha.
        Lo que apunta Marc ya está leído."""
        cid, _ = self._upsert(name, email, phone, _long_text(text, MAX_TEXT, "text", required=False), "",
                              origin, unread=False)
        return cid

    def _upsert(self, name, email, phone, text: str, lang: str, origin, *, unread: bool) -> tuple[int, bool]:
        name, email, phone, origin = _name(name), _email(email), _phone(phone), _origin(origin)
        if not email and not phone:
            raise ClientError("contact")
        stamp = self._stamp()

        def run() -> tuple[int, bool]:
            by_email, by_phone = self._find("email", email), self._find("telefono", phone)
            target = by_email or by_phone
            if target is None:
                cur = self._db.execute(
                    "INSERT INTO clientes (nombre, email, telefono, origen, estado, alta, ultimo_contacto) "
                    "VALUES (?, ?, ?, ?, 'nuevo', ?, ?)", (name, email, phone, origin, stamp, stamp))
                cid, new = int(cur.lastrowid), True
            else:
                cid, new = target, False
                row = self._db.execute("SELECT email, telefono FROM clientes WHERE id = ?", (cid,)).fetchone()
                # Completa el dato que faltaba, pero solo si no es de otra ficha (eso es un posible duplicado).
                if email and not row["email"] and by_email is None:
                    self._db.execute("UPDATE clientes SET email = ? WHERE id = ?", (email, cid))
                if phone and not row["telefono"] and by_phone is None:
                    self._db.execute("UPDATE clientes SET telefono = ? WHERE id = ?", (phone, cid))
                if by_email and by_phone and by_email != by_phone:
                    self._db.execute("UPDATE clientes SET posible_duplicado = ? WHERE id = ?", (by_phone, cid))
                self._touch(cid, stamp)
            if text:
                self._db.execute(
                    "INSERT INTO mensajes (cliente, fecha, origen, idioma, nombre, texto) VALUES (?, ?, ?, ?, ?, ?)",
                    (cid, stamp, origin, lang, name, text))
                if unread:
                    self._db.execute("UPDATE clientes SET sin_leer = sin_leer + 1 WHERE id = ?", (cid,))
            return cid, new

        return self._write(run)

    def _touch(self, cid: int, stamp: str) -> None:
        # Volver a hablar con alguien descartado lo reabre; los demás estados los cambia solo Marc.
        self._db.execute(
            "UPDATE clientes SET ultimo_contacto = ?, "
            "estado = CASE estado WHEN 'descartado' THEN 'hablando' ELSE estado END WHERE id = ?", (stamp, cid))

    def add_message(self, client_id: int, text: str, origin: str) -> None:
        """Marc apunta una llamada o un WhatsApp en la ficha. El mensaje lleva el nombre de la ficha."""
        text = _long_text(text, MAX_TEXT, "text", required=True)
        origin = _origin(origin)
        stamp = self._stamp()

        def run() -> None:
            row = self._db.execute("SELECT nombre FROM clientes WHERE id = ?", (client_id,)).fetchone()
            if row is None:
                raise KeyError(client_id)
            self._db.execute(
                "INSERT INTO mensajes (cliente, fecha, origen, idioma, nombre, texto) VALUES (?, ?, ?, '', ?, ?)",
                (client_id, stamp, origin, row["nombre"], text))
            self._touch(client_id, stamp)

        self._write(run)

    # --- lectura ---
    def list(self, query: str = "", estado: str | None = None) -> list[dict]:
        """Fichas ordenadas por último contacto. `query` busca en nombre, correo, teléfono y número (C-0012)."""
        if estado is not None and estado not in ESTADOS:
            raise ClientError("status")
        with self._lock:
            rows = self._db.execute(
                "SELECT id, nombre, email, telefono, estado, origen, ultimo_contacto, sin_leer, posible_duplicado "
                "FROM clientes ORDER BY ultimo_contacto DESC, id DESC").fetchall()
        match = self._matcher(query)
        return [{"id": r["id"], "numero": numero(r["id"]), "nombre": r["nombre"], "estado": r["estado"],
                 "origen": r["origen"], "ultimo_contacto": r["ultimo_contacto"], "sin_leer": r["sin_leer"],
                 "posible_duplicado": r["posible_duplicado"]}
                for r in rows if (estado is None or r["estado"] == estado) and match(r)]

    @staticmethod
    def _matcher(query: str) -> Callable[[sqlite3.Row], bool]:
        # Se filtra en Python y no con LIKE: LIKE de SQLite no entiende mayúsculas con acento («PÉREZ»). Son pocas
        # fichas (cientos), así que recorrerlas todas no cuesta nada.
        q = " ".join(query.split()).casefold()[:200]
        if not q:
            return lambda row: True
        by_number = _NUMERO.match(q.replace(" ", ""))
        if by_number:  # «C-0012», «c12»: solo el número de cliente
            wanted = int(by_number.group(1))
            return lambda row: row["id"] == wanted
        digits = re.sub(r"\D", "", q)
        only_digits = bool(digits) and re.fullmatch(r"[0-9 ()./+-]+", q) is not None

        def match(row: sqlite3.Row) -> bool:
            if q in row["nombre"].casefold() or q in row["email"]:
                return True
            if only_digits and len(digits) >= 3 and digits in row["telefono"]:
                return True
            return only_digits and digits.isdigit() and int(digits) == row["id"]

        return match

    def get(self, client_id: int) -> dict | None:
        """La ficha entera con sus mensajes en orden (el primero arriba), o None si no existe."""
        with self._lock:
            row = self._db.execute("SELECT * FROM clientes WHERE id = ?", (client_id,)).fetchone()
            if row is None:
                return None
            mensajes = self._db.execute(
                "SELECT id, fecha, origen, idioma, nombre, texto FROM mensajes WHERE cliente = ? ORDER BY fecha, id",
                (client_id,)).fetchall()
        ficha = dict(row)
        ficha["numero"] = numero(ficha["id"])
        ficha["mensajes"] = [dict(m) for m in mensajes]
        return ficha

    def mark_read(self, client_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE clientes SET sin_leer = 0 WHERE id = ?", (client_id,))

    # --- cambios ---
    _UPDATABLE = {"nombre": "name", "email": "email", "telefono": "phone", "estado": "status", "notas": "notes"}

    def update(self, client_id: int, **campos) -> None:
        """Cambia datos de la ficha. Un correo o teléfono que ya es de OTRA ficha → ClientError("duplicado"):
        fusionar fichas lo decide Marc (fuera de esta versión). Si no existe → KeyError."""
        for key in campos:
            if key not in self._UPDATABLE:
                raise ClientError(key)
        values: dict[str, object] = {}
        if "nombre" in campos:
            values["nombre"] = _name(campos["nombre"])
        if "email" in campos:
            values["email"] = _email(campos["email"])
        if "telefono" in campos:
            values["telefono"] = _phone(campos["telefono"])
        if "estado" in campos:
            if campos["estado"] not in ESTADOS:
                raise ClientError("status")
            values["estado"] = campos["estado"]
        if "notas" in campos:
            values["notas"] = _long_text(campos["notas"], MAX_NOTES, "notes", required=False)
        if not values:
            return

        def run() -> None:
            row = self._db.execute("SELECT email, telefono FROM clientes WHERE id = ?", (client_id,)).fetchone()
            if row is None:
                raise KeyError(client_id)
            email = values.get("email", row["email"])
            phone = values.get("telefono", row["telefono"])
            if not email and not phone:
                raise ClientError("contact")
            for column, value in (("email", email), ("telefono", phone)):
                other = self._find(column, value)
                if other is not None and other != client_id:
                    raise ClientError("duplicado")
            # Las columnas salen de _UPDATABLE (lista fija), nunca de la petición.
            sets = ", ".join(f"{column} = ?" for column in values)
            self._db.execute(f"UPDATE clientes SET {sets} WHERE id = ?", (*values.values(), client_id))

        self._write(run)

    # --- borrado ---
    def delete(self, client_id: int) -> bool:
        """Borra la ficha y todos sus mensajes (derecho de supresión)."""
        return self._write(lambda: self._db.execute("DELETE FROM clientes WHERE id = ?", (client_id,)).rowcount > 0)

    def purge(self, days: int = 365) -> int:
        """Borra las fichas sin contacto desde hace más de `days` días, salvo las de estado `cliente`: esas
        las rige la obligación de conservar los datos de facturación y nunca se borran solas."""
        cutoff = self._stamp(self._now() - timedelta(days=days))
        return self._write(lambda: self._db.execute(
            "DELETE FROM clientes WHERE ultimo_contacto < ? AND estado <> 'cliente'", (cutoff,)).rowcount)

    def close(self) -> None:
        with self._lock:
            self._db.close()
