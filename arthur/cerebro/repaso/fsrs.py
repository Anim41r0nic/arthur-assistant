"""Repaso espaciado FSRS-6, sin librerías.

Cada pregunta es una «tarjeta» con dos números: la estabilidad (cuántos días aguanta el recuerdo hasta que la
probabilidad de acordarse baja al 90 %) y la dificultad (1-10). Al contestar, Marc pone una nota
(1 Otra vez · 2 Difícil · 3 Bien · 4 Fácil) y las fórmulas deciden cuándo vuelve a salir.

Es una copia en Python de las fórmulas de la implementación de referencia py-fsrs
(github.com/open-spaced-repetition/py-fsrs, licencia MIT, fsrs/scheduler.py, leída el 2026-10-17), con sus
parámetros por defecto y sin «fuzz» (el pequeño azar que mete en los intervalos): aquí cada repaso es
predecible y los tests pueden comprobarlo contra los valores publicados de la referencia.

Pasos cortos: una tarjeta nueva o fallada vuelve a los 10 minutos («Otra vez») hasta que se acierta. La sesión
diaria no espera a esos 10 minutos: una fallada vuelve al final de la misma sesión (store.py) y cada intento se
apunta aquí como un repaso más del mismo día, que es justo para lo que están estos pasos cortos.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

# DEFAULT_PARAMETERS de py-fsrs (FSRS-6). Comprobados uno a uno contra la referencia el 2026-10-17: coinciden.
W = (0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614,
     0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542)

DECAY = -W[20]
# Elegido para que, cuando han pasado tantos días como la estabilidad, la probabilidad de acordarse sea 0,9
FACTOR = 0.9 ** (1 / DECAY) - 1
STABILITY_MIN = 0.001
MAX_INTERVAL = 36500  # días (100 años): el mismo tope que la referencia

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
# Estados de la referencia (Learning / Review / Relearning), con nombre en español para la base de datos
APRENDIENDO, REPASO, REAPRENDIENDO = "aprendiendo", "repaso", "reaprendiendo"
STATES = (APRENDIENDO, REPASO, REAPRENDIENDO)

# Un solo paso de 10 minutos: «Bien» en una tarjeta nueva ya la manda a días (como pide el Repaso) y
# «Otra vez» la trae de vuelta en la misma sesión. La referencia usa por defecto 1 min y 10 min.
LEARNING_STEPS = (timedelta(minutes=10),)
RELEARNING_STEPS = (timedelta(minutes=10),)


@dataclass
class Card:
    stability: float
    difficulty: float
    due: datetime
    reps: int
    lapses: int
    last_review: datetime | None
    # Lo que la referencia guarda además: en qué fase está la tarjeta y por qué paso corto va
    state: str = APRENDIENDO
    step: int | None = 0


def new_card(now: datetime) -> Card:
    """Tarjeta sin contestar nunca: vence ya. Estabilidad y dificultad se fijan con la primera nota."""
    _aware(now)
    return Card(stability=0.0, difficulty=0.0, due=now, reps=0, lapses=0, last_review=None)


def _aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("La fecha del repaso necesita zona horaria")


def _days_between(earlier: datetime, later: datetime) -> int:
    """Días enteros transcurridos, contados en UTC. Restar dos fechas con la MISMA zona en Python ignora el
    cambio de hora (sale la diferencia de reloj, no el tiempo real), así que se pasa todo a UTC antes."""
    return (later.astimezone(timezone.utc) - earlier.astimezone(timezone.utc)).days


def _after(now: datetime, interval: timedelta) -> datetime:
    """`now + interval` en tiempo real (en UTC) y devuelto en la zona de `now`: 2 días son 48 h aunque en
    medio se cambie la hora."""
    return (now.astimezone(timezone.utc) + interval).astimezone(now.tzinfo)


def retrievability(card: Card, now: datetime) -> float:
    """Probabilidad (0-1) de acordarse ahora. Una tarjeta nunca contestada da 0, como en la referencia."""
    if card.last_review is None or card.stability <= 0:
        return 0.0
    elapsed = max(0, _days_between(card.last_review, now))
    return (1 + FACTOR * elapsed / card.stability) ** DECAY


def _initial_stability(rating: int) -> float:
    return max(W[rating - 1], STABILITY_MIN)


def _initial_difficulty(rating: int, *, clamp: bool) -> float:
    d = W[4] - math.exp(W[5] * (rating - 1)) + 1
    return _clamp_difficulty(d) if clamp else d


def _clamp_difficulty(d: float) -> float:
    return min(max(d, 1.0), 10.0)


def _next_interval(stability: float, retention: float) -> int:
    days = (stability / FACTOR) * (retention ** (1 / DECAY) - 1)
    # round() de Python (al par en los empates), igual que la referencia
    return min(max(round(days), 1), MAX_INTERVAL)


def _short_term_stability(stability: float, rating: int) -> float:
    # Repasos del mismo día: la estabilidad apenas cambia, y acertar nunca la baja
    increase = math.exp(W[17] * (rating - 3 + W[18])) * stability ** -W[19]
    if rating in (HARD, GOOD, EASY):
        increase = max(increase, 1.0)
    return max(stability * increase, STABILITY_MIN)


def _next_difficulty(difficulty: float, rating: int) -> float:
    delta = -(W[6] * (rating - 3))
    # Cuanto más difícil ya es, menos sube (amortiguación lineal) …
    damped = difficulty + (10.0 - difficulty) * delta / 9.0
    # … y poco a poco vuelve hacia la dificultad de una tarjeta «Fácil» (sin tope, como la referencia)
    reverted = W[7] * _initial_difficulty(EASY, clamp=False) + (1 - W[7]) * damped
    return _clamp_difficulty(reverted)


def _next_recall_stability(difficulty: float, stability: float, r: float, rating: int) -> float:
    hard_penalty = W[15] if rating == HARD else 1
    easy_bonus = W[16] if rating == EASY else 1
    return stability * (1 + math.exp(W[8]) * (11 - difficulty) * stability ** -W[9]
                        * (math.exp((1 - r) * W[10]) - 1) * hard_penalty * easy_bonus)


def _next_forget_stability(difficulty: float, stability: float, r: float) -> float:
    long_term = (W[11] * difficulty ** -W[12] * ((stability + 1) ** W[13] - 1) * math.exp((1 - r) * W[14]))
    short_term = stability / math.exp(W[17] * W[18])
    return min(long_term, short_term)


def _next_stability(difficulty: float, stability: float, r: float, rating: int) -> float:
    if rating == AGAIN:
        s = _next_forget_stability(difficulty, stability, r)
    else:
        s = _next_recall_stability(difficulty, stability, r, rating)
    return max(s, STABILITY_MIN)


def _steps_interval(card: Card, rating: int, steps: tuple[timedelta, ...], retention: float) -> timedelta:
    """Fases con pasos cortos (aprendiendo / reaprendiendo): igual que la referencia. Cambia `card.state` y
    `card.step` y devuelve cuánto falta para que vuelva."""
    step = card.step or 0
    graduate = timedelta(days=_next_interval(card.stability, retention))
    if not steps or (step >= len(steps) and rating in (HARD, GOOD, EASY)):
        card.state, card.step = REPASO, None
        return graduate
    if rating == AGAIN:
        card.step = 0
        return steps[0]
    if rating == HARD:
        card.step = step
        if step == 0 and len(steps) == 1:
            return steps[0] * 1.5
        if step == 0:
            return (steps[0] + steps[1]) / 2
        return steps[step]
    if rating == GOOD and step + 1 < len(steps):
        card.step = step + 1
        return steps[step + 1]
    # «Bien» en el último paso o «Fácil»: pasa a repasarse por días
    card.state, card.step = REPASO, None
    return graduate


def review(card: Card, rating: int, now: datetime, retention: float = 0.9, *,
           learning_steps: tuple[timedelta, ...] = LEARNING_STEPS,
           relearning_steps: tuple[timedelta, ...] = RELEARNING_STEPS) -> Card:
    """Devuelve la tarjeta tras contestar con `rating` en `now` (no cambia la que se pasa)."""
    _aware(now)
    if rating not in (AGAIN, HARD, GOOD, EASY):
        raise ValueError(f"Nota desconocida: {rating}")
    if not 0 < retention < 1:
        raise ValueError("La retención tiene que estar entre 0 y 1")
    card = replace(card)
    days = _days_between(card.last_review, now) if card.last_review is not None else None

    if card.last_review is None:
        card.stability = _initial_stability(rating)
        card.difficulty = _initial_difficulty(rating, clamp=True)
    elif days < 1:
        card.stability = _short_term_stability(card.stability, rating)
        card.difficulty = _next_difficulty(card.difficulty, rating)
    else:
        r = retrievability(card, now)
        card.stability = _next_stability(card.difficulty, card.stability, r, rating)
        card.difficulty = _next_difficulty(card.difficulty, rating)

    if card.state == REPASO:
        if rating == AGAIN:
            # Un fallo de algo que ya se sabía es un «olvido»: lo que mide el agente de revisión (pieza 4)
            card.lapses += 1
            if relearning_steps:
                card.state, card.step = REAPRENDIENDO, 0
                interval = relearning_steps[0]
            else:
                interval = timedelta(days=_next_interval(card.stability, retention))
        else:
            interval = timedelta(days=_next_interval(card.stability, retention))
    elif card.state == REAPRENDIENDO:
        interval = _steps_interval(card, rating, relearning_steps, retention)
    else:
        interval = _steps_interval(card, rating, learning_steps, retention)

    card.reps += 1
    card.due = _after(now, interval)
    card.last_review = now
    return card
