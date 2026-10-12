"""Conexión con Google: leer calendario y Gmail, y crear eventos. Librería estándar, sin dependencias.

Cómo funciona la autorización (OAuth 2.0 para aplicaciones de escritorio):
  1. ARTHUR abre en el navegador la página de Google donde TÚ inicias sesión y
     aceptas los permisos (p. ej. "ver tu calendario").
  2. Google devuelve al navegador a http://127.0.0.1:<puerto>/ con un código de un
     solo uso, que recoge un mini servidor de ARTHUR que solo vive esos segundos.
  3. ARTHUR cambia ese código por un "refresh token": un permiso revocable que se
     guarda CIFRADO en tu llavero (gnome-keyring), nunca en un fichero.
  4. Con él pide permisos temporales de 1 hora cada vez que lo necesita.

En un equipo sin pantalla (el servidor, fase S5): nada escucha en 127.0.0.1 de tu navegador, así que la página de
vuelta no carga; copias su dirección (lleva el código) y la pegas en la terminal del servidor (`paste`).

Protecciones: PKCE (el código no sirve sin un secreto que solo conoce ARTHUR),
"state" contra peticiones falsificadas, y direcciones de Google fijas en el código.
Para retirar el permiso: https://myaccount.google.com/permissions
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from ..core.config import default_config_path

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
# Crear eventos. Google no tiene un permiso «solo crear»: este también permitiría borrar o cambiar eventos,
# pero el código de ARTHUR solo sabe crear (y solo con los campos de CalendarClient.create_event).
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"  # leer, nunca enviar ni borrar
SCOPES = [CALENDAR_SCOPE, CALENDAR_EVENTS_SCOPE, GMAIL_SCOPE]
RECONNECT = "Ejecuta: arthur --conectar-google"
EVENT_FIELDS = {"summary", "location", "start", "end"}  # lo único que ARTHUR escribe en un evento
EVENT_ID = re.compile(r"^[a-v0-9_]{5,100}$")  # ids de Google: base32hex en minúsculas (+ «_» en repeticiones)
KEYRING_ATTRS = ("service", "arthur", "account", "google")
MANUAL_PORT = 53682  # sin pantalla: la vuelta a 127.0.0.1 no carga (nada escucha); Google admite cualquier puerto


class GoogleError(Exception):
    pass


class NotConnected(GoogleError):
    pass


class NotFound(GoogleError):
    """El evento (o correo) no existe, o ya se borró."""


class Changed(GoogleError):
    """Alguien ha cambiado el evento entre que lo viste y que se aplicó tu orden."""


class MissingScope(GoogleError):
    """El permiso guardado no incluye lo necesario (p. ej. se conectó antes de existir «crear eventos»)."""


def client_file() -> Path:
    return default_config_path().parent / "google-client.json"


# --- Llavero ---------------------------------------------------------------

class Keyring:
    """Guarda el permiso de Google en gnome-keyring con secret-tool (paquete libsecret)."""

    def store(self, secret: str) -> None:
        subprocess.run(["secret-tool", "store", "--label=ARTHUR · permiso de Google", *KEYRING_ATTRS],
                       input=secret, text=True, check=True, capture_output=True, timeout=30)

    def load(self) -> str | None:
        proc = subprocess.run(["secret-tool", "lookup", *KEYRING_ATTRS], capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() or None

    def clear(self) -> None:
        subprocess.run(["secret-tool", "clear", *KEYRING_ATTRS], capture_output=True, timeout=30)


class FileKeyring:
    """El permiso de Google en un fichero 600 (carpeta 700). Para el servidor (arthur-core en Docker), que no
    tiene gnome-keyring. El fichero vive en el volumen de datos del contenedor, que no sale del servidor y
    entra en su copia de seguridad; quien tenga acceso de administrador al servidor podría leerlo."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def store(self, secret: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret)
        os.replace(tmp, self.path)

    def load(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def default_keyring():
    """gnome-keyring en el escritorio; con ARTHUR_KEYRING=file (lo pone la imagen Docker), un fichero."""
    if os.environ.get("ARTHUR_KEYRING") == "file":
        from ..core.config import data_dir

        return FileKeyring(data_dir() / "google-token")
    return Keyring()


# --- Autorización ----------------------------------------------------------

class GoogleAuth:
    def __init__(
        self,
        client_path: Path | None = None,
        keyring: Keyring | None = None,
        *,
        token_url: str = TOKEN_URL,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.client_path = client_path or client_file()
        self.keyring = keyring or default_keyring()
        self.token_url = token_url
        self._opener = opener or urllib.request.build_opener()
        self._access: tuple[str, float] | None = None
        self._scope: str | None = None  # permisos que Google dice que tiene el token (si lo ha dicho)
        self._lock = threading.Lock()

    def _client(self) -> dict[str, str]:
        try:
            data = json.loads(self.client_path.read_text())["installed"]
            return {"client_id": data["client_id"], "client_secret": data["client_secret"]}
        except FileNotFoundError:
            raise NotConnected(f"Falta {self.client_path}: sigue la guía de conexión con Google") from None
        except (KeyError, ValueError) as e:
            raise GoogleError(f"{self.client_path} no es un cliente OAuth de tipo «Aplicación de escritorio»") from e

    def connected(self) -> bool:
        return self.client_path.exists() and self.keyring.load() is not None

    def authorize(self, scopes: list[str], open_browser: Callable[[str], None], timeout: int = 300,
                  paste: Callable[[str], str] | None = None) -> None:
        """Flujo completo en el navegador. Bloquea hasta que aceptas (o hasta `timeout` s).
        `paste`: sin pantalla (servidor). Se le pide al usuario la dirección a la que volvió su navegador."""
        client = self._client()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        result: dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
                if query.get("state") != state:
                    self.send_response(400)
                    self.end_headers()
                    return
                result.update(query)
                body = ("<meta charset='utf-8'><h2>ARTHUR ya tiene permiso. Puedes cerrar esta pestaña.</h2>"
                        if "code" in query else "<meta charset='utf-8'><h2>No se ha concedido el permiso.</h2>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())

        if paste is None:
            server = HTTPServer(("127.0.0.1", 0), Callback)  # solo este equipo, puerto libre al azar
            redirect = f"http://127.0.0.1:{server.server_port}"
            server.timeout = 1
        else:
            server, redirect = None, f"http://127.0.0.1:{MANUAL_PORT}"
        params = {
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(scopes), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
        }
        open_browser(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")
        if server is None:
            answer = paste("Pega aquí la dirección completa de la barra del navegador (empieza por "
                           f"{redirect}/?…): ").strip()
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(answer).query))
            if query.get("state") != state:  # otra pestaña, otro intento o algo falsificado: no se usa
                raise GoogleError("Esa dirección no es la de esta conexión. Vuelve a empezar y pega la nueva.")
            result.update(query)
        else:
            deadline = time.monotonic() + timeout
            try:
                while not result and time.monotonic() < deadline:
                    server.handle_request()
            finally:
                server.server_close()
        if "code" not in result:
            raise GoogleError(f"No se ha concedido el permiso ({result.get('error', 'tiempo agotado')})")
        tokens = self._post_token({
            **client, "code": result["code"], "code_verifier": verifier,
            "grant_type": "authorization_code", "redirect_uri": redirect,
        })
        if "refresh_token" not in tokens:
            raise GoogleError("Google no ha devuelto un permiso duradero; vuelve a intentarlo")
        self.keyring.store(tokens["refresh_token"])
        self._access = (tokens["access_token"], time.time() + int(tokens.get("expires_in", 3600)) - 60)
        self._scope = tokens.get("scope")

    def disconnect(self) -> None:
        self.keyring.clear()
        self._access = None
        self._scope = None

    def lacks_scope(self, scope: str) -> bool:
        """True solo si Google ha dicho qué permisos tiene el token y falta este (así no se envía nada en vano)."""
        return self._scope is not None and scope not in self._scope.split()

    def access_token(self) -> str:
        with self._lock:
            if self._access and time.time() < self._access[1]:
                return self._access[0]
            refresh = self.keyring.load()
            if not refresh:
                raise NotConnected(f"ARTHUR no está conectado a Google. {RECONNECT}")
            tokens = self._post_token({**self._client(), "refresh_token": refresh, "grant_type": "refresh_token"})
            self._access = (tokens["access_token"], time.time() + int(tokens.get("expires_in", 3600)) - 60)
            self._scope = tokens.get("scope")
            return self._access[0]

    def _post_token(self, fields: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(self.token_url, data=urllib.parse.urlencode(fields).encode(), method="POST")
        try:
            with self._opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            try:
                error = json.load(e).get("error", "")
            finally:
                e.close()
            if error == "invalid_grant":
                raise NotConnected("El permiso de Google ha caducado o se ha retirado. "
                                   "Ejecuta: arthur --conectar-google") from e
            raise GoogleError(f"Google ha rechazado la petición ({e.code} {error})") from e
        except urllib.error.URLError as e:
            raise GoogleError(f"No puedo conectar con Google: {e.reason}") from e


# --- Calendario ------------------------------------------------------------

class GoogleApi:
    def __init__(self, auth: GoogleAuth, *, api_url: str = API_URL,
                 opener: urllib.request.OpenerDirector | None = None) -> None:
        self.auth = auth
        self.api_url = api_url
        self._opener = opener or urllib.request.build_opener()

    def _get(self, path: str, params: dict[str, str] | list[tuple[str, str]] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params)

    def _request(self, method: str, path: str, params: dict[str, str] | list[tuple[str, str]] | None = None,
                 body: dict[str, Any] | None = None, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self.api_url}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        headers = {"Authorization": f"Bearer {self.auth.access_token()}", **(extra_headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw.strip() else {}  # un borrado correcto responde 204, sin cuerpo
        except urllib.error.HTTPError as e:
            detail = e.read(4096).decode("utf-8", errors="replace")
            e.close()
            if e.code == 403 and method != "GET" and ("insufficientPermissions" in detail
                                                      or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in detail):
                raise MissingScope(f"El permiso de Google que tiene ARTHUR no deja hacer esto. {RECONNECT}") from e
            if e.code in (404, 410):
                raise NotFound(f"Google ha respondido con error {e.code} (no existe)") from e
            if e.code == 412:
                raise Changed("El evento ha cambiado desde que lo viste; no he tocado nada. Vuelve a pedirlo") from e
            if e.code == 403:
                raise GoogleError("Google no deja a ARTHUR leer esto: activa la API en tu proyecto de Google Cloud "
                                  "y vuelve a ejecutar «arthur --conectar-google» para dar el permiso") from e
            raise GoogleError(f"Google ha respondido con error {e.code}") from e
        except TimeoutError as e:
            if method == "GET":
                raise GoogleError("Google no ha contestado a tiempo") from e
            # Escritura: la petición pudo llegar. Nunca se repite sola (saldría el evento dos veces).
            raise GoogleError("Google no ha contestado a tiempo y no sé si se ha guardado: mira tu calendario "
                              "antes de volver a pedirlo") from e
        except urllib.error.URLError as e:
            raise GoogleError(f"No puedo conectar con Google: {e.reason}") from e


class CalendarClient(GoogleApi):
    def events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Eventos de todos tus calendarios visibles entre `start` y `end`, ordenados por hora."""
        calendars = [c for c in self._get("/calendar/v3/users/me/calendarList").get("items", [])
                     if c.get("selected", True)]
        events = []
        for cal in calendars:
            data = self._get(
                f"/calendar/v3/calendars/{urllib.parse.quote(cal['id'], safe='')}/events",
                {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                 "orderBy": "startTime", "maxResults": "100"},
            )
            for item in data.get("items", []):
                if item.get("status") == "cancelled":
                    continue
                item["_calendar"] = cal.get("summaryOverride") or cal.get("summary", "")
                item["_primary"] = bool(cal.get("primary"))  # solo en tu calendario principal se puede editar
                events.append(item)
        return sorted(events, key=lambda e: e["start"].get("dateTime") or e["start"].get("date", ""))

    def _events_write_check(self, event: dict[str, Any] | None = None) -> None:
        """Antes de escribir: solo los campos conocidos (nada de invitados) y con el permiso de eventos."""
        extra = (event or {}).keys() - EVENT_FIELDS
        if extra:
            raise GoogleError(f"Campos de evento no permitidos: {', '.join(sorted(extra))}")
        self.auth.access_token()  # refresca el permiso y averigua qué incluye
        if self.auth.lacks_scope(CALENDAR_EVENTS_SCOPE):
            raise MissingScope(f"ARTHUR todavía no tiene permiso para cambiar eventos. {RECONNECT}")

    @staticmethod
    def _event_path(event_id: str) -> str:
        if not EVENT_ID.match(event_id):  # el id va dentro de la dirección: solo letras/números de Google
            raise GoogleError("Identificador de evento no válido")
        return f"/calendar/v3/calendars/primary/events/{event_id}"

    def create_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Crea UN evento en tu calendario principal. Sin invitar a nadie.

        Solo se aceptan estos campos (así un fallo aguas arriba no puede colar invitados ni otras opciones),
        y `sendUpdates=none` hace que Google no envíe correos a nadie.
        """
        self._events_write_check(event)
        return self._request("POST", "/calendar/v3/calendars/primary/events", {"sendUpdates": "none"}, event)

    def get_event(self, event_id: str) -> dict[str, Any]:
        """Un evento de tu calendario principal (solo lectura). NotFound si no existe o está borrado."""
        event = self._get(self._event_path(event_id))
        if event.get("status") == "cancelled":  # Google devuelve los borrados con este estado
            raise NotFound("Ese evento ya está borrado")
        return event

    def patch_event(self, event_id: str, changes: dict[str, Any], etag: str) -> dict[str, Any]:
        """Cambia SOLO los campos indicados de un evento de tu calendario principal.

        `If-Match` con la versión (etag) que viste al confirmar: si alguien lo cambió entretanto,
        Google rechaza el cambio (412) en vez de pisarlo. Sin avisos a nadie (`sendUpdates=none`).
        """
        self._events_write_check(changes)
        if not changes or not etag:
            raise GoogleError("Falta qué cambiar o la versión del evento")
        return self._request("PATCH", self._event_path(event_id), {"sendUpdates": "none"}, changes,
                             {"If-Match": etag})

    def delete_event(self, event_id: str, etag: str) -> None:
        """Borra UN evento de tu calendario principal (con la misma protección de versión que patch_event)."""
        self._events_write_check()
        if not etag:
            raise GoogleError("Falta la versión del evento")
        self._request("DELETE", self._event_path(event_id), {"sendUpdates": "none"}, None, {"If-Match": etag})


def day_range(day: date, days: int = 1) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time()).astimezone()
    return start, start + timedelta(days=days)


# --- Gmail (solo lectura) ----------------------------------------------------

def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _plain_text(part: dict) -> str:
    """Busca el texto plano de un correo (recorriendo sus partes) y lo decodifica."""
    if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
        data = part["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    for sub in part.get("parts", []) or []:
        text = _plain_text(sub)
        if text:
            return text
    return ""


class GmailClient(GoogleApi):
    def list(self, query: str = "", max_results: int = 10) -> list[dict[str, str]]:
        found = self._get("/gmail/v1/users/me/messages", {"q": query, "maxResults": str(max_results)})
        result = []
        for ref in found.get("messages", []):
            message = self._get(f"/gmail/v1/users/me/messages/{ref['id']}",
                                [("format", "metadata"), ("metadataHeaders", "From"),
                                 ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])
            result.append({
                "id": message["id"],
                "from": _header(message, "From"),
                "subject": _header(message, "Subject"),
                "date": _header(message, "Date"),
                "unread": "UNREAD" in message.get("labelIds", []),
            })
        return result

    def sender_and_subject(self, message_id: str) -> dict[str, str]:
        """Solo las cabeceras De y Asunto (nunca el texto): lo justo para preparar una respuesta."""
        message = self._get(f"/gmail/v1/users/me/messages/{message_id}",
                            [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")])
        return {"from": _header(message, "From"), "subject": _header(message, "Subject")}

    def read(self, message_id: str) -> dict[str, str]:
        message = self._get(f"/gmail/v1/users/me/messages/{message_id}", {"format": "full"})
        return {
            "from": _header(message, "From"),
            "subject": _header(message, "Subject"),
            "date": _header(message, "Date"),
            "text": _plain_text(message.get("payload", {})) or message.get("snippet", ""),
        }
