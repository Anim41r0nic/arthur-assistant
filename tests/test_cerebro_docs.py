"""El servicio «cerebro» de compose.yaml tiene que ser tan seguro como dice docs/cerebro.md: si alguien le pone un
`ports:` o le quita el solo lectura a las bóvedas, el servidor sería menos seguro de lo que dice la guía. Sin librería
de YAML (núcleo con biblioteca estándar): se lee por sangría."""

import unittest
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"


def example_block() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def service(block: str, name: str) -> str:
    """Las líneas del servicio `name` (sangría 2 bajo `services:`), sin comentarios de línea entera."""
    lines = block.splitlines()
    try:
        i = lines.index(f"  {name}:")
    except ValueError:
        raise AssertionError(f"El ejemplo no tiene el servicio «{name}»") from None
    out = []
    for line in lines[i + 1:]:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if len(line) - len(line.lstrip()) <= 2:
            break  # el siguiente servicio o la siguiente sección (volumes:, secrets:)
        out.append(line)
    return "\n".join(out)


class ComposeDeEjemploTest(unittest.TestCase):
    def test_compose_de_ejemplo(self):
        block = example_block()
        cerebro = service(block, "cerebro")
        self.assertNotIn("ports:", cerebro, "El cerebro solo vive en la red de Docker: nada de «ports:»")
        self.assertRegex(cerebro, r"(?m)^\s+mem_limit: 384m\s*(#.*)?$")
        self.assertRegex(cerebro, r"(?m)^\s+read_only: true\s*(#.*)?$")
        self.assertRegex(cerebro, r"(?m)^\s+cap_drop: \[ALL\]\s*(#.*)?$")
        vaults = [line.strip() for line in cerebro.splitlines() if "/vaults/" in line]
        self.assertGreaterEqual(len(vaults), 2, "Faltan las bóvedas montadas")
        for line in vaults:
            self.assertTrue(line.split("#")[0].strip().endswith(":ro"), f"Bóveda sin solo lectura: {line}")
        self.assertRegex(cerebro, r"(?m)^\s+profiles: \[cerebro\]\s*$")
        apuntes = service(block, "apuntes-web")
        self.assertIn("dockerfile: Dockerfile.apuntes", apuntes)
        for name in ("webchat",):
            self.assertNotIn("ports:", service(block, name), f"{name} solo vive en la red de Docker")


if __name__ == "__main__":
    unittest.main()
