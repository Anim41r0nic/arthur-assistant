"""El juego de la revisión: puntos con topes, niveles sin límite, racha sin deuda e insignias.

Las reglas salen de la propuesta de Codex (`arthur-marca`, rama `codex/juego`, `juego/JUEGO.md`), con lo que decidió
Marc el 18-10-2026: nada se resta nunca, no hay avisos de miedo y todo se puede ocultar. No hay cajas Leitner (el
Repaso ya usa FSRS) ni «Dudas» con Gemini (ya no se usa), así que el máximo diario es 50 + 30 + 10 = 90 pt.

Todo son funciones puras con enteros: aquí no hay reloj, base de datos ni red, para poder probar cada regla.
"""

from __future__ import annotations

from datetime import date, timedelta
from math import isqrt

PT_REPASO, MAX_REPASOS = 10, 5   # cada pregunta de la sesión diaria, hasta 5 al día
PT_TAREA, MAX_TAREAS = 10, 3     # cada tarea del plan o bloque de estudio ✓, hasta 3 al día
PT_COMPLETO = 10                 # todo el plan hecho y la sesión del Repaso hecha, una vez al día
SESION = 10                      # preguntas de la sesión diaria del Repaso (si solo se sabe que se hizo)


# ── Niveles (JUEGO §3): cada salto cuesta un punto más que el anterior y no hay tope ─────────────────────────────

def cost(n: int) -> int:
    """Puntos para pasar del nivel n al n + 1."""
    return n + 99


def threshold(n: int) -> int:
    """Puntos acumulados para entrar en el nivel n. La división es exacta: uno de los dos factores es par."""
    return (n - 1) * (n + 198) // 2


def rank(n: int) -> int:
    """El número de «Explorador N»: cambia cada diez niveles, sin lista de nombres que se acabe."""
    return 1 + (n - 1) // 10


