"""Encender o apagar la cámara de los gestos («activa los gestos» / «apaga los gestos»)."""

from ..interfaces.gestos.watcher import GESTURE_HELP, GestureSwitch
from .base import Param, Risk, Tool


def build_gesture_tools(switch: GestureSwitch) -> list[Tool]:
    def summary(args: dict) -> str:
        if not args["enabled"]:
            return "Apagar la cámara de los gestos"
        return (f"ENCENDER LA CÁMARA para los gestos ({GESTURE_HELP}). "
                "Verás el punto rojo en el Orb y se apaga sola si no la usas.")

    return [
        Tool(
            name="set_gestures",
            description="Enciende (true) o apaga (false) la cámara de los gestos.",
            # Encender la cámara es PRIVADO: pide confirmación SIEMPRE, así el modelo no puede encenderla por su
            # cuenta ni porque se lo pida un correo. Apagarla es de bajo riesgo: siempre se puede.
            risk=Risk.PRIVATE,
            risk_for=lambda a: Risk.PRIVATE if a["enabled"] else Risk.LOW,
            params=(Param("enabled", bool, ""),),
            summarize=summary,
            run=lambda a: switch.set(a["enabled"], by="voz"),
        )
    ]
