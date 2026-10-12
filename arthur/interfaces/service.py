"""ARTHUR como servicio (sin terminal) y el cliente para hablarle desde una terminal.

Servicio (`arthur --servicio`, lo arranca systemd con tu escritorio):
  - te escucha con el atajo de teclado y te responde en voz alta;
  - te avisa de los mensajes (notificaciones);
  - las confirmaciones te salen en el Orb (`arthur --abrir-orb`).
  Lo que diría en la terminal va al registro (journalctl --user -u arthur).

Cliente (`arthur` en una terminal mientras el servicio está en marcha):
  envía lo que escribes al servicio por su buzón privado y muestra la respuesta.
  Un solo ARTHUR (una memoria, un historial) con varias formas de hablarle.
"""

import logging
import signal
import sys
import threading
import time
from collections.abc import Callable

from ..api.control import ControlServer, ControlUnavailable, send_command
from ..app import App
from ..tools.programs import desktop_notify
from ..voice.controller import VoiceController

log = logging.getLogger(__name__)

ASK_TIMEOUT = 600  # una petición puede incluir cargar el modelo y esperar una confirmación en el Orb


def run_service(app: App, *, orb_url: Callable[[], str | None], control_path,
                stop: threading.Event | None = None, wake=None, gestures=None, broker=None,
                camera_notify: Callable[[str], None] | None = None) -> int:
    """`stop`: para pararlo desde fuera (tests). En uso real lo para systemd con SIGTERM.
    `gestures`: GestureSwitch si los gestos están permitidos; `broker`: las confirmaciones del Orb.
    `camera_notify`: aviso de escritorio al encender/apagar la cámara (los tests ponen uno falso)."""
    def say(text: str) -> None:
        log.info("%s", text.strip())

    # El registro (journal y arthur.log) cuenta lo que pasa, no el contenido de tus conversaciones: lo que dices
    # y lo que ARTHUR responde (o avisa: mensajes de WhatsApp con announce_text) solo salen con el nivel DEBUG.
    app.bus.subscribe("assistant_message", lambda e: log.debug("%s: %s", app.config.assistant.name, e.data["text"]))
    handlers: dict[str, Callable[[str], str]] = {
        "status": lambda _: f"ok {app.config.assistant.name} {app.assistant.state}",
        "orb-url": lambda _: orb_url() or "error: el Orb no está activo",
        # Lo escrito en otra terminal: sin voz (lo estás leyendo). Si algo necesita
        # confirmación, se pregunta en el Orb (no es el hilo principal).
        "ask": lambda text: app.assistant.handle_text(text, speak=False) if text else "error: texto vacío",
        # Diagnóstico: de qué app viene cada aviso y cómo se clasificó. Sin texto ni remitente.
        "notifications": lambda _: _describe_notifications(app),
    }
    voice = None
    if app.listener:
        recorder, transcriber = app.listener
        if warning := transcriber.check():
            log.warning("Escucha desactivada: %s", warning)
        else:
            voice = VoiceController(app.assistant, recorder, transcriber, say=say, notify=desktop_notify,
                                    heard=lambda text: log.debug("%s", text),
                                    max_seconds=app.config.voice.max_record_seconds,
                                    warm_up=getattr(app.brain, "warm_up", None))
            handlers["toggle-listen"] = lambda _: voice.toggle()
    if app.assistant.speaker and (warning := app.assistant.speaker.check()):
        log.warning("Voz desactivada: %s", warning)
        app.assistant.speaker = None

    orb_window = make_orb_window(orb_url) if app.config.orb.fullscreen_on_wake else None
    listener = start_wake_word(app, voice, wake, orb_window) if wake is not None else None

    reminders = start_reminders(app)
    start_music_ducking(app)
    power = start_power_watch(app.config.power.auto_performance) if app.power_watch else None
    camera = (start_gestures(app, gestures, broker, notify=camera_notify, orb_url=orb_url)
              if gestures is not None else None)
    if gestures is not None:
        # El atajo de la cámara (scripts/arthur-camara): como el botón del Orb, sin preguntar; la notificación y el
        # punto rojo avisan siempre. Sin gestos permitidos, la orden no existe.
        handlers["toggle-camera"] = lambda _: gestures.toggle(by="atajo")

    control = ControlServer(handlers, control_path)
    control.start()
    if hasattr(app.brain, "warm_up"):
        app.brain.warm_up()  # que la primera petición no espere a cargar el modelo
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())  # systemctl stop / Ctrl+C: parar con orden
    log.info("ARTHUR en marcha como servicio (atajo: %s, Orb: %s)", "sí" if voice else "no",
             "sí" if orb_url() else "no")
    try:
        stop.wait()
    finally:
        if camera:
            camera.turn_off("servicio")  # y si ARTHUR se cae sin llegar aquí, bwrap --die-with-parent la apaga
        if reminders:
            reminders.stop()
        if power:
            power.stop()
        if listener:
            listener.stop()
        if voice:
            voice.cancel()
        control.stop()
    return 0


