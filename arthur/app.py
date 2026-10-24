"""Punto de montaje: crea las piezas y las conecta entre sí.

Es el único sitio que conoce todas las piezas. Si mañana cambiamos el cerebro
o añadimos el Orb, el cambio se hace aquí y no dentro del núcleo.
"""

import logging
import threading
from dataclasses import dataclass

from .brain import create_brain
from .brain.base import Brain
from .core.assistant import Assistant
from .core.config import Config
from .core.events import Event, EventBus
from .core.router import ToolRouter
from .core.state import StateMachine
from .security.audit import AuditLog
from .security.confirm import Confirmer
from .security.policy import Policy
from .tools import build_registry
from .tools.registry import ToolRegistry


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class App:
    config: Config
    bus: EventBus
    brain: Brain
    registry: ToolRegistry
    listener: tuple | None  # (Recorder, WhisperTranscriber) si la escucha está activada
    router: ToolRouter
    assistant: Assistant
    memory: object | None = None  # MemoryStore si la memoria está activada
    notifications: object | None = None  # NotificationWatcher si se escuchan las notificaciones
    media: object | None = None  # MediaPlayer (playerctl): música y vídeo
    power_watch: bool = True  # el servicio avisa si el portátil está en ahorro de energía (los tests lo apagan)
    audit: AuditLog | None = None  # el mismo registro de auditoría que usa el router (para los gestos)


def build_speaker(config: Config, bus: EventBus):
    """Crea la voz de ARTHUR. El nivel de audio se publica para que el Orb lata al hablar."""
    from .voice.tts import PiperSpeaker

    return PiperSpeaker(
        config.voice.model_path,
        length_scale=config.voice.speed,
        volume=config.voice.volume,
        on_level=lambda level: bus.publish(Event("audio_level", {"level": round(level, 3), "source": "tts"})),
    )


def build_listener(config: Config, bus: EventBus):
    """Crea el micrófono y el reconocimiento de voz. El nivel del micrófono también va al Orb."""
    from .voice.recorder import Recorder
    from .voice.stt import WhisperTranscriber, build_prompt

    recorder = Recorder(
        max_seconds=config.voice.max_record_seconds,
        on_level=lambda level: bus.publish(Event("audio_level", {"level": round(level, 3), "source": "mic"})),
    )
    transcriber = WhisperTranscriber(
        config.voice.stt_model_path,
        language=config.voice.language,
        beam_size=config.voice.stt_beam_size,
        prompt=build_prompt(config.assistant.name, list(config.security.trusted_apps)),
    )
    return recorder, transcriber


def attach_history(memory, bus: EventBus, assistant: Assistant, config: Config) -> None:
    """Si guardas el historial: borra lo caducado, recupera lo reciente y guarda lo nuevo."""
    memory.prune_history(config.memory.history_days)
    history = memory.recent_history(limit=12)
    pairs = [(u[1], a[1]) for u, a in zip(history, history[1:]) if u[0] == "user" and a[0] == "assistant"]
    assistant.preload(pairs[-6:])
    bus.subscribe("user_message", lambda e: memory.add_history("user", e.data["text"]))
    bus.subscribe("assistant_message", lambda e: memory.add_history("assistant", e.data["text"]))


GATHER_SECONDS = 1.5     # esperar un poco por si llegan varios mensajes seguidos (se dicen juntos)
BUSY_WAIT_SECONDS = 180  # si ARTHUR está ocupado, los avisos esperan como mucho esto
MAX_SPOKEN_TEXT = 300    # caracteres leídos por persona


def announcement(notes, *, read_text: bool) -> str:
    """Un aviso para varios mensajes, agrupados por persona (en el orden en que llegaron)."""
    from .tools.notifications import SOURCE_LABELS

    groups: dict[tuple[str, str], list[str]] = {}
    for note in notes:
        groups.setdefault((note.source, note.summary or "alguien"), []).append(
            note.body.replace("web.whatsapp.com", "").strip())
    parts = []
    for (source, who), bodies in groups.items():
        # Firefox no dice de qué web viene la notificación: no decimos "por WhatsApp" sin saberlo.
        via = "" if source == "web" else f" por {SOURCE_LABELS[source]}"
        texts = [b for b in bodies if b]
        if read_text and texts:
            said = ". ".join(t.rstrip(".") for t in texts)
            if len(said) > MAX_SPOKEN_TEXT:
                said = said[:MAX_SPOKEN_TEXT].rsplit(" ", 1)[0] + "…"
            parts.append(f"Mensaje de {who}{via}: {said}.")
        elif len(bodies) == 1:
            parts.append(f"Tienes un mensaje de {who}." if not via else f"Te ha escrito {who}{via}.")
        else:
            parts.append(f"Tienes {len(bodies)} mensajes de {who}{via}.")
    return " ".join(parts)


REPEAT_SECONDS = 60  # Firefox a veces repite la misma notificación: la idéntica de hace menos de esto se ignora


