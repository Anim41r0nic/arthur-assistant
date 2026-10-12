"""Ventanas y escritorios de Hyprland: consultar, cambiar, enfocar, mover y cerrar.

El modelo solo elige QUÉ app y QUÉ escritorio. ARTHUR busca la ventana en la lista
real de Hyprland, toma su dirección exacta y usa una plantilla fija (ver
integrations/hyprland.py). El texto del modelo nunca llega a Hyprland.
"""

from collections.abc import Callable

from ..integrations.desktop_entries import normalize
from ..integrations.hyprland import (
    HyprlandActionError,
    HyprlandController,
    HyprlandReader,
    HyprlandUnavailable,
    WindowGone,
    own_process_tree,
)
from .base import Param, Risk, Tool, ToolDenied, ToolError


def describe(w: dict) -> str:
    return f"{w.get('class') or '?'} «{(w.get('title') or '')[:60]}» (escritorio {w.get('workspace', {}).get('name', '?')})"


def build_window_tools(
    reader: HyprlandReader,
    controller: HyprlandController,
    protected_pids: Callable[[], set[int]] = own_process_tree,
) -> list[Tool]:
    def windows() -> list[dict]:
        try:
            return reader.windows()
        except HyprlandUnavailable as e:
            raise ToolError(str(e)) from e

    def find(app: str, workspace: int | None = None) -> dict:
        """Busca UNA ventana: primero por clase exacta, luego parcial, luego por título."""
        q = normalize(app)
        candidates = [w for w in windows() if workspace is None or w.get("workspace", {}).get("id") == workspace]
        for match in (
            lambda w: normalize(w.get("class", "")) == q,
            lambda w: q in normalize(w.get("class", "")),
            lambda w: q in normalize(w.get("title", "")),
        ):
            found = [w for w in candidates if q and match(w)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                options = "; ".join(describe(w) for w in found[:8])
                raise ToolError(f"Hay varias ventanas que encajan con «{app}»: {options}. Indica el escritorio.")
        where = f" en el escritorio {workspace}" if workspace else ""
        raise ToolError(f"No hay ninguna ventana abierta de «{app}»{where}")

    def act(action: Callable[[], None], done: str) -> str:
        try:
            action()
        except WindowGone as e:
            raise ToolError(f"{e}: no he hecho nada") from e
        except (HyprlandActionError, HyprlandUnavailable, OSError) as e:
            raise ToolError(str(e)) from e
        return done

    # --- consultas ----------------------------------------------------------
    def list_workspaces(_args: dict) -> str:
        try:
            workspaces = reader.query("j/workspaces") or []
            active = (reader.query("j/activeworkspace") or {}).get("id")
        except HyprlandUnavailable as e:
            raise ToolError(str(e)) from e
        per_ws: dict[int, list[str]] = {}
        for w in windows():
            title = (w.get("title") or "")[:40]
            per_ws.setdefault(w.get("workspace", {}).get("id"), []).append(
                f"{w.get('class') or '?'}" + (f" «{title}»" if title else ""))
        lines = []
        for ws in sorted(workspaces, key=lambda x: x.get("id", 0)):
            mark = " ← activo" if ws.get("id") == active else ""
            apps = ", ".join(per_ws.get(ws.get("id"), [])) or "vacío"
            lines.append(f"Escritorio {ws.get('name')}: {apps}{mark}")
        return "\n".join(lines) or "No hay escritorios"

    # --- acciones -----------------------------------------------------------
    def switch_workspace(args: dict) -> str:
        n = args["number"]
        return act(lambda: controller.switch_workspace(n), f"Cambiado al escritorio {n}")

    def focus_window(args: dict) -> str:
        w = find(args["app"], args["workspace"])
        return act(lambda: controller.focus_window(w["address"]), f"He puesto delante {describe(w)}")

    def move_window(args: dict) -> str:
        w = find(args["app"], args["from_workspace"])
        to = args["to_workspace"]
        return act(lambda: controller.move_window(w["address"], to, follow=args["follow"]),
                   f"He movido {describe(w)} al escritorio {to}")

    def check_close(args: dict) -> None:
        w = find(args["app"], args["workspace"])
        if w.get("pid") in protected_pids():
            raise ToolDenied("No puedo cerrar la ventana en la que me estoy ejecutando yo")

    def close_window(args: dict) -> str:
        check_close(args)  # se repite justo antes de actuar
        w = find(args["app"], args["workspace"])
        return act(lambda: controller.close_window(w["address"]),
                   f"Le he pedido a {describe(w)} que se cierre (si tiene cambios sin guardar, te preguntará)")

    app_param = Param("app", str, "p. ej. firefox", max_length=100)
    ws_filter = Param("workspace", int, "si hay varias, el escritorio donde está", required=False,
                      minimum=1, maximum=10)
    return [
        Tool(
            name="list_workspaces",
            description="Escritorios y ventanas abiertas (app y título) y cuál está activo.",
            risk=Risk.READ,
            run=list_workspaces,
        ),
        Tool(
            name="switch_workspace",
            description="Cambia al escritorio 1-10.",
            risk=Risk.LOW,
            params=(Param("number", int, "", minimum=1, maximum=10),),
            run=switch_workspace,
        ),
        Tool(
            name="focus_window",
            description="Pone delante la ventana de una app.",
            risk=Risk.LOW,
            params=(app_param, ws_filter),
            check=lambda a: find(a["app"], a["workspace"]) and None,
            run=focus_window,
        ),
        Tool(
            name="move_window",
            description="Mueve la ventana de una app a otro escritorio.",
            risk=Risk.LOW,
            params=(
                app_param,
                Param("to_workspace", int, "", minimum=1, maximum=10),
                Param("from_workspace", int, "si hay varias, dónde está ahora", required=False,
                      minimum=1, maximum=10),
                Param("follow", bool, "ir también a ese escritorio", required=False, default=False),
            ),
            check=lambda a: find(a["app"], a["from_workspace"]) and None,
            run=move_window,
        ),
        Tool(
            name="close_window",
            description="Cierra la ventana de una app (como pulsar la X).",
            risk=Risk.WRITE,
            params=(app_param, ws_filter),
            check=check_close,
            summarize=lambda a: f"Cerrar la ventana: {describe(find(a['app'], a['workspace']))}",
            run=close_window,
        ),
    ]
