"""Música y vídeo con playerctl (MPRIS): Spotify, YouTube en Firefox, VLC…

MediaPlayer: órdenes FIJAS a playerctl (nunca texto del modelo). Prefiere Spotify si está abierto.
MusicDucker: pausa la música mientras ARTHUR te escucha o te habla y la reanuda al terminar.
Las órdenes que pides durante la conversación («siguiente») se aplican al acabar ARTHUR de
hablar, para no hablarte por encima de la música.
"""

import logging
import threading

from ..core.events import Event, EventBus
from ..core.state import State
from ..tools.base import ToolError

log = logging.getLogger(__name__)

PLAYERS = "spotify,%any"  # Spotify primero; si no está, cualquier otro reproductor
COMMANDS = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next", "previous": "previous"}
# Volumen DEL REPRODUCTOR (no el del sistema) y saltos dentro de la canción: pasos fijos, nunca números del modelo.
ADJUSTMENTS = {
    "volume_up": ("volume", "0.1+"), "volume_down": ("volume", "0.1-"),
    "forward": ("position", "10+"), "back": ("position", "10-"),
}


class MediaPlayer:
    def __init__(self, programs) -> None:
        self.programs = programs
        self.ducker: "MusicDucker | None" = None  # solo en modo servicio

    def _run(self, *args: str) -> str:
        return self.programs.run(["playerctl", f"--player={PLAYERS}", *args], timeout=3).strip()

    def status(self) -> str | None:
        """'Playing', 'Paused', 'Stopped' o None si no hay ningún reproductor abierto."""
        try:
            return self._run("status") or None
        except ToolError:
            return None

    def now_playing(self) -> str:
        info = self._run("metadata", "--format", "{{playerName}}\t{{status}}\t{{artist}}\t{{title}}")
        player, status, artist, title = (info.split("\t") + ["", "", "", ""])[:4]
        state = {"Playing": "sonando", "Paused": "en pausa", "Stopped": "parado"}.get(status, status)
        song = " — ".join(x for x in (artist, title) if x) or "sin título"
        return f"{player} ({state}): {song}"

    def command(self, action: str) -> None:
        self._run(COMMANDS[action])

    def adjust(self, action: str) -> None:
        self._run(*ADJUSTMENTS[action])


class MusicDucker:
    def __init__(self, player: MediaPlayer, bus: EventBus) -> None:
        self.player = player
        self._lock = threading.Lock()
        self._was_playing = False
        self._requested: str | None = None
        self._in_turn = False  # ¿ARTHUR está escuchando, pensando o hablando?
        player.ducker = self
        bus.subscribe("state_changed", self._on_state)

    def request(self, action: str) -> bool:
        """Lo que has pedido durante la conversación: se aplica cuando ARTHUR termina. Devuelve True si se apuntó.

        Fuera de una conversación (p. ej. la palma de los gestos, con ARTHUR en reposo) no hay «final del turno» que
        esperar: devuelve False y quien llama lo aplica ya. Antes se apuntaba igual y la pausa llegaba al final de
        la SIGUIENTE conversación.
        """
        with self._lock:
            if not self._in_turn:
                return False
            self._requested = action
            return True

    def _on_state(self, event: Event) -> None:
        old, new = event.data["from"], event.data["to"]
        if old == State.IDLE and new != State.IDLE:
            playing = self.player.status() == "Playing"
            with self._lock:
                self._was_playing, self._requested, self._in_turn = playing, None, True
            if playing:
                self._safely("pause")
        elif new == State.IDLE:
            with self._lock:
                was_playing, requested = self._was_playing, self._requested
                self._was_playing, self._requested, self._in_turn = False, None, False
            threading.Thread(target=self._after_turn, args=(was_playing, requested), daemon=True).start()

    def _after_turn(self, was_playing: bool, requested: str | None) -> None:
        match requested:
            case "pause":
                self._safely("pause")
            case "toggle":
                if not was_playing:  # sonaba → se queda en pausa; no sonaba → que suene
                    self._safely("play")
            case "next" | "previous":
                self._safely(requested)
                self._safely("play")
            case "play":
                self._safely("play")
            case _ if was_playing:
                self._safely("play")  # la reanudamos tal como estaba

    def _safely(self, action: str) -> None:
        try:
            self.player.command(action)
        except ToolError as e:
            log.info("Música: no he podido hacer '%s': %s", action, e)