def level_for(points: int) -> dict:
    """Nivel con `points` puntos y el avance dentro de su tramo. Con enteros (isqrt) para que no se pierda precisión
    ni con muchísimos puntos: T(n) ≤ P ⇔ n² + 197n − (198 + 2P) ≤ 0."""
    p = max(0, int(points))
    n = max(1, (isqrt(197 * 197 + 4 * (198 + 2 * p)) - 197) // 2)
    while threshold(n + 1) <= p:   # la raíz entera puede quedarse a uno de distancia: se ajusta
        n += 1
    while n > 1 and threshold(n) > p:
        n -= 1
    return {"puntos": p, "nivel": n, "rango": rank(n), "avance": p - threshold(n), "coste": cost(n),
            "siguiente_en": threshold(n + 1)}


# ── Puntos de un día (JUEGO §2 adaptado) ─────────────────────────────────────────────────────────────────────────

def day_points(*, repaso_preguntas: int | None, repaso_hecha: bool | None, validas: int, completo: bool) -> dict:
    """`validas` = tareas del plan hechas + bloques de estudio ✓ (½ no da puntos: sí hace el día activo).
    Si del Repaso solo se sabe que la sesión se hizo, cuenta la sesión entera."""
    preguntas = repaso_preguntas if repaso_preguntas is not None else (SESION if repaso_hecha else 0)
    repaso = PT_REPASO * min(MAX_REPASOS, max(0, preguntas))
    tareas = PT_TAREA * min(MAX_TAREAS, max(0, validas))
    bono = PT_COMPLETO if completo else 0
    return {"repaso": repaso, "tareas": tareas, "bono": bono, "total": repaso + tareas + bono}


# ── Racha (JUEGO §4 + pregunta 6) ────────────────────────────────────────────────────────────────────────────────

def _week(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return iso[0], iso[1]


def streak(days: dict[date, dict], today: date) -> dict:
    """Recorre las fechas desde la primera con datos hasta hoy. `days[fecha]` = {"activo": True/False/None, "pausa"}.

    - pausa → congelada; sin fila o activo None («por confirmar») → congelada: sin datos no es un fallo;
    - activo → +1;
    - no activo → si hay racha y queda el comodín de esa semana, protegido (+1); si no, vuelve a 0;
    - hoy, mientras no sea activo, no cambia nada (el día aún no ha terminado).
    """
    racha = activos = protegidos = total = 0
    usados: set[tuple[int, int]] = set()
    estados: dict[date, str] = {}
    d = min(days) if days else today
    while d <= today:
        info = days.get(d)
        if info is None or (info.get("activo") is None and not info.get("pausa")):
            estados[d] = "sin_datos"
        elif info.get("activo"):
            racha, activos, total = racha + 1, activos + 1, total + 1
            estados[d] = "activo"
        elif info.get("pausa"):
            estados[d] = "pausa"
        elif d == today:
            estados[d] = "hoy"
        elif racha > 0 and _week(d) not in usados:
            usados.add(_week(d))
            racha, protegidos = racha + 1, protegidos + 1
            estados[d] = "protegido"
        else:
            racha = activos = protegidos = 0
            estados[d] = "inactivo"
        d += timedelta(days=1)
    lunes = today - timedelta(days=today.weekday())
    semana = []
    for i in range(7):
        f = lunes + timedelta(days=i)
        semana.append({"fecha": f.isoformat(), "estado": "futuro" if f > today else estados.get(f, "sin_datos")})
    hoy = days.get(today) or {}
    return {"racha": racha, "activos": activos, "protegidos": protegidos, "dias_activos": total,
            "comodin_disponible": _week(today) not in usados, "en_pausa": bool(hoy.get("pausa")), "semana": semana}


# ── Insignias (JUEGO §7): las que se pueden comprobar con lo que guarda el panel ─────────────────────────────────

BADGES = (
    {"id": "primer-paso", "nombre": "Primer paso", "condicion": "Contestar 1 pregunta del Repaso.",
     "texto": "Ya tienes un punto de partida."},
    {"id": "diez-repasos", "nombre": "Diez encuentros", "condicion": "10 preguntas del Repaso acumuladas.",
     "texto": "Diez ocasiones para volver a una idea."},
    {"id": "cincuenta-repasos", "nombre": "Ideas que vuelven", "condicion": "50 preguntas del Repaso acumuladas.",
     "texto": "Has vuelto a las ideas cincuenta veces."},
    {"id": "cien-repasos", "nombre": "Cuaderno abierto", "condicion": "100 preguntas del Repaso acumuladas.",
     "texto": "Tu cuaderno sigue creciendo, a tu ritmo."},
    {"id": "corregir", "nombre": "Entender la corrección",
     "condicion": "Un día con alguna pregunta del Repaso fallada y revisada.",
     "texto": "La corrección también forma parte del camino."},
    {"id": "primera-tarea", "nombre": "Un pendiente menos", "condicion": "1 tarea del plan o bloque de estudio hecho.",
     "texto": "Un paso concreto que ya está hecho."},
    {"id": "diez-tareas", "nombre": "Pasos concretos", "condicion": "10 tareas del plan o bloques hechos.",
     "texto": "Diez compromisos pequeños terminados."},
    {"id": "primer-completo", "nombre": "Un día redondo",
     "condicion": "1 día completo: todo el plan y la sesión del Repaso.", "texto": "Hoy encajaron la práctica y tu plan."},
    {"id": "tres-activos", "nombre": "Tres días a tu ritmo", "condicion": "3 días activos, no hace falta seguidos.",
     "texto": "Tres días de práctica cuentan, estén donde estén."},
    {"id": "diez-activos", "nombre": "Constancia tranquila", "condicion": "10 días activos, no hace falta seguidos.",
     "texto": "Has encontrado diez momentos para avanzar."},
    {"id": "retomar", "nombre": "Retomar",
     "condicion": "Un día activo después de 2 o más días seguidos sin actividad o en pausa.",
     "texto": "Puedes volver desde donde estás."},
    {"id": "revisar-semana", "nombre": "Mirar la semana", "condicion": "Pulsar «Leído» en un resumen semanal.",
     "texto": "Mirar lo ocurrido también ayuda a elegir."},
)
BADGE_IDS = tuple(b["id"] for b in BADGES)


def badges(history: list[tuple[date, dict]], semana_leida: date | None) -> dict[str, date]:
    """Fecha en que se cumplió cada insignia, según el historial ordenado [(fecha, día)]. Un día lleva `activo`,
    `pausa`, `preguntas` (ya con el tope diario), `errores`, `validas` (con tope) y `completo`.
    Los huecos de fechas (sin fila) son «sin datos» y cortan la cuenta de «Retomar»."""
    got: dict[str, date] = {}
    preguntas = validas = activos = parados = 0
    anterior: date | None = None

    def cumple(badge: str, cond: bool, d: date) -> None:
        if cond and badge not in got:
            got[badge] = d

    for d, info in history:
        if anterior is not None and d - anterior > timedelta(days=1):
            parados = 0
        anterior = d
        preguntas += info.get("preguntas") or 0
        validas += info.get("validas") or 0
        activo = info.get("activo") is True
        cumple("primer-paso", preguntas >= 1, d)
        cumple("diez-repasos", preguntas >= 10, d)
        cumple("cincuenta-repasos", preguntas >= 50, d)
        cumple("cien-repasos", preguntas >= 100, d)
        cumple("corregir", (info.get("errores") or 0) > 0, d)
        cumple("primera-tarea", validas >= 1, d)
        cumple("diez-tareas", validas >= 10, d)
        cumple("primer-completo", info.get("completo") is True, d)
        if activo:
            activos += 1
            cumple("retomar", parados >= 2, d)
            parados = 0
        elif info.get("pausa") or info.get("activo") is False:
            parados += 1
        else:
            parados = 0
        cumple("tres-activos", activos >= 3, d)
        cumple("diez-activos", activos >= 10, d)
    if semana_leida is not None:
        got["revisar-semana"] = semana_leida
    return got
