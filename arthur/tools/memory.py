"""Herramientas de memoria: recordar, consultar y olvidar.

Guardar en la memoria es una escritura: pide confirmación (como cualquier
escritura) para que nada se recuerde sin que tú lo veas.
"""

from ..memory.store import KINDS, MemoryStore, MemoryStoreError
from .base import Param, Risk, Tool, ToolDenied, ToolError

KIND_LABELS = {"preference": "preferencia", "fact": "dato"}


def build_memory_tools(store: MemoryStore) -> list[Tool]:
    def check_remember(args: dict) -> None:
        try:
            store.validate(args["kind"], args["content"])
        except MemoryStoreError as e:
            raise ToolDenied(str(e)) from e

    def remember(args: dict) -> str:
        try:
            memory = store.add(args["kind"], args["content"])
        except MemoryStoreError as e:
            raise ToolError(str(e)) from e
        return f"Guardado en la memoria como {KIND_LABELS[memory.kind]} #{memory.id}: {memory.content}"

    def list_memories(args: dict) -> str:
        memories = store.list(args["query"])
        if not memories:
            return "No tengo nada guardado" + (f" sobre «{args['query']}»" if args["query"] else "")
        return "\n".join(f"#{m.id} [{KIND_LABELS[m.kind]}] {m.content}" for m in memories)

    def check_forget(args: dict) -> None:
        if store.get(args["id"]) is None:
            raise ToolError(f"No hay ningún recuerdo con el número #{args['id']}")

    def summarize_forget(args: dict) -> str:
        memory = store.get(args["id"])
        return f"Olvidar el recuerdo #{args['id']}: «{memory.content if memory else '?'}»"

    def forget(args: dict) -> str:
        store.delete(args["id"])
        return f"He olvidado el recuerdo #{args['id']}"

    return [
        Tool(
            name="remember",
            description="Guarda algo en la memoria SOLO si el usuario pide que lo recuerdes.",
            risk=Risk.WRITE,
            params=(
                Param("content", str, "frase breve en tercera persona", max_length=300),
                Param("kind", str, " o ".join(KINDS), required=False, default="fact", max_length=20),
            ),
            check=check_remember,
            summarize=lambda a: f"Recordar como {KIND_LABELS.get(a['kind'], a['kind'])}: «{a['content']}»",
            run=remember,
        ),
        Tool(
            name="list_memories",
            description="Lo que recuerdas del usuario.",
            risk=Risk.READ,
            params=(Param("query", str, "", required=False, default="", max_length=100),),
            run=list_memories,
        ),
        Tool(
            name="forget",
            description="Olvida un recuerdo por su #id.",
            risk=Risk.WRITE,
            params=(Param("id", int, "", minimum=1),),
            check=check_forget,
            summarize=summarize_forget,
            run=forget,
        ),
    ]
