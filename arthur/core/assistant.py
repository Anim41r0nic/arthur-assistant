"""El orquestador: recibe texto, consulta al cerebro y devuelve la respuesta.

No sabe nada de la interfaz ni de la voz: solo recibe texto, devuelve texto
y publica eventos. La CLI, la voz y el Orb se conectan desde fuera.

Si el cerebro propone herramientas, se las pasa al router (que decide si se
ejecutan), le devuelve los resultados al cerebro y repite, con un límite de
rondas para que nunca pueda quedarse en un bucle infinito.
"""

import logging
import re
import threading
from collections import deque
from dataclasses import replace
from collections.abc import Callable
from typing import Protocol
from datetime import datetime

from ..brain.base import Brain, BrainError, Message
from ..tools.base import Risk
from .events import Event, EventBus
from .honesty import HONEST_REPLY, NUDGE, claims_action
from .router import ToolRouter
from .spoken import SpokenStream
from .state import State, StateMachine

log = logging.getLogger(__name__)

ERROR_REPLY = "Ha ocurrido un error interno. Lo he anotado en el registro."
MAX_TOOL_ROUNDS = 5
NUMBER_TAG = re.compile(r" #\d{1,4}\b")  # los «#N» del calendario son para la pantalla: leídos en voz alta molestan
PHOTO_NOTE = "\n[Aquí iba una foto; ya no la tienes delante.]"  # lo que queda de una foto que ya no se reenvía
TOO_MANY_ROUNDS_REPLY = "He alcanzado el límite de pasos para una sola petición y me detengo aquí."


class Speaker(Protocol):
    def speak(self, text: str) -> None: ...
    def stop(self) -> None: ...


