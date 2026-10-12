"""La documentación no puede quedarse atrás del código: la lista de herramientas del README es la que ve el usuario
para saber qué puede hacer ARTHUR y con qué riesgo, y ya se había desfasado (citaba herramientas que no existen)."""

import re
import unittest
from pathlib import Path

from arthur.core.config import SecurityConfig
from arthur.integrations.notifications import NotificationWatcher
from arthur.interfaces.gestos.watcher import GestureSwitch
from arthur.memory.store import MemoryStore
from arthur.tools import build_registry
from arthur.voice.wakeword import WakeSwitch

README = Path(__file__).resolve().parents[1] / "README.md"
TOOL_NAME = re.compile(r"`([a-z][a-z0-9_]*)`")


def registered_tools() -> dict[str, str]:
    """Todas las herramientas (con memoria, avisos, Google, palabra de activación y gestos activados) → su riesgo."""
    registry = build_registry(SecurityConfig(), memory=MemoryStore.in_memory(),
                              notifications=NotificationWatcher(lambda note: None),
                              calendar=object(), gmail=object(), wake=WakeSwitch(),
                              gestures=GestureSwitch())
    # Las del calendario de Google se documentan en su propia sección (docs/google.md), no en esta tabla.
    return {t.name: t.risk.value for t in registry.all() if t.run.__module__ != "arthur.tools.calendar"}


def documented_tools() -> dict[str, set[str]]:
    """Las herramientas que cita la tabla de riesgos del README (segunda columna) → los riesgos en que aparece."""
    text = README.read_text(encoding="utf-8")
    table = text[text.index("| Riesgo | Herramientas |"):]
    found: dict[str, set[str]] = {}
    for row in table.splitlines():
        if not row.startswith("|"):
            break  # fin de la tabla
        cells = [c.strip() for c in row.strip("|").split("|")]
        if cells[0] in ("Riesgo", "---"):
            continue
        for name in TOOL_NAME.findall(cells[1]):
            found.setdefault(name, set()).add(cells[0])
    return found


class ReadmeToolTableTest(unittest.TestCase):
    def test_every_tool_is_in_the_readme_table(self):
        missing = sorted(set(registered_tools()) - set(documented_tools()))
        self.assertEqual(missing, [], f"Herramientas que faltan en la tabla de «Seguridad» del README: {missing}")

    def test_the_table_does_not_mention_tools_that_do_not_exist(self):
        stale = sorted(set(documented_tools()) - set(registered_tools()))
        self.assertEqual(stale, [], f"El README cita herramientas que ya no existen: {stale}")

    def test_the_documented_risk_matches_the_code(self):
        labels = {"read": "lectura", "low": "bajo riesgo", "write": "escritura", "private": "PRIVADA", "dangerous": "PELIGROSA"}
        wrong = {name: (labels[risk], sorted(documented_tools().get(name, set())))
                 for name, risk in registered_tools().items()
                 if labels[risk] not in documented_tools().get(name, set())}
        # Las que cambian de riesgo según los argumentos (open_application, recent_messages) salen en varias filas:
        # basta con que su riesgo base esté entre ellas.
        self.assertEqual(wrong, {}, "El riesgo del README no coincide con el del código")


if __name__ == "__main__":
    unittest.main()
