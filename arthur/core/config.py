"""Carga y validación de la configuración (formato TOML).

Orden de búsqueda:
  1. la ruta pasada con --config (debe existir);
  2. ~/.config/arthur/config.toml (si existe);
  3. si no hay fichero, valores por defecto.

La validación es estricta: una sección o clave desconocida, o un tipo
incorrecto, es un error. Así una errata no se ignora en silencio.
"""

import dataclasses
import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
BRAIN_PROVIDERS = ("ollama", "rules")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AssistantConfig:
    name: str = "ARTHUR"
    history_limit: int = 20  # turnos (pregunta + respuesta) que se recuerdan en RAM

    def validate(self) -> None:
        if not self.name.strip():
            raise ConfigError("assistant.name no puede estar vacío")
        if not 1 <= self.history_limit <= 200:
            raise ConfigError("assistant.history_limit debe estar entre 1 y 200")


@dataclass(frozen=True)
class BrainConfig:
    provider: str = "ollama"  # "ollama" (LLM local) o "rules" (reglas, sin IA)
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b-instruct"  # variante sin razonamiento previo: mucho más rápida
    think: bool = False          # razonamiento previo: respuestas mejores pero mucho más lentas
    context_tokens: int = 8192   # memoria de trabajo del modelo (más = más RAM)
    timeout: int = 180           # segundos máximos esperando una respuesta
    keep_alive: str = "3h"       # cuánto tiempo sigue el modelo cargado en RAM tras usarlo (~4 GB)
    vision: bool = False         # el modelo ve fotos (p. ej. qwen3-vl:4b-instruct): la web deja adjuntarlas

    def validate(self) -> None:
        if self.provider not in BRAIN_PROVIDERS:
            raise ConfigError(f"brain.provider debe ser uno de {BRAIN_PROVIDERS}")
        url = urllib.parse.urlsplit(self.ollama_url)
        # Solo se permite este mismo equipo, o un servicio de la red interna de Docker (un nombre SIN puntos,
        # como «ollama», que no puede ser una dirección de internet): la conversación nunca debe salir del
        # servidor por un error de configuración.
        host = url.hostname or ""
        internal = bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", host)) and host != "localhost"
        if url.scheme != "http" or not (host in LOOPBACK_HOSTS or internal) or url.path not in ("", "/"):
            raise ConfigError("brain.ollama_url debe apuntar a este equipo (http://127.0.0.1:11434) "
                              "o a un servicio de Docker por su nombre (http://ollama:11434)")
        if not self.model.strip():
            raise ConfigError("brain.model no puede estar vacío")
        if not 2048 <= self.context_tokens <= 131072:
            raise ConfigError("brain.context_tokens debe estar entre 2048 y 131072")
        if not 5 <= self.timeout <= 900:
            raise ConfigError("brain.timeout debe estar entre 5 y 900")


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    to_file: bool = True

    def validate(self) -> None:
        if self.level not in LOG_LEVELS:
            raise ConfigError(f"logging.level debe ser uno de {LOG_LEVELS}")


@dataclass(frozen=True)
class SecurityConfig:
    # Modo seguro: solo se permiten herramientas de lectura.
    safe_mode: bool = False
    # Pedir confirmación antes de escribir. Las acciones peligrosas la piden siempre.
    confirm_writes: bool = True
    read_roots: tuple[str, ...] = ("~/Proyectos", "~/Documentos")
    write_roots: tuple[str, ...] = ("~/Proyectos",)
    # Programas que run_command puede ejecutar (solo el nombre, sin ruta).
    command_allowlist: tuple[str, ...] = (
        "uname", "uptime", "df", "free", "lsblk", "lscpu", "whoami", "hostname", "date", "ls",
    )
    command_timeout: int = 10  # segundos
    # Apps que se abren sin pedir confirmación (identificador del .desktop, sin la extensión).
    trusted_apps: tuple[str, ...] = ("firefox", "codeblocks")

    def validate(self) -> None:
        if not 1 <= self.command_timeout <= 120:
            raise ConfigError("security.command_timeout debe estar entre 1 y 120")
        for root in (*self.read_roots, *self.write_roots):
            if not root.startswith(("/", "~/")):
                raise ConfigError(f"Ruta no válida en security: {root!r} (usa /… o ~/…)")
            # Dar acceso a todo el disco o a toda tu carpeta personal es demasiado amplio.
            if Path(root).expanduser().resolve() in (Path("/"), Path.home().resolve()):
                raise ConfigError(f"Ruta demasiado amplia en security: {root!r}")
        for app in self.trusted_apps:
            if not app or not all(c.isalnum() or c in "._-" for c in app):
                raise ConfigError(f"Identificador de app no válido en trusted_apps: {app!r}")
        for name in self.command_allowlist:
            if not name or "/" in name or name != name.strip():
                raise ConfigError(f"Nombre de programa no válido en command_allowlist: {name!r}")


