"""Decide si una herramienta se ejecuta directamente, pide confirmación o se deniega.

Es independiente de cada herramienta: solo mira su nivel de riesgo y la
configuración. Las comprobaciones concretas (rutas, comandos) las hace cada
herramienta antes de llegar aquí.
"""

import enum
from dataclasses import dataclass

from ..core.config import SecurityConfig
from ..tools.base import Risk


class Decision(enum.StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str


class Policy:
    def __init__(self, cfg: SecurityConfig) -> None:
        self.cfg = cfg

    def decide(self, risk: Risk) -> Verdict:
        if self.cfg.safe_mode and risk is not Risk.READ:
            return Verdict(Decision.DENY, "Modo seguro activo: solo se permiten consultas")
        match risk:
            case Risk.READ:
                return Verdict(Decision.ALLOW, "Solo lectura")
            case Risk.LOW:
                return Verdict(Decision.ALLOW, "Acción trivial y reversible")
            case Risk.WRITE if not self.cfg.confirm_writes:
                return Verdict(Decision.ALLOW, "Escritura sin confirmación (confirm_writes = false)")
            case Risk.WRITE:
                return Verdict(Decision.CONFIRM, "Las escrituras requieren confirmación")
            case Risk.PRIVATE:
                return Verdict(Decision.CONFIRM, "Accede a datos que pueden ser privados: requiere confirmación siempre")
            case Risk.DANGEROUS:
                return Verdict(Decision.CONFIRM, "Acción peligrosa: requiere confirmación siempre")
