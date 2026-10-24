"""El día completo: junta tareas, Obsidian, calendario y correo en el diccionario de GET /api/today.

Una fuente en None (sin configurar) se salta sin avisar; si falla (SourceError) su texto va a `problems`
y el resto sigue: una tarjeta caída no debe dejar la pantalla en blanco.

`repaso_fn` da la línea del Repaso (cerebro/relay.repaso_line) o None; si el cerebro no contesta no sale nada y
tampoco va a `problems`: el Repaso no es parte del día que haya que arreglar.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .sources import SourceError
from .sources.google import Event, Mail, important_mail, today_events
from .sources.studio import sync_studio
from .store import PanelStore, Task


def _evento(e: Event) -> dict:
    return {"title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat(), "all_day": e.all_day}


def _correo(m: Mail) -> dict:
    return {"id": m.id, "sender": m.sender, "subject": m.subject, "link": m.link, "snippet": m.snippet}


def _tarea(t: Task) -> dict:
    return {"id": f"{t.source}-{t.id}", "title": t.title, "source": t.source,
            "done": t.done_at is not None, "due": t.due}


def _proximos(events: list[Event], now: datetime) -> dict:
    """Solo cuentan los eventos con hora: uno de día entero no «está pasando» a una hora concreta."""
    con_hora = sorted((e for e in events if not e.all_day), key=lambda e: e.start)
    actual = next((e for e in con_hora if e.start <= now < e.end), None)
    siguiente = next((e for e in con_hora if e.start > now), None)
    return {"current": _evento(actual) if actual else None,
            "following": _evento(siguiente) if siguiente else None}


def build_today(*, store: PanelStore, vault, calendar, gmail, studio_link, now: datetime,
                repaso_fn: Callable[[], dict | None] | None = None) -> dict:
    day = now.date()
    problems: list[str] = []

    def intentar(fuente, vacio):
        try:
            return fuente()
        except SourceError as e:
            problems.append(str(e))
            return vacio

    # Antes de leer las tareas, para que las de Studio recién creadas salgan en esta misma carga.
    if studio_link is not None:
        intentar(lambda: sync_studio(studio_link, store, day), 0)

    propias = [_tarea(t) for t in store.tasks_for(day)]
    obsidian = []
    if vault is not None:
        for c in intentar(vault.checkboxes, []):
            obsidian.append({"id": f"obsidian-{c.file}:{c.line}", "title": c.text, "source": "obsidian",
                             "done": c.done, "due": None, "file": c.file, "line": c.line})
    todas = propias + obsidian
    # Pendientes primero, luego hechas; dentro de cada grupo se conserva el orden de origen.
    tareas = [t for t in todas if not t["done"]] + [t for t in todas if t["done"]]

    events = intentar(lambda: today_events(calendar, day), []) if calendar is not None else []
    mail = intentar(lambda: important_mail(gmail), []) if gmail is not None else []

    hechas = sum(1 for t in tareas if t["done"])
    total = len(tareas)
    return {
        "date": day.isoformat(),
        "now": now.isoformat(),
        "next": _proximos(events, now),
        "tasks": tareas,
        "progress": {"done": hechas, "total": total, "percent": round(100 * hechas / total) if total else 0},
        "events": [_evento(e) for e in events],
        "mail": [_correo(m) for m in mail],
        "problems": problems,
        "repaso": repaso_fn() if repaso_fn is not None else None,
    }