def is_repeat(seen: dict, key: tuple, now: float, *, window: float = REPEAT_SECONDS) -> bool:
    """¿Es la misma notificación que otra de hace menos de `window` segundos? Si no, la anota.

    Olvida las que ya son más viejas: `seen` guarda el texto de los mensajes, y sin limpiarlo crecería con cada
    mensaje distinto y los mantendría en RAM todo el tiempo que ARTHUR esté en marcha (se promete solo las últimas 50).
    """
    for old in [k for k, when in seen.items() if now - when >= window]:
        del seen[old]
    if key in seen:
        return True
    seen[key] = now
    return False


def announce_when_free(assistant: Assistant, text: str, *, wait: float = BUSY_WAIT_SECONDS) -> None:
    """Dice `text` en cuanto ARTHUR esté libre (en segundo plano; espera como mucho `wait` s)."""
    import time

    def worker() -> None:
        deadline = time.monotonic() + wait
        while not assistant.announce(text):
            if time.monotonic() > deadline:
                log.info("Aviso descartado: ARTHUR lleva demasiado tiempo ocupado")
                return
            time.sleep(0.5)

    threading.Thread(target=worker, name="announce", daemon=True).start()


def attach_announcements(watcher, bus: EventBus, assistant: Assistant, config: Config, *,
                         gather: float = GATHER_SECONDS) -> None:
    """Cuando llegan mensajes de una fuente vigilada, ARTHUR avisa.

    Van a una cola: si ARTHUR está ocupado (escuchándote o hablando), esperan a que quede libre
    y no se pierde ninguno. Los mensajes seguidos de la misma persona se dicen juntos.
    """
    import queue
    import time

    pending: queue.Queue = queue.Queue()
    seen: dict[tuple[str, str, str], float] = {}

    def on_notification(note) -> None:
        bus.publish(Event("message_received", {"source": note.source, "from": note.summary or note.app}))
        if note.source not in config.notifications.announce:
            return
        if is_repeat(seen, (note.source, note.summary, note.body), time.time()):
            return
        pending.put(note)

    def worker() -> None:
        while True:
            notes = [pending.get()]
            time.sleep(gather)
            deadline = time.monotonic() + BUSY_WAIT_SECONDS
            while True:
                while not pending.empty():
                    notes.append(pending.get_nowait())
                if assistant.announce(announcement(notes, read_text=config.notifications.announce_text)):
                    break
                if time.monotonic() > deadline:
                    log.info("Avisos descartados: ARTHUR lleva demasiado tiempo ocupado")
                    break
                time.sleep(0.5)  # ocupado: lo intento en un momento

    threading.Thread(target=worker, name="announcements", daemon=True).start()
    watcher.on_notification = on_notification


def build_app(config: Config, *, confirmer: Confirmer, audit: AuditLog, memory=None, notifications=None,
              wake=None, gestures=None, profile: str = "full", studio=None, apuntes=None, panel=None,
              cerebro=None) -> App:
    """`memory`: almacén de memoria (MemoryStore) o None para no recordar nada entre sesiones.
    `notifications`: NotificationWatcher (escucha de notificaciones) o None.
    `gestures`: GestureSwitch (gestos con la cámara, solo en modo servicio) o None.
    `profile`: "full" (todo en este equipo) o "core" (el ARTHUR del servidor, sin escritorio).
    `studio` / `apuntes` / `panel` / `cerebro`: enlaces a ARTHUR Studio, Apuntes, el panel de vida y el segundo cerebro
    (tools/services.py), solo con profile="core"."""
    bus = EventBus()
    from .integrations.media import MediaPlayer
    from .tools.programs import Programs

    media = MediaPlayer(Programs()) if profile == "full" else None  # el servidor no tiene música que tocar
    assistant_box: list = []  # la foto del chat la tiene el asistente, que se crea después de las herramientas
    registry = build_registry(config.security, memory=memory, notifications=notifications, wake=wake,
                              media=media, gestures=gestures, profile=profile, studio=studio, apuntes=apuntes, panel=panel,
                              cerebro=cerebro, latest_image=lambda: assistant_box[0].latest_image() if assistant_box else None)
    router = ToolRouter(registry, Policy(config.security), confirmer, audit, bus)
    brain = create_brain(config, registry, memory)
    assistant = Assistant(
        brain=brain,
        router=router,
        bus=bus,
        states=StateMachine(bus),
        history_limit=config.assistant.history_limit,
        speaker=build_speaker(config, bus) if config.voice.tts else None,
    )
    assistant_box.append(assistant)
    if notifications:
        attach_announcements(notifications, bus, assistant, config)
    if memory and config.memory.save_history:
        attach_history(memory, bus, assistant, config)
    listener = build_listener(config, bus) if config.voice.stt else None
    return App(config=config, bus=bus, brain=brain, registry=registry, listener=listener,
               router=router, assistant=assistant, memory=memory, notifications=notifications,
               media=media, audit=audit)
