"""Control de la escucha: abrir el micrófono, cerrarlo, transcribir y enviar.

Lo usan dos "botones":
  - la terminal (Enter / Enter);
  - el atajo global de teclado (cada pulsación alterna escuchar / parar).
"""

import logging
import re
import threading
import time
from collections.abc import Callable

from ..core.assistant import Assistant
from ..core.state import State
from .recorder import Recorder, RecorderError
from .stt import SttError, WhisperTranscriber

log = logging.getLogger(__name__)


class VoiceController:
    def __init__(
        self,
        assistant: Assistant,
        recorder: Recorder,
        transcriber: WhisperTranscriber,
        *,
        say: Callable[[str], None],
        notify: Callable[[str], None] | None = None,
        max_seconds: int = 60,
        on_done: Callable[[], None] | None = None,
        warm_up: Callable[[], None] | None = None,
        heard: Callable[[str], None] | None = None,
    ) -> None:
        # Lo que has dicho (la transcripción) va aparte del resto de avisos: en modo servicio `say` va al registro
        # normal (journal) y tu voz no debe quedar escrita ahí; el servicio la manda solo al nivel DEBUG.
        self.heard = heard or say
        self.warm_up = warm_up or (lambda: None)  # cargar el modelo mientras hablas
        self.on_done = on_done or (lambda: None)  # tras una petición por atajo (p. ej. reponer el prompt)
        self.assistant = assistant
        self.recorder = recorder
        self.transcriber = transcriber
        self.say = say
        self.notify = notify or (lambda text: None)
        self.max_seconds = max_seconds
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self.recorder.recording

    def start(self, **recorder_options) -> bool:
        """Abre el micrófono. Si ARTHUR está hablando, le corta y escucha."""
        with self._lock:
            if self.recorder.recording:
                return True
            state = self.assistant.state
            if state is State.SPEAKING and self.assistant.speaker:
                self.assistant.speaker.stop()
                state = self._wait_until_idle()
            if state is not State.IDLE:
                self.say("  ⏳ Espera, todavía estoy con la petición anterior.")
                self.notify("Espera, todavía estoy con la petición anterior.")
                return False
            self.assistant.start_listening()
            try:
                self.recorder.start(**recorder_options)
            except RecorderError as e:
                self.assistant.stop_listening()
                self.say(f"  ⚠ {e}")
                return False
        self.say("  🎙 Te escucho…")
        self.warm_up()
        return True

    def _wait_until_idle(self, timeout: float = 3.0) -> State:
        deadline = time.monotonic() + timeout
        while self.assistant.state is not State.IDLE and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.assistant.state

    def cancel(self) -> None:
        with self._lock:
            if self.recorder.recording:
                self.recorder.stop()
                self.assistant.stop_listening()
                self.say("  (grabación cancelada)")

    def finish(self) -> None:
        """Cierra el micrófono, transcribe y le pasa el texto a ARTHUR. Bloquea hasta la respuesta."""
        with self._lock:
            if not self.recorder.recording:
                return
            audio = self.recorder.stop()
        if self.recorder.truncated:
            self.say(f"  (he cortado la grabación al llegar al límite de {self.max_seconds} s)")
        self.say("  … transcribiendo")
        try:
            text = self.transcriber.transcribe(audio)
        except SttError as e:
            self.say(f"  ⚠ {e}")
            self.assistant.stop_listening()
            return
        if not text:
            self.say("  No te he entendido. Vuelve a intentarlo.")
            self.notify("No te he entendido")
            self.assistant.stop_listening()
            return
        text = strip_wake_phrase(text)
        if not text:
            self.say("  ¿Sí? Dime qué necesitas.")
            self.assistant.stop_listening()
            return
        self.heard(f"tú (voz) › {text}")
        self.assistant.handle_text(text)

    def hands_free(self, *, preroll: bytes = b"", noise_floor: float | None = None,
                   chime: Callable[[], None] | None = None) -> None:
        """Tras la palabra de activación: escucha, detecta cuándo terminas y responde.

        El micrófono se abre YA (sin esperar al ding, que suena a la vez y no cuenta como voz),
        con el último segundo de audio previo por si dijiste la petición de un tirón.
        """
        # Mientras suena el ding, nada cuenta como voz (con el micro bien ajustado, el ding se oye fuerte).
        if not self.start(initial=preroll, noise_floor=noise_floor, ignore_seconds=CHIME_MAX if chime else 0.0):
            return
        if chime:
            def ding() -> None:
                chime()
                self.recorder.ignore_for(CHIME_ECHO)  # el ding ya ha sonado: solo falta su eco
            threading.Thread(target=ding, name="chime", daemon=True).start()
        if self.recorder.wait_for_end():
            self.finish()
        else:
            self.say("  (no te he oído decir nada)")
            self.cancel()

    def toggle(self) -> str:
        """Para el atajo de teclado: si no escucha, empieza; si escucha, termina (en segundo plano)."""
        if self.recorder.recording:
            threading.Thread(target=self._finish_safely, name="voice-turn", daemon=True).start()
            return "procesando"
        if not self.start():
            return "ocupado"
        self.notify("Te escucho… pulsa el atajo otra vez cuando termines")
        return "escuchando"

    def _finish_safely(self) -> None:
        try:
            self.finish()
        except Exception:
            log.exception("Fallo procesando la petición por voz")
        finally:
            self.on_done()


CHIME_MAX = 2.0   # como mucho, lo que se ignora si el ding tarda en sonar
CHIME_ECHO = 0.25  # tras el ding, su eco en la habitación

# Whisper a veces oye «Hey» como «El», «Oh» o «A»: también lo quitamos.
WAKE_PREFIX = re.compile(r"^\s*(?:(?:hey|hei|ey|eh|el|la|oh|ah|a|oye)[\s,]+)?(?:arthur|artur|arturo|ártur)\b[\s,.!]*",
                         re.I)


def strip_wake_phrase(text: str) -> str:
    """Quita el «Hey Arthur» del principio (viene en el audio previo si lo dijiste de un tirón)."""
    return WAKE_PREFIX.sub("", text, count=1).strip()
