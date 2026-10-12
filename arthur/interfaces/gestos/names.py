"""La LISTA BLANCA de gestos: el único sitio donde existen sus nombres.

Estos nombres son los que viajarán por la tubería del proceso auxiliar (docs/gestos.md). Quien lea esa tubería
solo acepta lo que está aquí (`from_wire`); cualquier otra cosa se ignora. Por eso no se renombran a la ligera, y
un gesto nuevo se añade AQUÍ y en ningún otro sitio (el resto del paquete importa `Gesture` de este módulo).
"""

from __future__ import annotations

from enum import Enum


class Gesture(Enum):
    # --- nivel 1: posturas (una sola foto de la mano basta) ---
    PALM = "palma"
    THUMB_UP = "pulgar_arriba"
    THUMB_DOWN = "pulgar_abajo"
    FIST = "puno"                      # sin «ñ» a propósito: el nombre viaja como texto ASCII
    INDEX_UP = "indice_arriba"
    OK = "circulo"                     # 👌; no "ok": chocaría con el estado "ok" de audit.jsonl y los logs
    PINCH = "pinza"
    TWO_FINGERS = "dos_dedos"
    THUMB_PINKY = "pulgar_menique"
    # --- nivel 2: movimiento (hace falta la trayectoria de la muñeca en el tiempo) ---
    WAVE = "saludo"
    SWIPE = "mover_lateral"


MOTIONS = frozenset({Gesture.WAVE, Gesture.SWIPE})
POSTURES = frozenset(set(Gesture) - MOTIONS)

_BY_NAME = {gesture.value: gesture for gesture in Gesture}


def from_wire(name: object) -> Gesture | None:
    """El gesto de ese nombre, o None si no está en la lista blanca (o no es un texto). Nunca lanza excepciones."""
    return _BY_NAME.get(name) if isinstance(name, str) else None
