"""Tests de calidad del código: errores que un test normal no ve porque dependen de la versión de Python
o de cómo está escrito el código, no de lo que hace."""

import ast
import unittest
from pathlib import Path

ARTHUR = Path(__file__).resolve().parents[1] / "arthur"


def uses_lazy_annotations(tree: ast.Module) -> bool:
    return any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
               and any(alias.name == "annotations" for alias in node.names) for node in tree.body)


def annotations_of(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    args = function.args
    every_arg = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [a.annotation for a in every_arg if a is not None and a.annotation is not None] + (
        [function.returns] if function.returns is not None else [])


class AnnotationShadowingTest(unittest.TestCase):
    def test_a_method_never_hides_a_name_used_later_in_an_annotation(self):
        """En Python 3.14 las anotaciones se evalúan tarde y esto no se nota; en 3.13 o antes, sí.

        Si una clase define un método `list` y más abajo escribe `-> list[Contact]`, hasta 3.13 Python
        evalúa `list` dentro de la clase, encuentra el MÉTODO y el módulo ni se importa (pyproject.toml
        dice que se admite 3.11 o superior). La solución es `from __future__ import annotations`.
        """
        problems = []
        for path in sorted(ARTHUR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if uses_lazy_annotations(tree):
                continue
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                defined: set[str] = set()  # métodos ya definidos en la clase, en orden
                for item in cls.body:
                    if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for note in annotations_of(item):
                        for node in ast.walk(note):
                            if isinstance(node, ast.Name) and node.id in defined:
                                problems.append(f"{path.relative_to(ARTHUR.parent)}:{node.lineno} "
                                                f"({cls.name}.{item.name}): «{node.id}» es un método de la clase")
                    defined.add(item.name)
        self.assertEqual(problems, [], "Anotaciones que fallan en Python < 3.14: añade "
                                       "`from __future__ import annotations` al módulo")


if __name__ == "__main__":
    unittest.main()
