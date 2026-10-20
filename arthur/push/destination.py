"""Dirección pública a la que puede abrir un aviso del panel.

La elige el administrador del núcleo, nunca el cuerpo de un aviso. Solo se acepta la raíz de una web HTTPS
o una dirección HTTP de Tailscale: así el clic no puede abrir una ruta, redirección o sitio arbitrario.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse

MAX_URL = 512
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def check_notice_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL or value != value.strip():
        raise ValueError("La dirección pública del panel no es válida")
    if any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in "\\?#"):
        raise ValueError("La dirección pública del panel no es válida")
    try:
        parts = urllib.parse.urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError("La dirección pública del panel no es válida") from None
    if (parts.scheme not in ("https", "http") or not host or port == 0 or parts.netloc.endswith(":")
            or "%" in parts.netloc
            or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("La dirección pública del panel debe ser la raíz de una web HTTPS o de Tailscale")
    if parts.scheme == "http":
        try:
            allowed = ipaddress.ip_address(host) in TAILSCALE
        except ValueError:
            allowed = host.endswith(".ts.net")
        if not allowed:
            raise ValueError("Sin HTTPS, el panel debe usar una dirección de Tailscale")
    # Sin ruta ni barra final: una sola forma canónica para el servicio y el navegador.
    return f"{parts.scheme}://{parts.netloc}"


# Las únicas «partes tras #» que puede llevar un aviso: la ficha de un cliente (`#c12`), un vídeo de Studio en su
# canal (`#v12-frutinovelas`) o la pestaña Repaso de Apuntes (`#repaso`, exacta). Cada página lee la suya para abrir esa ficha o ese vídeo; no son una ruta ni una
# consulta, así que no llevan a otro sitio. Sin ceros delante y como mucho 9 cifras, igual que el número que acepta el
# núcleo. Los perfiles se copian aquí a mano (studio/channels.py, ORDER): el contenedor de push solo lleva arthur/push/
# y un test comprueba que siguen siendo los mismos.
STUDIO_PROFILES = ("estoicismo_es", "estoicismo_en", "frutinovelas")
_NOTICE_FRAGMENT = re.compile(r"c[1-9][0-9]{0,8}|v[1-9][0-9]{0,8}-(?:" + "|".join(STUDIO_PROFILES) + ")|repaso")


def check_notice_target(value: str) -> str:
    """La dirección que abre un aviso ya resuelto por el núcleo: una raíz válida (check_notice_url) y, como mucho,
    `#c<número>` (la ficha de un cliente), `#v<número>-<perfil>` (un vídeo de Studio en su canal) o `#repaso`."""
    if not isinstance(value, str):
        raise ValueError("La dirección del aviso no es válida")
    root, sep, fragment = value.partition("#")
    if sep and not _NOTICE_FRAGMENT.fullmatch(fragment):
        raise ValueError("La dirección del aviso no es válida")
    root = check_notice_url(root)
    return f"{root}#{fragment}" if sep else root