class Assistant:
    def __init__(
        self,
        brain: Brain,
        router: ToolRouter,
        bus: EventBus,
        states: StateMachine,
        history_limit: int,
        speaker: Speaker | None = None,
    ) -> None:
        self.speaker = speaker  # None = solo texto
        self._brain = brain
        self._router = router
        self._bus = bus
        self._states = states
        # Memoria a corto plazo, solo en RAM: se pierde al cerrar ARTHUR.
        # Se guarda por turnos (petición + herramientas + respuesta) para que,
        # al olvidar lo antiguo, nunca quede un resultado sin su petición.
        self._turns: deque[list[Message]] = deque(maxlen=history_limit)
        # Una petición cada vez: evita que dos entradas se mezclen.
        self._lock = threading.Lock()
        self._acted = False  # lo mismo que `acted` en _run_turn, para la voz a trozos (core/spoken.py)

    @property
    def state(self) -> State:
        return self._states.state

    @property
    def brain_label(self) -> str:
        return self._brain.label

    def preload(self, exchanges: list[tuple[str, str]]) -> None:
        """Carga conversaciones de sesiones anteriores (si guardas el historial)."""
        for user_text, reply in exchanges:
            self._turns.append([Message("user", user_text), Message("assistant", reply)])

    def announce(self, text: str) -> bool:
        """Aviso espontáneo (p. ej. «Te ha escrito Laura»). Si ARTHUR está ocupado, no interrumpe."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._states.state is not State.IDLE:
                return False
            self._states.transition(State.THINKING)
            self._states.transition(State.SPEAKING)
            self._bus.publish(Event("assistant_message", {"text": text, "announcement": True}))
            self._speak(text)
            self._states.transition(State.IDLE)
            return True
        finally:
            self._lock.release()

    def start_listening(self) -> None:
        """El micrófono se abre: estado LISTENING (el Orb se pone en modo escucha)."""
        self._states.transition(State.LISTENING)

    def stop_listening(self) -> None:
        """Se acabó de escuchar sin petición (silencio, error…): vuelta a IDLE."""
        if self._states.state is State.LISTENING:
            self._states.transition(State.IDLE)

    def handle_text(self, text: str, *, speak: bool = True, images: tuple[str, ...] = (),
                    on_speech: Callable[[str], None] | None = None) -> str:
        """`images`: fotos en base64 que van con el mensaje (solo si el cerebro tiene visión, brain.vision).
        `on_speech`: recibe la respuesta a frases según se escribe, para decirla en voz alta sin esperar al final
        (core/spoken.py). Lo que recibe ya pasó la regla de honestidad; la respuesta devuelta es la de siempre."""
        text = text.strip()
        if not text:
            self.stop_listening()
            return ""
        with self._lock:
            log.debug("Entrada: %r", text)
            self._bus.publish(Event("user_message", {"text": text}))
            self._states.transition(State.THINKING)
            if images:
                self._forget_old_images()
            turn = [Message("user", text, timestamp=datetime.now(), images=tuple(images))]
            spoken = None
            if on_speech is not None:
                self._acted = False
                spoken = SpokenStream(on_speech, may_say=lambda said: self._acted or not claims_action(said),
                                      clean=lambda t: NUMBER_TAG.sub("", t))
            try:
                reply = self._run_turn(turn, spoken)
            except BrainError as e:
                # Fallo esperado (p. ej. Ollama apagado): se explica, sin traza de error.
                log.info("El cerebro no está disponible: %s", e)
                return self._fail(str(e), f"No puedo pensar ahora mismo: {e}")
            except Exception as e:
                log.exception("Fallo procesando la petición (cerebro: %r)", self._brain.name)
                return self._fail(str(e), ERROR_REPLY)

            self._turns.append(turn)
            if spoken is not None:
                spoken.finish(reply)  # lo que falte por decir (o la respuesta entera si no venía a trozos)
            # En modo texto, "hablar" es mostrar la respuesta; con TTS será voz real.
            self._states.transition(State.SPEAKING)
            self._bus.publish(Event("assistant_message", {"text": reply}))
            if speak:
                self._speak(reply)
            self._states.transition(State.IDLE)
            return reply

    def _speak(self, text: str) -> None:
        if self.speaker is None:
            return
        try:
            self.speaker.speak(NUMBER_TAG.sub("", text))
        except KeyboardInterrupt:
            self.speaker.stop()  # Ctrl+C corta la voz; la respuesta ya está en pantalla
        except Exception:
            # Si la voz falla, la respuesta escrita sigue valiendo: no es un error de la petición.
            log.exception("No he podido leer la respuesta en voz alta")

    def _fail(self, message: str, reply: str) -> str:
        self._states.transition(State.ERROR)
        self._bus.publish(Event("error", {"message": message}))
        self._states.transition(State.IDLE)
        return reply

    def _run_turn(self, turn: list[Message], spoken: SpokenStream | None = None) -> str:
        acted = False   # en esta petición salió bien alguna herramienta que HACE algo (no solo mirar)
        nudge = None    # dónde está el aviso de «has dicho que lo has hecho y no» (se quita al acabar)
        stream = getattr(self._brain, "reply_stream", None) if spoken is not None else None
        for _ in range(MAX_TOOL_ROUNDS):
            if stream is not None:
                spoken.new_pass()
                reply = stream(self._conversation(turn), spoken.feed)
            else:
                reply = self._brain.reply(self._conversation(turn))
            if not reply.tool_calls:
                text = reply.text
                if not acted and claims_action(text):
                    if nudge is None:
                        # Dice que lo ha hecho sin haber hecho nada: se le avisa UNA vez para que use la
                        # herramienta (o lo diga claro). Pasó con «abre Code::Blocks» (core/honesty.py).
                        log.info("El cerebro dice que ha hecho algo sin haberlo hecho: se le avisa")
                        nudge = len(turn)
                        turn += [Message("assistant", text), Message("user", NUDGE)]
                        continue
                    log.info("El cerebro insiste sin hacer nada: se responde con sinceridad")
                    text = HONEST_REPLY
                if nudge is not None:
                    del turn[nudge:nudge + 2]  # el aviso no se queda en el historial
                turn.append(Message("assistant", text))
                return text

            turn.append(Message("assistant", reply.text, tool_calls=reply.tool_calls))
            self._states.transition(State.EXECUTING)
            results = []
            for call in reply.tool_calls:
                result = self._router.execute(call)
                results.append(result)
                turn.append(Message("tool", result.as_text(), tool_call_id=call.id))
                tool = self._router.registry.get(result.tool)
                if result.status == "ok" and tool is not None and tool.risk not in (Risk.READ, Risk.PRIVATE):
                    acted = self._acted = True
            tool = self._router.registry.get(results[0].tool) if len(results) == 1 else None
            if tool and tool.direct_reply and results[0].status == "ok":
                if nudge is not None:
                    del turn[nudge:nudge + 2]
                turn.append(Message("assistant", results[0].output))
                return results[0].output
            self._states.transition(State.THINKING)

        if nudge is not None:
            del turn[nudge:nudge + 2]
        turn.append(Message("assistant", TOO_MANY_ROUNDS_REPLY))
        return TOO_MANY_ROUNDS_REPLY

    def history(self) -> list[dict]:
        """La conversación que recuerda (solo en RAM), para que la web la enseñe al abrirse o recargarse:
        lo que dijiste y lo que respondió, sin las vueltas por herramientas ni las fotos (solo si había una)."""
        items = []
        for past in list(self._turns):  # copia: no se bloquea esperando a una petición que piensa
            if not past or past[0].role != "user":
                continue
            first = past[0]
            had_photo = bool(first.images) or first.content.endswith(PHOTO_NOTE)
            items.append({"role": "user", "text": first.content.removesuffix(PHOTO_NOTE), "photo": had_photo})
            reply = next((m.content for m in reversed(past) if m.role == "assistant" and not m.tool_calls), None)
            if reply:
                items.append({"role": "assistant", "text": reply})
        return items

    def _forget_old_images(self) -> None:
        """Solo se recuerda la última foto. Cada foto son ~1.100 piezas que el modelo vuelve a leer en cada
        mensaje (en este servidor, más de un minuto): con una basta para seguir preguntando por ella, y la
        conversación no se sale de la memoria de trabajo del modelo. Lo que dijo de las anteriores se queda."""
        for past in self._turns:
            for i, m in enumerate(past):
                if m.images:
                    past[i] = replace(m, images=(), content=f"{m.content}{PHOTO_NOTE}")

    def _conversation(self, turn: list[Message]) -> tuple[Message, ...]:
        return (*(m for past in self._turns for m in past), *turn)
