"""Utilidades compartidas por los tests."""

from arthur.core.config import SecurityConfig
from arthur.core.events import EventBus
from arthur.core.router import ToolRouter
from arthur.security.audit import AuditLog
from arthur.security.policy import Policy
from arthur.tools.registry import ToolRegistry


class ScriptedConfirmer:
    """Responde a las confirmaciones con una lista fija y recuerda qué se preguntó."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.requests = []

    def confirm(self, request) -> bool:
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else False


def make_router(tools, *, confirmer=None, audit=None, bus=None, **security):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolRouter(
        registry,
        Policy(SecurityConfig(**security)),
        confirmer or ScriptedConfirmer(),
        audit or AuditLog(None),
        bus or EventBus(),
    )


def fake_input(lines):
    """Simula lo que el usuario teclea; al acabarse, es como pulsar Ctrl+D."""
    it = iter(lines)

    def _input(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return _input
