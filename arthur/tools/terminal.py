"""run_command: ejecutar un programa permitido dentro del sandbox."""

import shlex

from ..security.commands import CommandDenied, CommandPolicy
from ..security.sandbox import Sandbox
from .base import Param, Risk, Tool, ToolDenied


def build_terminal_tools(commands: CommandPolicy, sandbox: Sandbox, timeout: int) -> list[Tool]:
    def validated(args: dict) -> list[str]:
        try:
            return commands.check(args["command"])
        except CommandDenied as e:
            raise ToolDenied(str(e)) from e

    def run_command(args: dict) -> str:
        out = sandbox.run(validated(args), timeout=timeout)
        if out.timed_out:
            status = f"cortado tras {timeout} s"
        else:
            status = f"código de salida {out.returncode}"
        parts = [f"[{status}]"]
        if out.stdout.strip():
            parts.append(out.stdout.rstrip())
        if out.stderr.strip():
            parts.append(f"[errores]\n{out.stderr.rstrip()}")
        return "\n".join(parts)

    return [
        Tool(
            name="run_command",
            description="Ejecuta un programa permitido (sin shell, en sandbox).",
            risk=Risk.DANGEROUS,
            params=(Param("command", list, "p. ej. [\"df\", \"-h\"]", max_length=32),),
            check=lambda a: validated(a) and None,
            summarize=lambda a: f"Ejecutar en sandbox: {shlex.join(a['command'])}",
            run=run_command,
        )
    ]
