"""Qué preguntas salen hoy, el modo libre, la racha y el % de aciertos.

Sesión diaria: primero las que toca repasar (las más atrasadas si hay muchas) y después hasta 5 nuevas, 10 en
total. La primera vez que se pide en el día se guarda la lista: recargar la página enseña solo las que faltan.
Una fallada vuelve al final de la misma sesión; cuando todas están acertadas, retiradas o falladas 3 veces, el
día queda hecho (lo comprueba el almacén: store.answer y store.retire).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .fsrs import retrievability
from .store import RepasoStore


def daily(store: RepasoStore, subjects: set[str], now: datetime, size: int = 10, max_new: int = 5) -> list[dict]:
    today = store.local_day(now)
    ids = store.session(today)
    if ids is None:
        ids = store.due_ids(subjects, now, size)
        ids += store.new_ids(subjects, min(max_new, size - len(ids)))
        if not ids:
            # No se fija una sesión vacía: si esta noche no hubo preguntas, las que lleguen luego aún salen hoy
            return []
        store.save_session(today, ids)
    # Primero las que aún no se han intentado; detrás, las falladas que tienen que volver
    return store.questions(store.session_pending(today))


def _inside(carpeta: list[str], folder_path: list[str]) -> bool:
    # Se compara por carpetas enteras: «Tema 2» no incluye «Tema 20»
    return carpeta[:len(folder_path)] == folder_path


def free(store: RepasoStore, folder_path: list[str], now: datetime) -> list[dict]:
    """Todas las preguntas de una asignatura o tema, sin límite, de más a menos urgente: primero las que ya
    tocan (las más atrasadas antes), luego las nuevas y al final las demás, la que más se va olvidando antes."""
    atrasadas, nuevas, resto = [], [], []
    for q in store.active_questions():
        if not _inside(q["carpeta"], folder_path):
            continue
        if q["nueva"]:
            nuevas.append(q)
        elif q["due"] <= now:
            atrasadas.append(q)
        else:
            resto.append((retrievability(store.card(q["id"]), now), q["id"], q))
    atrasadas.sort(key=lambda q: (q["due"], q["id"]))
    resto.sort(key=lambda t: t[:2])
    return atrasadas + nuevas + [q for _, _, q in resto]


def streak(store: RepasoStore, today: date) -> int:
    """Días seguidos con la sesión hecha. Si hoy aún no está hecha, la racha sigue viva (cuenta hasta ayer):
    solo se rompe cuando acaba un día sin hacerla. Un día sin ninguna pregunta que hacer (ni pendientes ni
    nuevas) también la rompe: no hubo sesión hecha, y así lo decidió Marc (2026-10-18) en vez de «congelarla».
    Se cuenta con fechas, no con horas, así que el día de 25 horas del cambio de hora es un día más."""
    done = store.done_days(today)
    d = today if today in done else today - timedelta(days=1)
    n = 0
    while d in done:
        n += 1
        d -= timedelta(days=1)
    return n


def first_daily(answers: list[dict]) -> list[dict]:
    """De `store.answers_since`, solo el primer intento de cada pregunta en su sesión diaria: el repaso libre y
    los reintentos de una fallada no cuentan para el % (si no, repetir lo que ya sabes lo inflaría)."""
    seen: set[tuple[int, str]] = set()
    out = []
    for a in answers:
        key = (a["question_id"], a["day"])
        if a["mode"] == "diaria" and key not in seen:
            seen.add(key)
            out.append(a)
    return out


def accuracy(store: RepasoStore, now: datetime, days: int = 30) -> dict[str, float]:
    """Fracción de aciertos (0-1) por asignatura en los últimos `days` días, con el primer intento de cada
    pregunta de la sesión diaria (`first_daily`)."""
    totals: dict[str, list[int]] = {}
    for a in first_daily(store.answers_since(now - timedelta(days=days))):
        t = totals.setdefault(a["asignatura"], [0, 0])
        t[0] += a["correct"]
        t[1] += 1
    return {asignatura: ok / n for asignatura, (ok, n) in sorted(totals.items())}