def start_music_ducking(app: App) -> None:
    """Pausa la música mientras ARTHUR te escucha o te habla (y aplica lo que pidas al terminar)."""
    from ..integrations.media import MusicDucker

    player = getattr(app, "media", None)
    if player is not None:
        MusicDucker(player, app.bus)


CAMERA_NOTICES = {True: "Cámara ENCENDIDA para los gestos ({why}). {help}.",
                  False: "Cámara apagada ({why})."}


def start_gestures(app: App, switch, broker=None, *, notify: Callable[[str], None] | None = None,
                   orb_url: Callable[[], str | None] | None = None):
    """Gestos con la cámara: el vigilante nace con la cámara APAGADA; solo la enciende el usuario (Orb o voz)."""
    def stop_speaking() -> None:
        if app.assistant.speaker:
            app.assistant.speaker.stop()

    # 🤙 trae el Orb como «Hey Arthur» (se crea al usarlo: sin Hyprland solo queda en el registro que falló)
    return attach_gestures(
        app.bus, switch, app.config.gestures, router=app.router, audit=app.audit, stop_speaking=stop_speaking,
        broker=lambda: broker, repeat=app.assistant.announce, notify=notify,
        show_orb=(lambda: make_orb_window(orb_url).show()) if orb_url is not None else None,
        hide_orb=(lambda: make_orb_window(orb_url).hide()) if orb_url is not None else None)


def attach_gestures(bus, switch, config, *, router, audit, stop_speaking: Callable[[], None],
                    broker: Callable[[], object | None], repeat: Callable[[str], bool],
                    notify: Callable[[str], None] | None = None, show_orb=None, hide_orb=None):
    """Lo común del ARTHUR completo y del aparato en modo cliente (fase S7): el vigilante (cámara APAGADA al
    nacer), la tabla de acciones y el aviso cada vez que la cámara se enciende o se apaga.
    `router`: algo con execute(ToolCall) (el ToolRouter, o las manos del aparato en modo cliente)."""
    from ..security.audit import AuditLog
    from .gestos.actions import GestureActions
    from .gestos.watcher import GESTURE_HELP, REASONS, GestureWatcher

    audit = audit or AuditLog(None)
    notify = notify or (lambda text: desktop_notify(text, title="ARTHUR · Cámara", expire_ms=8_000))
    watcher = GestureWatcher(bus, config)
    GestureActions(bus, router=router, audit=audit, stop_speaking=stop_speaking, broker=broker, repeat=repeat,
                   show_orb=show_orb, hide_orb=hide_orb)

    def on_camera(event) -> None:
        # Cada vez que la cámara se enciende o se apaga (por lo que sea): a audit.jsonl y notificación de escritorio
        # (además del punto rojo del Orb), para que nunca esté encendida sin que lo sepas.
        on, reason = event.data["on"], event.data["reason"]
        audit.record(tool="camera", risk="private", arguments={"on": on}, decision=reason, status="ok")
        notify(CAMERA_NOTICES[on].format(why=REASONS[reason], help=GESTURE_HELP))

    bus.subscribe("camera_state", on_camera)
    switch.watcher = watcher
    log.info("gestures: ready (camera off)")
    return watcher


def mains_online() -> bool | None:
    """¿Está enchufado? Lee /sys (solo lectura). None si el equipo no tiene cargador que consultar."""
    from pathlib import Path

    found = None
    for supply in Path("/sys/class/power_supply").glob("*"):
        try:
            if (supply / "type").read_text().strip() == "Mains":
                found = bool(found) or (supply / "online").read_text().strip() == "1"
        except OSError:
            continue
    return found


def start_power_watch(auto_performance: bool = True):
    """Perfil de energía: avisa del ahorro y, si está activado, pone «performance» al enchufar."""
    from ..core.powerwatch import PowerWatcher
    from ..tools.base import ToolError
    from ..tools.programs import Programs

    def read_profile() -> str | None:
        try:
            return Programs().run(["powerprofilesctl", "get"], timeout=3).strip() or None
        except ToolError:
            return None  # sin power-profiles-daemon: no hay nada que vigilar

    def set_profile(profile: str) -> None:
        Programs().run(["powerprofilesctl", "set", profile], timeout=5)  # profile siempre sale de PROFILES

    def notify(text: str) -> None:
        desktop_notify(text, title="ARTHUR · Energía", expire_ms=12_000)

    watcher = PowerWatcher(read_profile, notify, read_ac=mains_online,
                           set_profile=set_profile if auto_performance else None)
    watcher.start()
    return watcher


