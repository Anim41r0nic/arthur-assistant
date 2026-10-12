"""Aparatos que pueden hablar con el ARTHUR del servidor (arthur-core), cada uno con su propia llave.

- `add("cachyos")` crea una llave nueva y la devuelve UNA vez: se copia al aparato y aquí solo queda su huella
  (sha256). Quien lea `devices.json` no puede usarla para entrar.
- `check(llave)` dice de qué aparato es (o None). Compara las huellas en tiempo constante.
- `revoke("cachyos")` quita el acceso a ese aparato sin tocar los demás.

Las llaves son aleatorias de 256 bits (`secrets`): no hace falta un hash lento como con las contraseñas.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")  # p. ej. «cachyos», «iphone», «pc-salon»
MAX_DEVICES = 20


class DeviceError(Exception):
    pass


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


class DeviceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def add(self, name: str) -> str:
        """Da de alta un aparato y devuelve su llave (solo esta vez)."""
        if not NAME_PATTERN.match(name):
            raise DeviceError("El nombre del aparato: minúsculas, números y guiones (máx. 32), p. ej. «cachyos»")
        with self._lock:
            devices = self._load()
            if any(d["name"] == name for d in devices):
                raise DeviceError(f"Ya hay un aparato llamado «{name}». Revócalo antes o usa otro nombre.")
            if len(devices) >= MAX_DEVICES:
                raise DeviceError(f"Ya hay {MAX_DEVICES} aparatos: revoca alguno antes de añadir otro.")
            token = "arthur_" + secrets.token_urlsafe(32)
            devices.append({"name": name, "hash": _digest(token),
                            "created": datetime.now().isoformat(timespec="minutes")})
            self._save(devices)
            return token

    def revoke(self, name: str) -> bool:
        with self._lock:
            devices = self._load()
            kept = [d for d in devices if d["name"] != name]
            if len(kept) == len(devices):
                return False
            self._save(kept)
            return True

    def names(self) -> list[tuple[str, str]]:
        """(nombre, fecha de alta) de cada aparato. Nunca las huellas."""
        with self._lock:
            return [(d["name"], d.get("created", "?")) for d in self._load()]

    def check(self, token: str | None) -> str | None:
        """El nombre del aparato dueño de esta llave, o None si no vale."""
        if not token or len(token) > 200:
            return None
        digest = _digest(token)
        with self._lock:
            devices = self._load()
        found = None
        for d in devices:  # se recorren todas siempre: el tiempo no dice cuál (ni si alguna) coincidió
            if hmac.compare_digest(d["hash"], digest):
                found = d["name"]
        return found

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise DeviceError(f"No puedo leer {self.path}: {e}") from e
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, list):
            raise DeviceError(f"{self.path} no tiene el formato esperado")
        return [d for d in devices
                if isinstance(d, dict) and isinstance(d.get("name"), str) and isinstance(d.get("hash"), str)]

    def _save(self, devices: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        # Se crea ya con permisos 600 (nunca hay un instante en que otros puedan leerlo) y se cambia de golpe.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"devices": devices}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
