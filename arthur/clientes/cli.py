"""Las órdenes de clientes (`arthur --clientes-web`, `--clientes-dispositivo-nuevo`, `--clientes-llave-webchat`).

Separado de `__main__` para poder probar el montaje sin arrancar el servidor. Todo lo de clientes vive en una sola
carpeta (en el contenedor, el volumen /data): la base `clientes.sqlite3`, `devices.json` (huellas de las llaves de
los aparatos) y `webchat.key` (huella de la llave del chat de la web).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_PORT = 8777
PURGE_EVERY = 24 * 3600
# «webchat» es la llave especial de la ruta interna (webchat.key), no un aparato de la página.
RESERVED_DEVICES = ("webchat",)

log = logging.getLogger("arthur.clientes")


def _now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid"))


def clientes_dir(env: Mapping[str, str]) -> Path:
    # Se lee del `env` recibido (no de os.environ) para poder probarlo sin tocar el entorno real.
    if env.get("ARTHUR_CLIENTES_DATA"):
        return Path(env["ARTHUR_CLIENTES_DATA"])
    base = env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "arthur" / "clientes"


@dataclass
class Settings:
    host: str
    port: int
    data: Path


def runtime_settings(env: Mapping[str, str]) -> Settings:
    return Settings(host=env.get("ARTHUR_CLIENTES_HOST", "127.0.0.1"),
                    port=int(env.get("ARTHUR_CLIENTES_PORT", str(DEFAULT_PORT))),
                    data=clientes_dir(env))


def new_device(name: str, env: Mapping[str, str], out=print) -> int:
    from ..security.devices import DeviceError, DeviceRegistry

    if name in RESERVED_DEVICES:
        print(f"✗ «{name}» está reservado para el chat de la web; usa otro nombre (p. ej. «movil»).",
              file=sys.stderr)
        return 1
    try:
        key = DeviceRegistry(clientes_dir(env) / "devices.json").add(name)
    except DeviceError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    out(f"Llave de «{name}» para la página de clientes (se enseña SOLO esta vez; pégala al entrar):\n\n{key}\n")
    return 0


def webchat_key_command(env: Mapping[str, str]) -> int:
    """Crea (o rota) la llave del chat de la web e imprime SOLO la llave, en una línea: así se puede mandar tal
    cual a un fichero (`… --clientes-llave-webchat > secrets/clientes_webchat`). La anterior deja de valer."""
    from .web import WebchatKey

    key = WebchatKey(clientes_dir(env) / "webchat.key").rotate()
    print(key)
    return 0


def clientes_command(args, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    if args.clientes_dispositivo_nuevo:
        return new_device(args.clientes_dispositivo_nuevo, env)
    if args.clientes_llave_webchat:
        return webchat_key_command(env)
    if args.clientes_web:
        return _serve(env)
    return 0


def _purge_loop(store, stop: threading.Event) -> None:
    # Las fichas sin contacto en 365 días se borran (privacidad), salvo los clientes: al arrancar y cada 24 h.
    while True:
        try:
            n = store.purge()
            log.info("Clientes: purga hecha (%d fichas antiguas borradas)", n)
        except Exception:  # noqa: BLE001 — un fallo de la purga no puede parar el hilo ni la página
            log.exception("Clientes: la purga ha fallado")
        if stop.wait(PURGE_EVERY):
            return


def _serve(env: Mapping[str, str]) -> int:
    from ..security.devices import DeviceRegistry
    from .store import ClientStore
    from .web import ClientesWeb, WebchatKey

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = runtime_settings(env)
    store = ClientStore(settings.data / "clientes.sqlite3", _now)
    webchat = WebchatKey(settings.data / "webchat.key")
    if not webchat.path.exists():
        log.warning("Clientes: sin llave de webchat (arthur --clientes-llave-webchat): el chat no puede entregar")
    web = ClientesWeb(store, DeviceRegistry(settings.data / "devices.json"), webchat,
                      host=settings.host, port=settings.port)
    stop = threading.Event()
    threading.Thread(target=_purge_loop, args=(store, stop), daemon=True).start()
    print(f"Clientes en {web.url} (Ctrl+C para parar)", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        web.shutdown()
        store.close()
    return 0
