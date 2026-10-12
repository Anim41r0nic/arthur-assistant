"""Interfaz de texto en la terminal (la primera "cara" de ARTHUR)."""

import sys
import threading
from pathlib import Path
from collections.abc import Callable
from typing import TextIO

from ..app import App
from ..core.events import Event
from ..security.confirm import ConfirmationRequest
from ..api.control import ControlServer, ControlUnavailable
from ..tools.programs import desktop_notify
from ..voice.controller import VoiceController

COMMANDS_HELP = """Voz (si está activada): pulsa Enter con la línea vacía, habla y pulsa Enter otra vez.

Comandos:
  /estado        muestra el estado actual de ARTHUR
  /herramientas  lista las herramientas y su nivel de riesgo
  /memoria       muestra lo que ARTHUR recuerda de ti
  /olvidar N     borra el recuerdo número N
  /borrar-historial  borra el historial de conversaciones guardado
  /notificaciones    de qué apps te han llegado avisos (sin el texto)
  /ayuda         muestra esta ayuda
  /salir         cierra ARTHUR (también Ctrl+D)"""

EXIT_COMMANDS = {"/salir", "/exit", "/quit"}
YES = {"s", "si", "sí", "y", "yes"}
RISK_LABELS = {"read": "lectura", "low": "bajo riesgo", "write": "escritura", "private": "PRIVADA", "dangerous": "PELIGROSA"}


class CliConfirmer:
    """Pregunta en la terminal. Cualquier cosa que no sea «s» cuenta como NO.

    Solo pregunta en peticiones hechas desde la terminal. Las que llegan por el atajo de
    teclado (otro hilo) se rechazan: no estás mirando la terminal y no verías la pregunta.
    """

    def __init__(self, input_fn: Callable[[str], str], say: Callable[[str], None]) -> None:
        self._input = input_fn
        self._say = say

    def confirm(self, request: ConfirmationRequest) -> bool:
        if threading.current_thread() is not threading.main_thread():
            self._say(f"  ⚠ «{request.summary}» necesita tu confirmación: pídemelo desde la terminal.")
            return False
        self._say(f"  ⚠ Acción {RISK_LABELS[request.risk]}: {request.summary}")
        try:
            answer = self._input("  ¿Lo permites? [s/N] ")
        except (EOFError, KeyboardInterrupt):
            self._say("")
            return False
        return answer.strip().lower() in YES


