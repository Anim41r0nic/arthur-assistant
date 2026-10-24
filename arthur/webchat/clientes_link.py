"""El chat de la web entrega cada contacto al registro de clientes (servicio `clientes`, docs/clientes.md).

Habla con `POST /api/interno/contacto` por la red interna de Docker, con la llave del aparato especial `webchat` de
clientes: esa llave SOLO sirve para esa ruta (no lista, no lee, no borra). `clientes` aplica las reglas de duplicados y
contesta `{id, nuevo}`.

Si `clientes` está caído o tarda más de 5 s, el contacto no se pierde: va a una cola en el volumen del chat (un JSON
por contacto, permisos 600, escrito de forma atómica) y un hilo la reintenta cada minuto (cli.py). La visita ve
«recibido» igual. Un fichero de cola roto se aparta (`.corrupto`) y no frena a los demás.

Nunca se escriben en el registro ni en las excepciones la llave ni los datos del contacto: solo el tipo de fallo o el
código HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

from ..integrations.voz import check_voice_url

log = logging.getLogger("arthur.webchat.clientes")

TIMEOUT = 5
ROUTE = "/api/interno/contacto"
FIELDS = ("name", "email", "phone", "need", "lang")
# Los campos que `clientes` puede señalar en un 400 y que tiene sentido decirle a la visita.
VISITOR_FIELDS = ("name", "email", "phone", "need")


class ClientesRejected(Exception):
    """`clientes` dice que un dato no vale (400): reintentar no lo arreglaría. `field` es el campo, sin su valor."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class _Failed(Exception):
    """No se ha podido entregar ahora (red, tiempo, 5xx, llave mal puesta, respuesta rara): toca encolar."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Una redirección se llevaría la llave a otra dirección: `clientes` nunca redirige, así que se trata como fallo.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ClientesLink:
    def __init__(self, url: str, token: str, queue_dir: Path | str, opener=None) -> None:
        try:
            # Misma regla que la voz y los avisos: solo red interna, la llave no puede salir a internet por un error.
            base = check_voice_url(url)
        except ValueError:
            raise ValueError("ARTHUR_WEBCHAT_CLIENTES_URL debe ser de la red interna (p. ej. http://clientes:8777)") \
                from None
        if len(token) < 32:
            raise ValueError("La llave de clientes es demasiado corta")
        self.url = base + ROUTE
        self.queue_dir = Path(queue_dir)
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def __repr__(self) -> str:   # sin la llave
        return f"ClientesLink({self.url!r})"

    # --- entrega ---
    def _post(self, contact: dict) -> tuple[int, bool]:
        body = json.dumps({k: contact.get(k, "") for k in FIELDS}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                answer = json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                if e.code == 400:
                    raise ClientesRejected(_field(e)) from None
            raise _Failed(f"error {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            # Solo el tipo: el mensaje de un fallo de red puede repetir la petición entera.
            raise _Failed(type(e).__name__) from None
        id_, nuevo = (answer.get("id"), answer.get("nuevo")) if isinstance(answer, dict) else (None, None)
        # `bool` es un `int` en Python: {"id": true} no es un cliente.
        if isinstance(id_, bool) or not isinstance(id_, int) or id_ < 1 or not isinstance(nuevo, bool):
            raise _Failed("respuesta rara")
        return id_, nuevo

    def deliver(self, contact: dict) -> tuple[int, bool] | None:
        """Entrega el contacto: (id, nuevo), o None si `clientes` no responde y ha quedado en la cola.
        Lanza ClientesRejected si `clientes` rechaza un dato, y OSError si ni siquiera se puede encolar."""
        try:
            return self._post(contact)
        except _Failed as e:
            log.warning("Webchat: clientes no responde (%s); el contacto queda en la cola", e)
        self.enqueue(contact)
        return None

    # --- cola ---
    def enqueue(self, contact: dict) -> Path:
        self.queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = uuid.uuid4().hex
        tmp = self.queue_dir / f"{name}.tmp"
        final = self.queue_dir / f"{name}.json"
        data = json.dumps({k: contact.get(k, "") for k in FIELDS}, ensure_ascii=False).encode("utf-8")
        # Se crea ya con 600 (lleva datos de una persona) y se renombra al final: el reintento nunca ve uno a medias.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return final

    def _set_aside(self, path: Path, suffix: str) -> None:
        try:
            path.replace(path.with_name(path.name + suffix))
        except OSError as e:
            log.error("Webchat: no se puede apartar un fichero de la cola (%s)", type(e).__name__)

    def flush(self, on_delivered: Callable[[int, bool], None] | None = None) -> int:
        """Intenta entregar lo que hay en la cola; devuelve cuántos han llegado. Si `clientes` sigue caído, para en
        el primero (los demás esperarán al siguiente minuto)."""
        if not self.queue_dir.is_dir():
            return 0
        delivered = 0
        for path in sorted(self.queue_dir.glob("*.json"), key=_age):
            try:
                contact = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(contact, dict):
                    raise ValueError("no es un contacto")
            except (OSError, ValueError) as e:
                log.warning("Webchat: fichero de la cola ilegible (%s); se aparta", type(e).__name__)
                self._set_aside(path, ".corrupto")
                continue
            try:
                result = self._post(contact)
            except ClientesRejected as e:
                # No se arreglará solo: se aparta para que Marc lo mire, sin bloquear la cola.
                log.warning("Webchat: clientes rechaza un contacto de la cola (campo %s); se aparta", e.field)
                self._set_aside(path, ".rechazado")
                continue
            except _Failed as e:
                log.warning("Webchat: clientes sigue sin responder (%s); se reintentará", e)
                break
            path.unlink(missing_ok=True)
            delivered += 1
            if on_delivered is not None:
                on_delivered(*result)
        return delivered


def _age(path: Path) -> float:
    # Los más antiguos primero; si uno desaparece mientras se mira, da igual su sitio.
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _field(error: urllib.error.HTTPError) -> str:
    try:
        field = json.loads(error.read()).get("field")
    except (OSError, ValueError, AttributeError):
        field = None
    return field if field in VISITOR_FIELDS else "peticion"


def migrate_old_contacts(db_path: Path | str, link: ClientesLink) -> int:
    """Migración única (0.63): los contactos que guardaba el chat en su propia base pasan a la cola de clientes y se
    borra el fichero viejo. Se encolan (no se entregan aquí) para que un `clientes` caído no pare el arranque: el
    reintento los entrega. Si se corta a medias, repetirla solo duplicaría mensajes, nunca clientes (mismo correo)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    db = sqlite3.connect(db_path)
    try:
        has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'contacts'").fetchone()
        if not has_table:
            return 0
        rows = db.execute("SELECT name, email, need, lang FROM contacts ORDER BY id").fetchall()
        for name, email, need, lang in rows:
            link.enqueue({"name": name, "email": email, "phone": "", "need": need, "lang": lang})
        db.execute("DROP TABLE contacts")
        db.commit()
    finally:
        db.close()
    # El fichero solo tenía esa tabla: se borra entero (con sus restos de diario) para no dejar correos en disco.
    for leftover in (db_path, Path(f"{db_path}-journal"), Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        leftover.unlink(missing_ok=True)
    log.info("Webchat: %d contactos antiguos pasados a la cola de clientes", len(rows))
    return len(rows)


def notify(notifier, result: tuple[int, bool] | None) -> None:
    """Aviso al móvil tras un contacto. Nunca falla hacia fuera: el contacto ya está a salvo (en clientes o en cola)."""
    from .store import QUEUED_NOTICE, notice_text

    if notifier is None:
        return
    try:
        if result is None:
            sent = notifier.send(QUEUED_NOTICE)
        else:
            client_id, nuevo = result
            text = notice_text(client_id, nuevo)
            sent = notifier.send(text, destination="clientes", client=client_id)
            if not sent:
                # Un núcleo sin ARTHUR_CLIENTES_PUBLIC_URL rechaza el destino: mejor el aviso sin enlace que ninguno.
                log.warning("Webchat: el aviso con ficha no ha entrado; se manda sin enlace")
                sent = notifier.send(text)
    except Exception as e:  # noqa: BLE001 — un aviso que falla no puede tumbar la respuesta a la visita
        log.error("Webchat: el aviso del contacto ha fallado (%s)", type(e).__name__)
        return
    if not sent:
        log.error("Webchat: el aviso del contacto no se ha enviado")