@dataclass(frozen=True)
class VoiceConfig:
    tts: bool = False                       # responder hablando (también se activa con --voz)
    voice: str = "es_ES-davefx-medium"      # nombre del modelo de Piper
    voices_dir: str = "~/.local/share/arthur/voices"
    speed: float = 1.0                      # >1 más lento, <1 más rápido
    volume: float = 1.0                     # 0,1 a 1,0 (sobre el volumen del sistema)
    stt: bool = False                       # escucharte al pulsar Enter (también con --voz)
    stt_model: str = "ggml-small-q5_1"      # modelo de whisper.cpp
    stt_models_dir: str = "~/.local/share/arthur/whisper"
    language: str = "es"
    max_record_seconds: int = 60            # límite de seguridad de cada grabación
    stt_beam_size: int = 1                  # 1 = más rápido; hasta 8 = más preciso pero más lento

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.voice):
            raise ConfigError(f"voice.voice no es un nombre de voz válido: {self.voice!r}")
        if not self.voices_dir.startswith(("/", "~/")):
            raise ConfigError("voice.voices_dir debe ser una ruta absoluta o empezar por ~/")
        if not 0.5 <= self.speed <= 2.0:
            raise ConfigError("voice.speed debe estar entre 0.5 y 2.0")
        if not 0.1 <= self.volume <= 1.0:
            raise ConfigError("voice.volume debe estar entre 0.1 y 1.0")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.stt_model):
            raise ConfigError(f"voice.stt_model no es un nombre de modelo válido: {self.stt_model!r}")
        if not self.stt_models_dir.startswith(("/", "~/")):
            raise ConfigError("voice.stt_models_dir debe ser una ruta absoluta o empezar por ~/")
        if not re.fullmatch(r"[a-z]{2}", self.language):
            raise ConfigError("voice.language debe ser un código de idioma de 2 letras, p. ej. \"es\"")
        if not 1 <= self.stt_beam_size <= 8:
            raise ConfigError("voice.stt_beam_size debe estar entre 1 y 8")
        if not 3 <= self.max_record_seconds <= 300:
            raise ConfigError("voice.max_record_seconds debe estar entre 3 y 300")

    @property
    def model_path(self) -> Path:
        return Path(self.voices_dir).expanduser() / f"{self.voice}.onnx"

    @property
    def stt_model_path(self) -> Path:
        return Path(self.stt_models_dir).expanduser() / f"{self.stt_model}.bin"


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True          # recordar preferencias y datos que tú le pidas
    save_history: bool = False    # guardar el historial de conversaciones (desactivado por defecto)
    history_days: int = 30        # si se guarda, se borra solo pasados estos días

    def validate(self) -> None:
        if not 1 <= self.history_days <= 365:
            raise ConfigError("memory.history_days debe estar entre 1 y 365")


@dataclass(frozen=True)
class NotificationsConfig:
    enabled: bool = True                            # escuchar las notificaciones de escritorio
    announce: tuple[str, ...] = ("whatsapp", "web")  # de qué fuentes avisa (whatsapp, gmail, web, otra)
    announce_text: bool = False                     # leer también el texto (si no, solo quién escribe)

    def validate(self) -> None:
        for source in self.announce:
            if source not in ("whatsapp", "gmail", "web", "otra"):
                raise ConfigError(f"notifications.announce: fuente desconocida {source!r}")


@dataclass(frozen=True)
class WakeConfig:
    enabled: bool = False          # escuchar de fondo la palabra de activación (solo en modo servicio)
    phrase: str = "hey arthur"     # tiene que estar en el vocabulario del modelo de Vosk
    variants: tuple[str, ...] = ()  # otras formas en que el modelo "oye" tu manera de decirlo (calibración)
    model: str = "~/.local/share/arthur/vosk/vosk-model-small-en-us-0.15"
    chime: bool = True             # "ding" al oír la palabra
    min_confidence: float = 0.7    # seguridad mínima (0-1): más alto = menos falsas alarmas, pero te oye peor
    double_clap: bool = False      # una doble palmada también despierta a ARTHUR (mismo micrófono)
    clap_peak: float = 0.3         # pico mínimo (0-1 de la escala) de cada palmada: más alto = hay que dar más fuerte

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z']+( [a-z']+){1,3}", self.phrase):
            raise ConfigError(f"wake.phrase: {self.phrase!r} debe tener de 2 a 4 palabras en minúsculas, sin tildes")
        for variant in self.variants:  # las variantes pueden ser una sola palabra (p. ej. "arthur")
            if not re.fullmatch(r"[a-z']+( [a-z']+){0,3}", variant):
                raise ConfigError(f"wake.variants: {variant!r} debe tener de 1 a 4 palabras en minúsculas, sin tildes")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ConfigError("wake.min_confidence debe estar entre 0 y 1")
        if not 0.05 <= self.clap_peak <= 1.0:
            raise ConfigError("wake.clap_peak debe estar entre 0,05 y 1")
        if not self.model.startswith(("/", "~/")):
            raise ConfigError("wake.model debe ser una ruta absoluta o empezar por ~/")

    @property
    def model_path(self) -> Path:
        return Path(self.model).expanduser()


