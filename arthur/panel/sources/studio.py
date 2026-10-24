"""Fuente Studio: cada corto publicado hace poco genera dos tareas para subirlo a Instagram y a TikTok.

Studio publica en YouTube solo; las otras dos redes las sube Marc a mano, y es fácil olvidarlo.
"""

from __future__ import annotations

import urllib.error
from datetime import date, datetime, timedelta

from ...tools.base import ToolError
from ..store import MAX_TITLE, PanelStore
from . import SourceError

REDES = (("instagram", "Instagram"), ("tiktok", "TikTok"))


def _titulo(red: str, title: str) -> str:
    # Un título largo de Studio no debe hacer fallar add_task (límite de 200): se recorta con «…».
    full = f"Subir a {red}: {' '.join(title.split())}"
    return full if len(full) <= MAX_TITLE else full[:MAX_TITLE - 1] + "…"


def _dia(created_at) -> date | None:
    try:
        return datetime.fromisoformat(str(created_at)).date()
    except ValueError:
        return None


def sync_studio(link, store: PanelStore, today: date) -> int:
    """Crea las tareas que falten; devuelve cuántas se crearon. Idempotente gracias a studio_ref."""
    try:
        data = link.call("/api/panel/shorts")
    except (ToolError, OSError, urllib.error.URLError):
        raise SourceError("Studio no disponible ahora") from None
    reviews = data.get("shorts") if isinstance(data, dict) else None
    if not isinstance(reviews, list):
        return 0
    created = 0
    for r in reviews:
        if not isinstance(r, dict):
            continue
        day = _dia(r.get("published_at"))
        if day is None or day not in (today, today - timedelta(days=1)):
            continue
        title = r.get("title")
        if r.get("id") is None or not isinstance(title, str) or not title.strip():
            continue
        for slug, red in REDES:
            ref = f"review-{r['id']}:{slug}"
            # add_task devuelve la existente si ya estaba; para contar solo las nuevas se mira antes.
            existed = store._q("SELECT 1 FROM tasks WHERE studio_ref = ?", (ref,))
            store.add_task(_titulo(red, title), day, source="studio", studio_ref=ref)
            if not existed:
                created += 1
    return created
