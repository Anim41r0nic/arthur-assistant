from ..core.config import Config
from ..tools.registry import ToolRegistry
from .base import Brain
from .ollama import OllamaBrain
from .rules import RuleBrain


def create_brain(config: Config, registry: ToolRegistry, memory=None) -> Brain:
    """Elige el cerebro según `brain.provider` en la configuración."""
    match config.brain.provider:
        case "rules":
            return RuleBrain(assistant_name=config.assistant.name)
        case "ollama":
            from ..memory.store import memory_context

            context = (lambda: memory_context(memory)) if memory else None
            return OllamaBrain(config.brain, config.security, config.assistant.name, registry.schemas, context)
    raise ValueError(f"Cerebro desconocido: {config.brain.provider}")
