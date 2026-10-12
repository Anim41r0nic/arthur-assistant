"""Aplicaciones y ventanas: buscar apps instaladas, abrirlas y ver qué ventanas hay."""

import shutil

from ..integrations.desktop_entries import AppCatalog, DesktopApp
from ..integrations.hyprland import HyprlandReader, HyprlandUnavailable
from .base import Param, Risk, Tool, ToolError
from .programs import Programs

MAX_LISTED = 40


def build_application_tools(
    catalog: AppCatalog,
    trusted_apps: tuple[str, ...],
    programs: Programs,
    hyprland: HyprlandReader,
) -> list[Tool]:
    trusted = frozenset(trusted_apps)

    def resolve(name: str) -> DesktopApp:
        matches = catalog.search(name)
        if not matches:
            raise ToolError(f"No encuentro ninguna aplicación instalada que se llame «{name}»")
        if len(matches) > 1:
            options = ", ".join(f"{a.name} ({a.id})" for a in matches[:10])
            raise ToolError(f"Hay varias aplicaciones que encajan con «{name}»: {options}. ¿Cuál quieres?")
        return matches[0]

    # --- list_applications ------------------------------------------------
    def list_applications(args: dict) -> str:
        apps = catalog.search(args["query"])
        if not apps:
            return f"No hay aplicaciones que encajen con «{args['query']}»"
        lines = [f"{a.name} (id: {a.id}){' · de confianza' if a.id in trusted else ''}" for a in apps[:MAX_LISTED]]
        if len(apps) > MAX_LISTED:
            lines.append(f"… y {len(apps) - MAX_LISTED} más; afina la búsqueda")
        return "\n".join(lines)

    # --- open_application -------------------------------------------------
    def open_risk(args: dict) -> Risk:
        app = resolve(args["name"])
        if app.privileged:
            return Risk.DANGEROUS  # pide permisos de administrador
        return Risk.LOW if app.id in trusted else Risk.WRITE

    def summarize_open(args: dict) -> str:
        app = resolve(args["name"])
        note = " (pide permisos de administrador)" if app.privileged else ""
        return f"Abrir la aplicación {app.name} [{app.id}]{note}"

    def open_application(args: dict) -> str:
        app = resolve(args["name"])
        desktop_id = f"{app.id}.desktop"
        if shutil.which("uwsm"):
            # Igual que tus atajos de Hyprland: cada app en su propio scope de systemd.
            programs.launch(["uwsm", "app", "--", desktop_id])
        else:
            programs.launch(["gtk-launch", app.id])
        return f"He abierto {app.name}"

    # --- list_windows -----------------------------------------------------
    def list_windows(_args: dict) -> str:
        try:
            windows = hyprland.windows()
        except HyprlandUnavailable as e:
            raise ToolError(str(e)) from e
        if not windows:
            return "No hay ventanas abiertas"
        lines = [
            f"{w.get('class') or '?'} · «{(w.get('title') or '')[:80]}» · escritorio {w.get('workspace', {}).get('name', '?')}"
            for w in windows
        ]
        return f"{len(windows)} ventanas abiertas:\n" + "\n".join(lines)

    name_param = Param("name", str, "p. ej. firefox", max_length=100)
    return [
        Tool(
            name="list_applications",
            description="Busca apps instaladas por nombre o tipo (p. ej. navegador).",
            risk=Risk.READ,
            params=(Param("query", str, "", required=False, default="", max_length=100),),
            run=list_applications,
        ),
        Tool(
            name="open_application",
            description="Abre una app instalada.",
            risk=Risk.WRITE,
            params=(name_param,),
            check=lambda a: resolve(a["name"]) and None,
            risk_for=open_risk,
            summarize=summarize_open,
            run=open_application,
        ),
    ]