@dataclass(frozen=True)
class OrbConfig:
    port: int = 8765          # solo en 127.0.0.1: no es accesible desde otros equipos
    open_browser: bool = True  # abrir el navegador al arrancar con --orb
    fullscreen_on_wake: bool = True  # «Hey Arthur» trae el Orb a pantalla completa (y se oculta al terminar)

    def validate(self) -> None:
        if not 1024 <= self.port <= 65535:
            raise ConfigError("orb.port debe estar entre 1024 y 65535")


@dataclass(frozen=True)
class PowerConfig:
    auto_performance: bool = True  # enchufado → perfil «performance»; al desenchufar vuelve al de antes

    def validate(self) -> None:
        pass


@dataclass(frozen=True)
class GesturesConfig:
    # Gestos con la cámara (docs/gestos.md). `enabled = true` NO enciende nada: solo permite que TÚ la enciendas
    # (botón del Orb o «activa los gestos», que pide confirmación). Cada sesión empieza con la cámara apagada.
    enabled: bool = False
    idle_minutes: int = 5        # se apaga sola tras estos minutos sin ningún gesto
    hold_seconds: float = 1.0    # cuánto hay que mantener el gesto para que cuente
    fps: int = 8                 # fotogramas por segundo que mira el proceso de la cámara
    min_score: float = 0.8       # seguridad mínima del gesto (0-1): más alto = menos falsas alarmas

    def validate(self) -> None:
        if not 1 <= self.idle_minutes <= 60:
            raise ConfigError("gestures.idle_minutes debe estar entre 1 y 60")
        if not 0.5 <= self.hold_seconds <= 3.0:
            raise ConfigError("gestures.hold_seconds debe estar entre 0.5 y 3.0")
        if not 1 <= self.fps <= 15:
            raise ConfigError("gestures.fps debe estar entre 1 y 15")
        if not 0.5 <= self.min_score <= 1.0:
            raise ConfigError("gestures.min_score debe estar entre 0.5 y 1.0")


@dataclass(frozen=True)
class Config:
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    orb: OrbConfig = field(default_factory=OrbConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    gestures: GesturesConfig = field(default_factory=GesturesConfig)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "arthur" / "config.toml"


def state_dir() -> Path:
    """Carpeta para datos que ARTHUR genera (logs, más adelante memoria)."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "arthur"


def data_dir() -> Path:
    """Carpeta para los datos que ARTHUR guarda a largo plazo (memoria, voces, modelos)."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "arthur"


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = default_config_path()
        if not path.exists():
            return Config()
    elif not path.exists():
        raise ConfigError(f"No existe el fichero de configuración: {path}")

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: TOML no válido: {e}") from e
    return config_from_dict(raw)


def config_from_dict(raw: dict[str, Any]) -> Config:
    sections = {f.name: f.default_factory for f in dataclasses.fields(Config)}
    unknown = raw.keys() - sections.keys()
    if unknown:
        raise ConfigError(f"Secciones desconocidas: {', '.join(sorted(unknown))}")

    built = {}
    for name, factory in sections.items():
        values = raw.get(name, {})
        if not isinstance(values, dict):
            raise ConfigError(f"[{name}] debe ser una sección")
        built[name] = _build_section(name, factory(), values)
    return Config(**built)


def _build_section(name: str, defaults: Any, values: dict[str, Any]) -> Any:
    allowed = {f.name for f in dataclasses.fields(defaults)}
    unknown = values.keys() - allowed
    if unknown:
        raise ConfigError(f"Claves desconocidas en [{name}]: {', '.join(sorted(unknown))}")
    converted = {}
    for key, value in values.items():
        expected = type(getattr(defaults, key))
        if expected is tuple:
            # En TOML las listas se escriben [..]; aquí las guardamos como tuplas inmutables.
            if type(value) is not list or not all(type(v) is str for v in value):
                raise ConfigError(f"{name}.{key} debe ser una lista de textos")
            value = tuple(value)
        elif expected is float and type(value) is int:
            value = float(value)  # en TOML "1" es un entero; para un decimal lo aceptamos
        # Comparación exacta de tipos: en Python True es un int y no lo queremos aceptar.
        elif type(value) is not expected:
            raise ConfigError(f"{name}.{key} debe ser de tipo {expected.__name__}")
        converted[key] = value
    section = dataclasses.replace(defaults, **converted)
    section.validate()
    return section