def start_reminders(app: App):
    """Avisos de los recordatorios: ding + notificación + voz (cuando ARTHUR esté libre)."""
    if app.memory is None:
        return None
    from ..app import announce_when_free
    from ..core.reminders import ReminderScheduler
    from ..voice.tts import chime

    def on_due(text: str) -> None:
        desktop_notify(text, title="ARTHUR · Recordatorio", expire_ms=15_000)
        chime()
        announce_when_free(app.assistant, text)

    scheduler = ReminderScheduler(app.memory, on_due)
    scheduler.check()  # los que vencieron con el ordenador apagado, nada más arrancar
    scheduler.start()
    return scheduler


def make_orb_window(orb_url: Callable[[], str | None]):
    from ..integrations.hyprland import HyprlandController, HyprlandReader
    from ..integrations.orb_window import OrbWindow
    from ..tools.programs import Programs

    return OrbWindow(HyprlandController(), HyprlandReader(), url=orb_url, launch=Programs().launch)


ORB_HIDE_DELAY = 8.0  # segundos que el Orb sigue a la vista tras la respuesta


def wake_turn(app: App, voice, listener, cfg, orb_window, chime) -> None:
    """Un turno «manos libres»: Orb a la vista, escuchar, responder y volver a ocultar el Orb."""
    from ..core.state import State

    if orb_window:
        threading.Thread(target=orb_window.show, name="orb-show", daemon=True).start()
    voice.hands_free(preroll=listener.preroll(), noise_floor=listener.noise_floor,
                     chime=chime if cfg.chime else None)
    if not orb_window:
        return
    turn = listener.wake_count
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and not (app.assistant.state is State.IDLE and not voice.recording):
        time.sleep(0.2)
    time.sleep(ORB_HIDE_DELAY)
    if listener.wake_count == turn and app.assistant.state is State.IDLE and not voice.recording:
        orb_window.hide()  # nadie ha vuelto a llamar a ARTHUR mientras tanto


def start_wake_word(app: App, voice, wake, orb_window=None):
    """Arranca la escucha de «hey arthur». Si falta algo (Vosk, el modelo, la escucha), sigue sin ella."""
    from ..core.state import State
    from ..voice.clap import ClapDetector
    from ..voice.tts import chime
    from ..voice.wakeword import WakeWordListener

    cfg = app.config.wake
    if voice is None:
        log.warning("Palabra de activación desactivada: la escucha (Whisper) no está disponible")
        return None
    if not cfg.model_path.is_dir():
        log.warning("Palabra de activación desactivada: no encuentro el modelo de Vosk en %s", cfg.model_path)
        return None

    def on_wake() -> None:
        wake_turn(app, voice, listener, cfg, orb_window, chime)

    listener = WakeWordListener(
        on_wake,
        is_idle=lambda: app.assistant.state is State.IDLE and not voice.recording,
        phrase=cfg.phrase,
        variants=cfg.variants,
        min_confidence=cfg.min_confidence,
        model_path=cfg.model_path,
        clap=ClapDetector(cfg.clap_peak) if cfg.double_clap else None,
    )
    try:
        listener.start()
    except ImportError:
        log.warning("Palabra de activación desactivada: falta Vosk (sudo pacman -S python-vosk)")
        return None
    except Exception:
        log.exception("Palabra de activación desactivada: no he podido cargar el modelo de Vosk")
        return None
    wake.listener = listener
    return listener


def _describe_notifications(app: App) -> str:
    if app.notifications is None:
        return "la escucha de notificaciones está desactivada"
    items = app.notifications.latest(limit=15)
    if not items:
        return "no ha llegado ningún aviso desde que arrancó el servicio"
    lines = []
    for n in items:
        marks = [m for m in ("whatsapp", "web.whatsapp.com") if m in n.body.lower() or m in n.summary.lower()]
        lines.append(f"app={n.app!r} · clasificado={n.source} · pistas={marks or 'ninguna'}")
        lines.extend(f"    {m}" for m in n.meta)
    return "\n".join(lines)


def service_running(control_path=None) -> bool:
    try:
        return send_command("status", control_path, timeout=2).startswith("ok ")
    except ControlUnavailable:
        return False


def run_client(control_path=None, input_fn: Callable[[str], str] = input, output=sys.stdout) -> int:
    """Chat de texto contra el ARTHUR que ya está en marcha como servicio."""
    def say(text: str) -> None:
        print(text, file=output, flush=True)

    say("Conectado al ARTHUR que ya está en marcha (servicio). Escribe /salir para terminar.")
    say("  Lo que necesite confirmación te saldrá en el Orb: arthur --abrir-orb")
    while True:
        try:
            line = input_fn("tú › ").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            break
        if not line:
            continue
        if line in ("/salir", "/exit", "/quit"):
            break
        if line.startswith("/"):
            say("  En modo conectado solo se puede conversar (los comandos /… van en el ARTHUR de terminal).")
            continue
        say("  … pensando")
        try:
            say(f"arthur › {send_command('ask ' + line, control_path, timeout=ASK_TIMEOUT)}")
        except ControlUnavailable:
            say("  ⚠ El servicio de ARTHUR ya no responde (¿se ha parado?).")
            return 1
    return 0
