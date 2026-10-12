"""El enlace de arthur-core con arthur-push (fase S6): avisos al móvil aunque la web esté cerrada.

arthur-push es otro contenedor de la red interna de Docker (Dockerfile.push, push/push_server.py): cifra, firma
y entrega. Este módulo solo usa la biblioteca estándar: guarda qué aparato quiere avisos y le pide a arthur-push
que los mande.

Se configura con variables de entorno del contenedor del core:
  ARTHUR_PUSH_URL         p. ej. http://push:8772   (sin ella, el servidor funciona sin avisos al móvil)
  ARTHUR_PUSH_TOKEN_FILE  la llave compartida con arthur-push (fichero de secretos)

Las suscripciones (una por aparato) se guardan en push.json (600) junto a devices.json. Si un aparato se revoca,
su suscripción deja de usarse y se borra en el siguiente aviso; si el servicio de avisos dice que ya no vale
(404/410: p. ej. borraste la app del iPhone), también.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ..push.subscription import check_subscription
from .voz import check_voice_url

log = logging.getLogger(__name__)

TIMEOUT = 30
GONE = (404, 410)   # el servicio de avisos dice que esa suscripción ya no existe
MAX_SUBSCRIPTIONS = 20


class PushError(Exception):
    pass


class PushLink:
    def __init__(self, url: str, token: str, *, opener=None) -> None:
        try:
            self.url = check_voice_url(url)  # la misma regla que la voz: solo dentro de la red interna
        except ValueError:
            raise ValueError("ARTHUR_PUSH_URL debe ser una dirección de la red interna (p. ej. http://push:8772)") from None
        if len(token) < 32:
            raise ValueError("La llave de arthur-push es demasiado corta (mínimo 32 caracteres)")
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls, env) -> PushLink | None:
        url = env.get("ARTHUR_PUSH_URL", "").strip()
        if not url:
            return None
        path = env.get("ARTHUR_PUSH_TOKEN_FILE", "/run/secrets/push_token")
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ValueError(f"No puedo leer la llave de arthur-push ({path}): {e}") from e
        return cls(url, token)

    def _call(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data else "GET")
        req.add_header("Authorization", f"Bearer {self._token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                answer = json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                try:
                    detail = json.loads(e.read()).get("error", "")
                except (ValueError, AttributeError):
                    detail = ""
            raise PushError(detail or f"arthur-push respondió {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PushError(f"No puedo conectar con arthur-push: {getattr(e, 'reason', e)}") from None
        if not isinstance(answer, dict):
            raise PushError("arthur-push devolvió una respuesta rara")
        return answer

    def public_key(self) -> str:
        key = self._call("/key").get("key")
        if not isinstance(key, str) or not key:
            raise PushError("arthur-push devolvió una respuesta rara")
        return key

    def send(self, subscription: dict, title: str, body: str, *, ttl: int = 86400, urgency: str = "high") -> int:
        status = self._call("/send", {"subscription": subscription, "title": title, "body": body,
                                      "ttl": ttl, "urgency": urgency}).get("status")
        if not isinstance(status, int):
            raise PushError("arthur-push devolvió una respuesta rara")
        return status


class PushSubscriptions:
    """Aparato → su suscripción. Fichero 600, escrito de golpe (como devices.json)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def set(self, device: str, subscription: object) -> None:
        clean = check_subscription(subscription)  # SubscriptionError si no vale
        with self._lock:
            subs = self._load()
            if device not in subs and len(subs) >= MAX_SUBSCRIPTIONS:
                raise ValueError("demasiados aparatos con avisos")
            subs[device] = {**clean, "since": datetime.now().isoformat(timespec="minutes")}
            self._save(subs)

    def remove(self, device: str) -> bool:
        with self._lock:
            subs = self._load()
            if subs.pop(device, None) is None:
                return False
            self._save(subs)
            return True

    def all(self) -> dict[str, dict]:
        with self._lock:
            return self._load()

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("No puedo leer %s: %s", self.path, e)
            return {}
        subs = raw.get("subs") if isinstance(raw, dict) else None
        if not isinstance(subs, dict):
            return {}
        clean = {}
        for device, sub in subs.items():
            try:
                clean[device] = {**check_subscription(sub), "since": sub.get("since", "?")}
            except ValueError:
                continue  # una línea rara no tumba las demás
        return clean

    def _save(self, subs: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"subs": subs}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


class PushNotifier:
    """Manda un aviso a cada aparato suscrito (y aún dado de alta)."""

    def __init__(self, link: PushLink, subscriptions: PushSubscriptions, devices) -> None:
        self.link = link
        self.subscriptions = subscriptions
        self.devices = devices

    def notify(self, title: str, body: str, *, only: str | None = None) -> int:
        """Cuántos aparatos lo han recibido (el servicio de avisos lo aceptó). Nunca lanza: un aviso que no
        llega se anota y ya; no debe tumbar el recordatorio ni la petición que lo provocó."""
        alive = {name for name, _ in self.devices.names()}
        sent = 0
        for device, sub in self.subscriptions.all().items():
            if only is not None and device != only:
                continue
            if device not in alive:
                self.subscriptions.remove(device)  # aparato revocado: fuera también sus avisos
                log.info("Avisos: «%s» ya no está dado de alta; quito su suscripción", device)
                continue
            try:
                status = self.link.send({"endpoint": sub["endpoint"], "keys": sub["keys"]}, title, body[:1000])
            except PushError as e:
                log.warning("Avisos: no se pudo avisar a «%s»: %s", device, e)
                continue
            if status in GONE:
                self.subscriptions.remove(device)
                log.info("Avisos: la suscripción de «%s» ya no vale (%d); la quito", device, status)
            elif 200 <= status < 300:
                sent += 1
            else:
                log.warning("Avisos: el servicio de avisos respondió %d para «%s»", status, device)
        return sent

    def notify_later(self, title: str, body: str, *, only: str | None = None) -> None:
        """Lo mismo, en segundo plano (el que avisa no espera a internet)."""
        threading.Thread(target=self.notify, args=(title, body), kwargs={"only": only},
                         name="push", daemon=True).start()
