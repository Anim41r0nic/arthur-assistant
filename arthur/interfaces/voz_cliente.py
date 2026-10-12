"""La voz de un aparato que habla con el ARTHUR del servidor (fase S4, paso 4; docs/servidor.md).

El aparato pone el micrófono y el altavoz; oír (whisper) y hablar (Piper) lo hace el servidor (arthur-voz):
  1. graba lo que dices aquí (pw-record, 16 kHz mono, como siempre);
  2. lo manda al servidor (POST /api/v1/voice), que lo transcribe, se lo pasa a ARTHUR y devuelve la respuesta;
  3. pide la voz de la respuesta (POST /api/v1/speech) y la reproduce aquí (pw-play). Se puede cortar.

El audio solo sale del aparato hacia TU servidor (Tailscale o casa), con la llave de este aparato.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import queue
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable

from ..voice.recorder import SAMPLE_RATE, Recorder, RecorderError
from ..voice.stt import is_silent
from ..voice.tts import LEAD_SECONDS, SLICE_SECONDS, rms_level

log = logging.getLogger(__name__)

SERVER_MAX_SECONDS = 60  # lo máximo que acepta el servidor por petición
WINDOWS = sys.platform == "win32"  # el aparato Windows (docs/windows.md): otro altavoz, otros avisos, otro buzón


class WavPlayer:
    """Reproduce un WAV por el altavoz de este aparato y permite cortarlo a mitad."""

    def __init__(self, *, volume: float = 1.0, command: Callable[[int, float], list[str]] | None = None,
                 on_level: Callable[[float], None] | None = None) -> None:
        """`command` solo se cambia en los tests (un reproductor falso: nada debe sonar).
        `on_level`: el volumen de lo que va sonando, para que el Orb lata al hablar."""
        self.volume = volume
        self.on_level = on_level or (lambda level: None)
        self.command = command or (lambda rate, volume: [
            "pw-play", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16",
            "--volume", f"{volume:.2f}", "-",
        ])
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def playing(self) -> bool:
        return self._proc is not None

    def play(self, wav_bytes: bytes) -> None:
        """Bloquea hasta que termina de sonar (o hasta stop())."""
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError("Audio de ARTHUR en un formato inesperado")
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        with self._lock:
            self.stop()  # una sola voz a la vez
            proc = subprocess.Popen(self.command(rate, self.volume), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._proc = proc
        # A ritmo real (como la voz del portátil, voice/tts.py): así el nivel que ve el Orb va con lo que suena.
        step = int(rate * SLICE_SECONDS) * 2
        start, sent = time.monotonic(), 0.0
        try:
            for i in range(0, len(pcm), step):
                if self._proc is not proc:
                    break  # stop(): lo han cortado
                piece = pcm[i:i + step]
                wait = start + sent - LEAD_SECONDS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                proc.stdin.write(piece)
                self.on_level(rms_level(piece))
                sent += len(piece) / 2 / rate
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass  # lo han cortado mientras le pasábamos el audio
        proc.wait()
        self.on_level(0.0)
        with self._lock:
            if self._proc is proc:
                self._proc = None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()


def make_player(*, volume: float = 1.0, on_level: Callable[[float], None] | None = None, windows: bool = WINDOWS):
    """El altavoz de este aparato: pw-play en Linux, winsound en Windows. Los dos cumplen lo mismo
    (play bloquea, stop corta, playing, on_level)."""
    if windows:
        from ..voice.win_audio import WinSoundPlayer

        return WinSoundPlayer(volume=volume, on_level=on_level)
    return WavPlayer(volume=volume, on_level=on_level)


def wav_bytes(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return out.getvalue()


class RemoteVoice:
    """Hablar con el ARTHUR del servidor desde este aparato. `core` es un RemoteCore."""

    def __init__(self, core, *, recorder: Recorder | None = None, player: WavPlayer | None = None,
                 say: Callable[[str], None] = print, max_seconds: int = SERVER_MAX_SECONDS,
                 heard: Callable[[str], None] | None = None,
                 events: Callable[[str, dict], None] | None = None) -> None:
        """`heard`: dónde va lo que has dicho y lo que responde (en el servicio, solo al nivel DEBUG del registro);
        `say`: los avisos de estado; `events`: lo que ve el Orb del aparato (estado, mensajes, nivel del micro)."""
        max_seconds = min(max_seconds, SERVER_MAX_SECONDS)
        self.heard = heard or say
        self.events = events or (lambda kind, data: None)
        self.core = core
        self.recorder = recorder or Recorder(
            max_seconds=max_seconds, on_level=lambda level: self.events("audio_level", {"level": round(level, 3),
                                                                                        "source": "mic"}))
        self.player = player or make_player()
        self.say = say
        self.max_seconds = max_seconds
        self._turn = threading.Lock()  # un turno de voz a la vez (atajo, «Hey Arthur» y terminal)
        self._cut = threading.Event()  # «calla»: corta la frase que suena Y las que esperan su turno
        self._by_toggle = False        # el turno en marcha lo abrió el atajo (y solo el atajo lo cierra)

    @property
    def recording(self) -> bool:
        return self.recorder.recording

    @property
    def idle(self) -> bool:
        """Ni escuchando, ni esperando al servidor, ni hablando."""
        return not self.busy and not self.recording and not self.player.playing

    def _state(self, state: str) -> None:
        self.events("state", {"to": state})

    @property
    def busy(self) -> bool:
        return self._turn.locked()

    def stop_speaking(self) -> None:
        """Calla ya (✋, o le vuelves a hablar): la frase que suena y las que venían detrás."""
        self._cut.set()
        self.player.stop()

    def start(self, **recorder_options) -> bool:
        """Abre el micrófono. Si ARTHUR estaba hablando, le corta."""
        self.stop_speaking()
        try:
            self.recorder.start(**recorder_options)
        except RecorderError as e:
            self.say(f"  ⚠ {e}")
            return False
        self._state("listening")
        self.say("  🎙 Te escucho…")
        return True

    def cancel(self) -> None:
        if self.recorder.recording:
            self.recorder.stop()
            self._state("idle")

    def finish(self) -> str | None:
        """Cierra el micrófono, lo manda al servidor y dice la respuesta. Devuelve la respuesta (o None)."""
        try:
            return self._finish()
        finally:
            self._state("idle")

    def _finish(self) -> str | None:
        from .core import RemoteError

        audio = self.recorder.stop()
        if self.recorder.truncated:
            self.say(f"  (he cortado la grabación al llegar al límite de {self.max_seconds} s)")
        if is_silent(audio):  # ni se envía: no gasta red ni el whisper del servidor
            self.say("  (no te he oído decir nada)")
            return None
        self._state("thinking")
        self.say("  … enviando al servidor")
        if hasattr(self.core, "voice_stream"):
            try:
                return self._finish_streamed(wav_bytes(audio))
            except RemoteError as e:
                if "respondió 404" not in str(e):  # un servidor anterior a la 0.56.0: se hace como antes
                    self.say(f"  ⚠ {e}")
                    return None
        try:
            result = self.core.voice(wav_bytes(audio))
        except RemoteError as e:
            self.say(f"  ⚠ {e}")
            return None
        heard, reply = result.get("heard") or "", result.get("reply")
        if not heard or not isinstance(reply, str):
            self.say("  No te he entendido. Vuelve a intentarlo.")
            return None
        self.heard(f"tú (voz) › {heard}")
        self.heard(f"arthur › {reply}")
        self.events("user_message", {"text": heard})
        self.events("assistant_message", {"text": reply})
        self.speak(reply)
        return reply

    def _finish_streamed(self, wav: bytes) -> str | None:
        """Voz a trozos (0.56.0): el servidor manda cada frase con su audio en cuanto la tiene, y aquí se van
        diciendo en orden mientras él sigue escribiendo. Bloquea hasta que acaba de hablar, como speak()."""
        self._cut = cut = threading.Event()  # el de ESTE turno: un «calla» de antes no cuenta
        audios: queue.Queue = queue.Queue()

        def play_all() -> None:
            while (audio := audios.get()) is not None:
                if cut.is_set():
                    continue  # te ha cortado: lo que quedaba en cola ya no suena
                self._state("speaking")
                try:
                    self.player.play(audio)
                except (ValueError, wave.Error, OSError) as e:
                    log.warning("No he podido reproducir la voz de ARTHUR: %s", e)

        player = threading.Thread(target=play_all, name="remote-speech", daemon=True)
        player.start()
        heard, reply = "", None
        try:
            for item in self.core.voice_stream(wav):
                if cut.is_set():
                    break  # al salir se cierra la conexión: el servidor deja de gastar voz en frases que no oirás
                if "heard" in item:
                    heard = item["heard"] or ""
                    if heard:
                        self.heard(f"tú (voz) › {heard}")
                        self.events("user_message", {"text": heard})
                elif "say" in item:
                    try:
                        audios.put(base64.b64decode(item.get("audio") or "", validate=True))
                    except (binascii.Error, ValueError):
                        log.warning("El servidor ha mandado una voz que no entiendo")
                elif "reply" in item:
                    reply = item["reply"]
        finally:
            audios.put(None)
        if not heard or not isinstance(reply, str):
            player.join()
            if not cut.is_set():
                self.say("  No te he entendido. Vuelve a intentarlo.")
            return None
        self.heard(f"arthur › {reply}")
        self.events("assistant_message", {"text": reply})
        player.join()
        return reply

    def speak(self, text: str) -> None:
        """La voz del servidor para este texto, por el altavoz de aquí. Si falla, ya se ha leído en pantalla."""
        from .core import RemoteError

        try:
            audio = self.core.speech(text)
        except RemoteError as e:
            self.say(f"  (sin voz: {e})")
            return
        self._state("speaking")
        try:
            self.player.play(audio)
        except (ValueError, wave.Error, OSError) as e:
            log.warning("No he podido reproducir la voz de ARTHUR: %s", e)
        finally:
            if not self.recording:  # si le has cortado para hablarle, ya está escuchando
                self._state("idle")

    def speak_in_background(self, text: str) -> None:
        threading.Thread(target=self.speak, args=(text,), name="remote-speech", daemon=True).start()

    def push_to_talk(self, input_fn: Callable[[str], str]) -> None:
        """Desde la terminal: Enter abre el micrófono, Enter lo cierra."""
        with self._turn:
            if not self.start():
                return
            self.say("  (pulsa Enter cuando termines)")
            try:
                input_fn("")
            except (EOFError, KeyboardInterrupt):
                self.cancel()
                self.say("  (grabación cancelada)")
                return
            self.finish()

    def hands_free(self, *, preroll: bytes = b"", noise_floor: float | None = None,
                   chime: Callable[[], None] | None = None) -> None:
        """Tras «Hey Arthur» o la palmada: escucha hasta que te callas y lo manda. Como en el portátil
        (voice/controller.py): el micro se abre YA con el último segundo previo, y el ding no cuenta como voz."""
        from ..voice.controller import CHIME_ECHO, CHIME_MAX

        if not self._turn.acquire(blocking=False):
            return  # ya hay un turno en marcha
        try:
            if not self.start(initial=preroll, noise_floor=noise_floor, ignore_seconds=CHIME_MAX if chime else 0.0):
                return
            if chime:
                def ding() -> None:
                    chime()
                    self.recorder.ignore_for(CHIME_ECHO)
                threading.Thread(target=ding, name="chime", daemon=True).start()
            if self.recorder.wait_for_end():
                self.finish()
            else:
                self.cancel()
                self.say("  (no te he oído decir nada)")
        finally:
            self._turn.release()

    def toggle(self, notify: Callable[[str], None] = lambda text: None) -> str:
        """Para el atajo de teclado: si no escucha, empieza; si escucha, termina (en segundo plano)."""
        if self._by_toggle and self.recorder.recording:
            self._by_toggle = False

            def finish() -> None:
                try:
                    self.finish()
                except Exception:
                    log.exception("Fallo procesando la petición por voz")
                finally:
                    self._turn.release()
            threading.Thread(target=finish, name="voice-turn", daemon=True).start()
            return "procesando"
        if not self._turn.acquire(blocking=False):
            notify("Espera, todavía estoy con la petición anterior.")
            return "ocupado"
        if not self.start():
            self._turn.release()
            return "error"
        self._by_toggle = True
        notify("Te escucho… pulsa el atajo otra vez cuando termines")
        return "escuchando"


CONFIRM_SECONDS = 55  # el servidor espera 60 s y luego responde NO él solo


def notify_confirm(summary: str, *, timeout: float = CONFIRM_SECONDS, command: list[str] | None = None) -> bool:
    """Confirmación sin terminal ni Orb: una notificación con botones «Sí» y «No».
    Solo el botón «Sí» confirma; cerrarla, pulsar en el texto o no contestar a tiempo es NO."""
    cmd = command or ["notify-send", "--app-name=ARTHUR", "--urgency=critical", f"--expire-time={int(timeout * 1000)}",
                      "--action=no=No", "--action=si=Sí", "--", "ARTHUR · ¿Confirmas?", summary[:300]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    except OSError as e:
        log.warning("No puedo preguntar la confirmación (%s): cuenta como NO", e)
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "si"


def default_confirm(windows: bool = WINDOWS) -> Callable[[str], bool]:
    """Sin Orb abierto, la confirmación sale en una notificación con Sí/No: notify-send en Linux, la de Windows allí."""
    if windows:
        from ..integrations.win_escritorio import windows_confirm

        return lambda summary: windows_confirm(summary, timeout=CONFIRM_SECONDS)
    return notify_confirm


def default_notify(windows: bool = WINDOWS) -> Callable[[str], None]:
    if windows:
        from ..integrations.win_escritorio import windows_notify

        return windows_notify
    from ..tools.programs import desktop_notify

    return desktop_notify


# --- el servicio del portátil en modo cliente -------------------------------------------------------------------

SERVER_EVENTS_FOR_ORB = {"tool_started", "tool_finished"}  # del servidor, el Orb solo enseña las herramientas


class DeviceHands:
    """Para los gestos en modo cliente: lo que en el portátil hacía el router (✋ pausa la música, ✌️ pausa/play),
    aquí lo hacen las manos de ESTE aparato, con sus reglas y su auditoría (LocalExecutor)."""

    def __init__(self, executor) -> None:
        self.executor = executor

    def execute(self, call):
        from types import SimpleNamespace

        if self.executor is None:
            return SimpleNamespace(status="error", output="Este aparato no tiene manos")
        status, output = self.executor.run(call.name, dict(call.arguments))
        return SimpleNamespace(status=status, output=output)


class DeviceAnnouncer:
    """Para la cola de avisos (app.attach_announcements), que espera algo con announce(texto) -> bool como el
    asistente del portátil: si ARTHUR está libre, lo dice aquí (con la voz del servidor) y lo enseña en el Orb;
    si no, devuelve False y la cola lo vuelve a intentar en un momento."""

    def __init__(self, voice: RemoteVoice, emit: Callable[[str, dict], None]) -> None:
        self.voice = voice
        self.emit = emit

    def announce(self, text: str) -> bool:
        if not self.voice.idle:
            return False
        self.emit("assistant_message", {"text": text, "announcement": True})
        self.voice.speak(text)  # en el hilo de la cola: espera a terminar, como el portátil
        return True


def start_client_orb(config, bus, get_state: Callable[[], str], gestures=None):
    """El Orb de este aparato (fase S7): la misma página y el mismo servidor local que en el portátil
    (api/server.py: 127.0.0.1, token, cookie…), alimentado por lo que pasa aquí. Devuelve (servidor, broker) o
    (None, None) si el puerto está ocupado: el servicio sigue sin Orb (confirmaciones por notificación)."""
    from ..api.server import OrbServer
    from ..security.broker import ConfirmationBroker

    try:
        broker = ConfirmationBroker(bus, timeout=CONFIRM_SECONDS)
        server = OrbServer(bus, port=config.orb.port, assistant_name=config.assistant.name, get_state=get_state,
                           broker=broker, gestures=gestures)
    except OSError as e:
        log.warning("Sigo sin Orb: no puedo usar el puerto %s (%s)", config.orb.port, e.strerror)
        return None, None
    broker.has_clients = lambda: server.client_count() > 0
    server.start()
    return server, broker


def run_client_service(core, config, *, control_path, executor=None, specs=None,
                       stop: threading.Event | None = None, notify: Callable[[str], None] | None = None,
                       ask_confirm: Callable[[str], bool] | None = None, voice: RemoteVoice | None = None,
                       wake_listener_factory=None, orb_window_factory=None, gestures=None,
                       camera_notify: Callable[[str], None] | None = None, notifications=None,
                       windows: bool = WINDOWS) -> int:
    """`arthur --servicio-cliente` (lo arranca systemd): el portátil como aparato del ARTHUR del servidor.
    Escucha «Hey Arthur» (y la palmada) y el atajo, manda tu voz al servidor y reproduce la respuesta; le presta
    sus «manos» (ventanas, apps, volumen…) y da los avisos. Tiene su Orb, como el portátil: «Hey Arthur» lo trae a
    pantalla completa y las confirmaciones salen allí (si no está abierto, en una notificación con Sí/No).
    Lo que dices y lo que responde solo va al registro con el nivel DEBUG, como en el servicio de siempre.
    `gestures`: GestureSwitch si los gestos están permitidos en config.toml: la cámara y su burbuja van aquí y los
    gestos actúan sobre este aparato (callar, Sí/No en el Orb, repetir, saludar, música).
    `notifications`: NotificationWatcher (los avisos del escritorio, p. ej. WhatsApp Web) o None: ARTHUR los dice
    en voz alta aquí, cuando está libre, con la misma cola que el portátil completo (app.attach_announcements).
    `windows`: el aparato Windows (fase A, docs/windows.md): altavoz con winsound, avisos y confirmaciones con
    notificaciones de Windows, buzón con llave (control.key) y, de momento, sin «Hey Arthur» ni Orb a pantalla
    completa (el Orb se abre en el navegador con `arthur --abrir-orb`). Lo demás es igual.
    `notify`, `ask_confirm`, `voice`, `wake_listener_factory`, `orb_window_factory`, `camera_notify` y `windows`
    solo se cambian en los tests."""
    import signal

    from ..api.control import ControlUnavailable, make_control_server
    from ..core.events import Event, EventBus
    from ..security.confirm import ConfirmationRequest
    from ..tools.base import Risk
    from .core import CoreEventLink, RemoteError

    notify = notify or default_notify(windows)
    ask_confirm = ask_confirm or default_confirm(windows)
    bus = EventBus()
    current = {"state": "idle"}
    state_lock = threading.Lock()

    def emit(kind: str, data: dict) -> None:
        """Lo que pasa en este aparato, al Orb."""
        if kind == "state":
            with state_lock:
                old, current["state"] = current["state"], data["to"]
            if old != data["to"]:
                bus.publish(Event("state_changed", {"from": old, "to": data["to"]}))
        else:
            bus.publish(Event(kind, data))

    def say(text: str) -> None:
        text = text.strip()
        log.info("%s", text)
        if text.startswith(("⚠", "No te he entendido")):
            notify(text.lstrip("⚠ "))

    voice = voice or RemoteVoice(
        core, say=say, max_seconds=config.voice.max_record_seconds, heard=lambda text: log.debug("%s", text),
        player=make_player(volume=config.voice.volume, windows=windows,
                           on_level=lambda level: emit("audio_level", {"level": round(level, 3), "source": "tts"})))
    voice.events = emit
    server, broker = start_client_orb(config, bus, lambda: current["state"], gestures)

    def orb_url() -> str | None:
        return server.url if server else None

    window = None
    if server is not None and config.orb.fullscreen_on_wake and not windows:  # el Orb a pantalla completa es de Hyprland
        from .service import make_orb_window

        window = (orb_window_factory or make_orb_window)(orb_url)
    try:
        info = core.whoami()
        log.info("Conectado a %s en %s como «%s»", info.get("name", "ARTHUR"), core.url, info.get("device"))
    except RemoteError as e:
        log.warning("Aún no llego al servidor (%s); lo seguiré intentando", e)

    def confirm(details: dict) -> bool:
        summary = str(details.get("summary", ""))
        voice.speak_in_background(f"¿Confirmas? {summary}")
        if broker is not None and broker.has_clients():  # hay un Orb abierto: se pregunta allí, a la vista
            if window is not None:
                threading.Thread(target=window.show, name="orb-show", daemon=True).start()
            try:
                risk = Risk(str(details.get("risk", "")))
            except ValueError:
                risk = Risk.PRIVATE  # sin saberlo, el más prudente que admite el Orb (el Sí solo con clic)
            answer = broker.ask(ConfirmationRequest(tool=str(details.get("tool", ""))[:64], risk=risk,
                                                    summary=summary))
            if answer is not None:
                return answer
        return ask_confirm(summary)

    def announce(text: str) -> None:
        notify(text)
        emit("assistant_message", {"text": text})
        voice.speak_in_background(text)

    def from_server(event: dict) -> None:
        if event.get("type") in SERVER_EVENTS_FOR_ORB and isinstance(event.get("data"), dict):
            bus.publish(Event(event["type"], event["data"]))

    link = CoreEventLink(core, executor=executor, specs=specs, ask_confirm=ask_confirm, announce=announce, say=say,
                         on_event=from_server, confirm_details=confirm)
    link.start()

    def ask(text: str) -> str:  # `arthur` en una terminal, mientras este servicio está en marcha
        if not text:
            return "error: texto vacío"
        try:
            reply = core.ask(text)
        except RemoteError as e:
            return f"error: {e}"
        emit("user_message", {"text": text})
        emit("assistant_message", {"text": reply})
        return reply

    handlers = {
        "status": lambda _: f"ok {config.assistant.name} cliente de {core.url}",
        "orb-url": lambda _: orb_url() or "error: el Orb no está activo",
        "toggle-listen": lambda _: voice.toggle(notify),
        "ask": ask,
    }
    if notifications is not None:
        from ..app import attach_announcements

        attach_announcements(notifications, bus, DeviceAnnouncer(voice, emit), config)
    camera = None
    if gestures is not None:
        from .service import attach_gestures

        def repeat(text: str) -> bool:  # ☝️ y 👋: solo si ARTHUR no está haciendo nada (si no, espera o no saluda)
            if not voice.idle:
                return False
            voice.speak_in_background(text)
            return True

        camera = attach_gestures(
            bus, gestures, config.gestures, router=DeviceHands(executor), audit=getattr(executor, "audit", None),
            stop_speaking=voice.stop_speaking, broker=lambda: broker, repeat=repeat, notify=camera_notify,
            show_orb=window.show if window else None, hide_orb=window.hide if window else None)
        # El atajo de la cámara (Super+Shift+G, scripts/arthur-camara): igual que en el servicio completo.
        handlers["toggle-camera"] = lambda _: gestures.toggle(by="atajo")
    try:
        control = make_control_server(handlers, control_path)
    except ControlUnavailable as e:
        # El buzón del atajo es de UN solo ARTHUR: el servicio de siempre (arthur.service) o este, no los dos.
        hint = "Administrador de tareas" if windows else "systemctl --user stop arthur"
        log.error("%s: para el otro antes (%s)", e, hint)
        link.stop()
        if server:
            server.stop()
        return 1  # (la cámara aún no se ha podido encender: nace apagada)
    control.start()
    # «Hey Arthur» en Windows llegará en la fase B (Vosk necesita su propio micro abierto): de momento, el atajo.
    listener = None if windows and wake_listener_factory is None else start_remote_wake(
        config, voice, factory=wake_listener_factory, orb_window=window)

    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())  # systemctl stop / Ctrl+C
    log.info("ARTHUR (cliente del servidor) en marcha: atajo sí, «Hey Arthur» %s, Orb %s, gestos %s",
             "sí" if listener else "no", "sí" if server else "no", "sí" if camera else "no")
    try:
        stop.wait()
    finally:
        if camera:
            camera.turn_off("servicio")  # y si se cae sin llegar aquí, bwrap --die-with-parent la apaga
        if listener:
            listener.stop()
        voice.cancel()
        voice.stop_speaking()
        link.stop()
        control.stop()
        if server:
            server.stop()
    return 0


ORB_HIDE_DELAY = 8.0  # como en el portátil (service.py): el Orb sigue a la vista un poco tras la respuesta


def hide_orb_when_done(voice: RemoteVoice, listener, window, *, limit: float = 300) -> None:
    """Tras un turno de «Hey Arthur»: cuando ARTHUR acaba (y pasan `delay` s), quita el Orb de en medio,
    salvo que le hayas vuelto a llamar mientras tanto."""
    turn = getattr(listener, "wake_count", 0)
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and not voice.idle:
        time.sleep(0.2)
    time.sleep(ORB_HIDE_DELAY)
    if getattr(listener, "wake_count", 0) == turn and voice.idle:
        window.hide()


def start_remote_wake(config, voice: RemoteVoice, *, factory=None, orb_window=None):
    """«Hey Arthur» y la palmada, detectados AQUÍ (Vosk en el portátil): tienen que oír el micro al momento.
    Si falta algo (desactivado, sin Vosk o sin modelo), el servicio sigue solo con el atajo."""
    cfg = config.wake
    if not cfg.enabled:
        return None
    if factory is None and not cfg.model_path.is_dir():
        log.warning("Palabra de activación desactivada: no encuentro el modelo de Vosk en %s", cfg.model_path)
        return None
    from ..voice.clap import ClapDetector
    from ..voice.tts import chime
    from ..voice.wakeword import WakeWordListener

    listener = None

    def on_wake() -> None:
        if orb_window is not None:  # el Orb a pantalla completa mientras le hablas (como en el portátil)
            threading.Thread(target=orb_window.show, name="orb-show", daemon=True).start()
        voice.hands_free(preroll=listener.preroll(), noise_floor=listener.noise_floor,
                         chime=chime if cfg.chime else None)
        if orb_window is not None:
            hide_orb_when_done(voice, listener, orb_window)

    make = factory or WakeWordListener
    listener = make(on_wake, is_idle=lambda: not voice.busy and not voice.player.playing, phrase=cfg.phrase,
                    variants=cfg.variants, min_confidence=cfg.min_confidence, model_path=cfg.model_path,
                    clap=ClapDetector(cfg.clap_peak) if cfg.double_clap else None)
    try:
        listener.start()
    except ImportError:
        log.warning("Palabra de activación desactivada: falta Vosk (sudo pacman -S python-vosk)")
        return None
    except Exception:
        log.exception("Palabra de activación desactivada: no he podido cargar el modelo de Vosk")
        return None
    return listener