def run_chat(
    app: App,
    *,
    show_states: bool = False,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
    control_path: Path | None = None,
) -> int:
    """`control_path`: si se indica (y la escucha está activa), abre el buzón del atajo de teclado."""
    def say(text: str) -> None:
        print(text, file=output, flush=True)

    if show_states:
        app.bus.subscribe("state_changed", lambda e: _print_state(e, say))

    name = app.config.assistant.name
    mode = " · MODO SEGURO" if app.config.security.safe_mode else ""
    say(f"{name} · cerebro: {app.assistant.brain_label}{mode} · escribe /ayuda o /salir")
    check = getattr(app.brain, "check", None)
    if check and (warning := check()):
        say(f"  ⚠ {warning}")
    speaker = app.assistant.speaker
    if speaker and (warning := speaker.check()):
        say(f"  ⚠ Voz desactivada: {warning}")
        app.assistant.speaker = speaker = None
    if speaker:
        say("  🔊 Voz activada (Ctrl+C corta la voz)")
    listener = app.listener
    if listener and (warning := listener[1].check()):
        say(f"  ⚠ Escucha desactivada: {warning}")
        listener = None
    voice = None
    control = None
    if listener:
        voice = VoiceController(
            app.assistant, listener[0], listener[1], say=say, notify=desktop_notify,
            max_seconds=app.config.voice.max_record_seconds,
            on_done=lambda: print("tú › ", end="", file=output, flush=True),
        )
        say("  🎙 Escucha activada: pulsa Enter con la línea vacía para hablar")
        if control_path:
            try:
                control = ControlServer({"toggle-listen": lambda _: voice.toggle()}, control_path)
                control.start()
                say("  ⌨ Atajo de teclado activo (scripts/arthur-ptt)")
            except (ControlUnavailable, OSError) as e:
                say(f"  ⚠ Atajo de teclado no disponible: {e}")
    # La respuesta se muestra en cuanto llega, antes de empezar a hablar.
    app.bus.subscribe("assistant_message", lambda e: say(f"{name.lower()} › {e.data['text']}"))
    if not show_states and app.brain.name != "rules":
        # Un LLM local tarda unos segundos: avisamos de que está trabajando.
        app.bus.subscribe("state_changed", lambda e: _print_progress(e, say))

    while True:
        try:
            line = input_fn("tú › ").strip()
        except (EOFError, KeyboardInterrupt):
            say("")
            break
        if not line:
            if voice:
                _voice_turn(voice, input_fn)
            continue
        if line in EXIT_COMMANDS:
            break
        if line == "/ayuda":
            say(COMMANDS_HELP)
        elif line == "/estado":
            say(f"Estado: {app.assistant.state.upper()}")
        elif line == "/herramientas":
            for tool in app.registry.all():
                say(f"  {tool.name:<16} [{RISK_LABELS[tool.risk]}] {tool.description}")
        elif line == "/notificaciones":
            _notifications_command(app, say)
        elif line == "/memoria" or line.startswith("/olvidar") or line == "/borrar-historial":
            _memory_command(app, line, say)
        elif line.startswith("/"):
            say(f"Comando desconocido: {line}. Escribe /ayuda.")
        else:
            app.assistant.handle_text(line)

    if voice:
        voice.cancel()
    if control:
        control.stop()
    say("Hasta luego.")
    return 0


def _voice_turn(voice: VoiceController, input_fn: Callable[[str], str]) -> None:
    """Pulsar para hablar desde la terminal: Enter abre el micrófono, Enter lo cierra."""
    if not voice.start():
        return
    voice.say("  (pulsa Enter cuando termines)")
    try:
        input_fn("")
    except (EOFError, KeyboardInterrupt):
        voice.cancel()
        return
    voice.finish()


def _notifications_command(app: App, say: Callable[[str], None]) -> None:
    """Diagnóstico: de qué app viene cada aviso y cómo lo clasifica ARTHUR (sin el texto)."""
    if app.notifications is None:
        say("  La escucha de notificaciones está desactivada.")
        return
    items = app.notifications.latest(limit=15)
    if not items:
        say("  No ha llegado ningún aviso desde que arranqué.")
    for n in items:
        say(f"  {n.app} · {n.source} · de: {n.summary or '?'}")


def _memory_command(app: App, line: str, say: Callable[[str], None]) -> None:
    """Comandos directos de memoria: los escribes tú, así que no pasan por el modelo."""
    if app.memory is None:
        say("La memoria está desactivada (memory.enabled = false).")
        return
    if line == "/memoria":
        memories = app.memory.list()
        say("\n".join(f"  #{m.id} [{m.kind}] {m.content}" for m in memories) or "  No recuerdo nada todavía.")
    elif line == "/borrar-historial":
        say(f"  Historial borrado ({app.memory.clear_history()} mensajes).")
    else:
        number = line.removeprefix("/olvidar").strip().lstrip("#")
        if not number.isdigit():
            say("  Uso: /olvidar N (mira los números con /memoria)")
        elif app.memory.delete(int(number)):
            say(f"  Olvidado el recuerdo #{number}.")
        else:
            say(f"  No hay ningún recuerdo #{number}.")


def _print_progress(event: Event, say: Callable[[str], None]) -> None:
    if event.data["to"] == "thinking" and event.data["from"] in ("idle", "listening"):
        say("  … pensando")


def _print_state(event: Event, say: Callable[[str], None]) -> None:
    say(f"  · {event.data['from'].upper()} → {event.data['to'].upper()}")
