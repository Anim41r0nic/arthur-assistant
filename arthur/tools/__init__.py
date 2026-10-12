from ..core.config import SecurityConfig
from ..integrations.desktop_entries import AppCatalog
from ..integrations.hyprland import HyprlandController, HyprlandReader
from ..integrations.media import MediaPlayer
from ..security.commands import CommandPolicy
from ..security.paths import PathPolicy
from ..security.sandbox import Sandbox
from .applications import build_application_tools
from .calendar import build_calendar_tools
from .desktop import build_desktop_tools
from .files import build_file_tools
from .gestures import build_gesture_tools
from .gmail import build_gmail_tools
from .media import build_media_tools
from .memory import build_memory_tools
from .messaging import build_messaging_tools
from .notifications import build_notification_tools
from .packages import build_package_tools
from .programs import Programs
from .registry import ToolRegistry
from .reminders import build_reminder_tools
from .system import build_system_tools
from .terminal import build_terminal_tools
from .wake import build_wake_tools
from .windows import build_window_tools


def build_registry(
    cfg: SecurityConfig,
    *,
    programs: Programs | None = None,
    catalog: AppCatalog | None = None,
    hyprland: HyprlandReader | None = None,
    hyprland_actions: HyprlandController | None = None,
    memory=None,
    notifications=None,
    calendar=None,
    gmail=None,
    wake=None,
    media=None,
    gestures=None,
    profile: str = "full",
) -> ToolRegistry:
    """Crea el catálogo de herramientas con sus políticas de seguridad.

    `programs`, `catalog` y `hyprland` se pueden sustituir en los tests por versiones
    falsas, para que nunca toquen el escritorio real.

    `profile="core"`: el ARTHUR del servidor (docs/servidor.md). Solo lo que tiene sentido allí: memoria,
    recordatorios, calendario, correo y el estado del servidor. Nada de ficheros, órdenes, apps, ventanas,
    volumen ni música: eso es de cada aparato y llega como herramientas remotas (fase S3).

    `profile="desktop"`: las «manos» de este aparato para el ARTHUR del servidor (fase S3): apps, ventanas,
    volumen, notificaciones, portapapeles, música y ficheros. Sin órdenes ni instalaciones (son PELIGROSAS: nunca
    se usan a distancia) y sin lo que vive en el servidor (memoria, calendario, correo).
    """
    if profile not in ("full", "core", "desktop"):
        raise ValueError(f"Perfil de herramientas desconocido: {profile!r}")
    if profile == "desktop":
        paths = PathPolicy.from_config(cfg)
        programs = programs or Programs()
        hyprland = hyprland or HyprlandReader()
        registry = ToolRegistry()
        for tool in (
            *build_file_tools(paths),
            *build_application_tools(catalog or AppCatalog(), cfg.trusted_apps, programs, hyprland),
            *build_window_tools(hyprland, hyprland_actions or HyprlandController()),
            *build_desktop_tools(programs),
            *build_media_tools(media or MediaPlayer(programs)),
            *(build_gesture_tools(gestures) if gestures else ()),  # la cámara es de este aparato (fase S7)
            *(build_notification_tools(notifications) if notifications else ()),  # sus avisos, también
        ):
            registry.register(tool)
        return registry
    if profile == "core":
        registry = ToolRegistry()
        for tool in (
            *build_system_tools(),
            *(build_memory_tools(memory) if memory else ()),
            *(build_reminder_tools(memory) if memory else ()),
            *build_calendar_tools(calendar or _calendar_client()),
            *build_gmail_tools(gmail or _gmail_client()),
        ):
            registry.register(tool)
        return registry
    paths = PathPolicy.from_config(cfg)
    sandbox = Sandbox(read_roots=paths.read_roots, hidden=paths.sensitive_dirs)
    programs = programs or Programs()
    gmail = gmail or _gmail_client()
    hyprland = hyprland or HyprlandReader()
    registry = ToolRegistry()
    for tool in (
        *build_system_tools(),
        *build_file_tools(paths),
        *build_terminal_tools(CommandPolicy(cfg.command_allowlist, paths), sandbox, cfg.command_timeout),
        *build_application_tools(catalog or AppCatalog(), cfg.trusted_apps, programs, hyprland),
        *build_window_tools(hyprland, hyprland_actions or HyprlandController()),
        *build_desktop_tools(programs),
        *build_package_tools(programs),
        *build_media_tools(media or MediaPlayer(programs)),
        *(build_memory_tools(memory) if memory else ()),
        *(build_reminder_tools(memory) if memory else ()),
        # Sin memoria persistente, los contactos viven solo mientras ARTHUR esté abierto.
        *build_messaging_tools(memory or _ephemeral_store(), programs, gmail),
        *(build_notification_tools(notifications) if notifications else ()),
        *build_calendar_tools(calendar or _calendar_client()),
        *build_gmail_tools(gmail),
        *(build_wake_tools(wake) if wake else ()),
        *(build_gesture_tools(gestures) if gestures else ()),
    ):
        registry.register(tool)
    return registry


def _ephemeral_store():
    from ..memory.store import MemoryStore

    return MemoryStore.in_memory()


def _calendar_client():
    from ..integrations.google import CalendarClient, GoogleAuth

    return CalendarClient(GoogleAuth())


def _gmail_client():
    from ..integrations.google import GmailClient, GoogleAuth

    return GmailClient(GoogleAuth())
