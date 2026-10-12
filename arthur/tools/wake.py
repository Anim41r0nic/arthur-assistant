"""Pausar o reanudar la palabra de activación («hey arthur»)."""

from ..voice.wakeword import WakeSwitch
from .base import Param, Risk, Tool


def build_wake_tools(switch: WakeSwitch) -> list[Tool]:
    return [
        Tool(
            name="set_wake_word",
            description="«Deja de escuchar» (false) o «vuelve a escuchar» (true) la palabra de activación.",
            risk=Risk.LOW,
            params=(Param("enabled", bool, ""),),
            run=lambda a: switch.set(a["enabled"]),
        )
    ]
