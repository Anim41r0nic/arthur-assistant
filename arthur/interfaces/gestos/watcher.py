"""GestureWatcher: enciende y apaga la cámara y convierte lo que ve en gestos (docs/gestos.md, paso 4).

Reglas de privacidad (decisiones de diseño):
  - Cada sesión empieza con la cámara APAGADA. Crear el vigilante no enciende nada: solo `turn_on()`, que se llama
    cuando el usuario pulsa el botón del Orb o confirma «activa los gestos». `[gestures] enabled = false` lo impide del todo.
  - Se apaga sola tras `idle_minutes` sin ningún gesto, al bloquear la pantalla y al parar el servicio.
  - Cada cambio publica `camera_state` {on, reason} (el Orb pinta el punto rojo, sale una notificación y queda en
    el registro y la auditoría). Cada gesto válido publica `gesture` {name}, que NO va al Orb.

Anti-rebote: un gesto solo cuenta si se MANTIENE `hold_seconds` con seguridad ≥ `min_score`, visto claro al menos
`MIN_HITS` veces y sin huecos de más de `MAX_GAP` s entre lecturas claras; mantenerlo más no lo repite, y tras un
gesto hay `COOLDOWN` s en los que no cuenta ninguno. Así un gesto de paso (alguien que saluda, una mano que cruza)
no hace nada.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ...core.config import GesturesConfig
from ...core.events import Event, EventBus
from .names import Gesture
from .protocol import GestureLine, Message, StatusLine
from .worker import GestureWorker, WorkerUnavailable

log = logging.getLogger(__name__)

MAX_GAP = 1.0        # s: si entre dos lecturas claras del mismo gesto pasa más, se soltó y hay que empezar de nuevo
# Los gestos que dicen «Sí» se mantienen más (docs/gestos.md): un pulgar arriba que no era para ARTHUR suele
# ser breve. Se suma a `hold_seconds`.
EXTRA_HOLD = {Gesture.THUMB_UP.value: 0.5, Gesture.OK.value: 0.5}
# Los gestos de MOVIMIENTO llegan UNA vez, ya terminados (el proceso de la cámara los manda al completarse el saludo
# o el barrido): cuentan al momento, sin «mantener». La pausa entre gestos (COOLDOWN) les vale igual.
INSTANT = frozenset({Gesture.WAVE.value, Gesture.SWIPE.value})
MIN_HITS = 3         # lecturas claras mínimas dentro de `hold_seconds`: dos sueltas no bastan
# Medido con la burbuja real (0.35.4): un 👎 mantenido sale claro (0,94-0,99) a ráfagas de 1-3 fotogramas
# con huecos de 0,5-0,8 s entre ellas (el detector pierde la mano un momento). Con MAX_GAP = 0,5 la cuenta se
# reiniciaba siempre y ningún gesto llegaba a 1 s; MIN_HITS compensa la tolerancia mayor.
COOLDOWN = 3.0       # s de pausa entre dos gestos
TICK_SECONDS = 2.0   # cada cuánto se mira la inactividad y el bloqueo mientras la cámara está encendida

# Por qué se enciende o se apaga: lista cerrada (va al Orb, a la notificación, al registro y a la auditoría).
REASONS = {
    "orb": "desde el Orb",
    "voz": "a petición tuya",
    "atajo": "con el atajo de teclado",
    "inactividad": "un rato sin gestos",
    "bloqueo": "pantalla bloqueada",
    "servicio": "ARTHUR se ha parado",
    "sin-camara": "no encuentro la cámara",
    "sin-detector": "aún no hay detector de manos (pasos 0 y 1 de docs/gestos.md)",
    "fallo": "el proceso de la cámara ha fallado",
}
ON_REASONS = ("orb", "voz", "atajo")
GESTURE_HELP = "✋ calla, 👍/👌 sí, 👎/✊ no, ☝️ repite, ✌️ pausa/play, 👋 hola (despacio)"
STATUS_REASONS = {"no-camera": "sin-camara", "no-detector": "sin-detector", "error": "fallo"}

WorkerFactory = Callable[[Callable[[Message], None], Callable[[int | None], None]], GestureWorker]


class GestureWatcher:
    def __init__(
        self,
        bus: EventBus,
        config: GesturesConfig,
        *,
        worker_factory: WorkerFactory | None = None,
        is_locked: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        """`worker_factory`, `is_locked` y `clock` solo se cambian en los tests (proceso y reloj FALSOS)."""
        self.bus = bus
        self.config = config
        self.worker_factory = worker_factory or (
            lambda on_message, on_exit: GestureWorker(on_message, on_exit=on_exit, fps=config.fps))
        self.is_locked = is_locked or screen_locked
        self.clock = clock
        self.tick_seconds = tick_seconds  # 0 = sin hilo propio (los tests llaman a tick() a mano)
        self._lock = threading.RLock()
        self._worker: GestureWorker | None = None
        self._ticker_stop: threading.Event | None = None
        self._last_activity = 0.0
        self._reset_hold()
        self._quiet_until = float("-inf")
        self._stats = SessionStats()

    @property
    def active(self) -> bool:
        return self._worker is not None

    # --- encender y apagar -------------------------------------------------------------------------------------

    def turn_on(self, by: str) -> str:
        if by not in ON_REASONS:
            raise ValueError(f"Motivo de encendido desconocido: {by!r}")
        if not self.config.enabled:
            return "Los gestos están desactivados: para poder usarlos, pon `enabled = true` en [gestures] de config.toml."
        # Todo el cambio (y su aviso) bajo el candado: si el proceso dice «no hay cámara» nada más arrancar, ese
        # «apagada» espera a que salga el «encendida» y el Orb nunca se queda con el punto rojo de un proceso muerto.
        with self._lock:
            if self._worker is not None:
                return "La cámara ya está encendida."
            worker: GestureWorker | None = None

            # Lo que avise un proceso solo vale mientras sea EL proceso actual: uno viejo que aún suelta líneas
            # al pararse no puede apagar (ni mover) al nuevo.
            def on_message(message: Message) -> None:
                self._on_message(worker, message)

            def on_exit(code: int | None) -> None:
                self._turn_off_if(worker, "fallo")

            worker = self.worker_factory(on_message, on_exit)
            self._worker = worker
            try:
                worker.start()
            except (WorkerUnavailable, OSError) as e:
                self._worker = None
                log.warning("gestures: camera not started (%s)", type(e).__name__)
                return f"No he podido encender la cámara: {e}"
            self._last_activity = self.clock()
            self._reset_hold()
            self._quiet_until = float("-inf")
            self._stats = SessionStats()
            if self.tick_seconds > 0:
                self._ticker_stop = threading.Event()
                threading.Thread(target=self._ticker, args=(self._ticker_stop,), name="gestures-tick",
                                 daemon=True).start()
            log.info("gestures: camera on (%s)", by)
            self.bus.publish(Event("camera_state", {"on": True, "reason": by}))
        return (f"Cámara encendida para los gestos ({GESTURE_HELP}). "
                f"Se apaga sola tras {self.config.idle_minutes} min sin gestos.")

    def turn_off(self, reason: str) -> str:
        return self._turn_off_if(None, reason)

    def _turn_off_if(self, only: GestureWorker | None, reason: str) -> str:
        if reason not in REASONS:
            raise ValueError(f"Motivo de apagado desconocido: {reason!r}")
        with self._lock:
            worker = self._worker
            if worker is None or (only is not None and worker is not only):
                return "La cámara ya estaba apagada."
            self._worker = None
            if self._ticker_stop:
                self._ticker_stop.set()
                self._ticker_stop = None
            worker.stop()
            log.info("gestures: camera off (%s) · %s", reason, self._stats.summary(self.config.min_score))
            self.bus.publish(Event("camera_state", {"on": False, "reason": reason}))
        return f"Cámara apagada ({REASONS[reason]})."

    # --- lo que llega del proceso de la cámara -----------------------------------------------------------------

    def _on_message(self, worker: GestureWorker | None, message: Message) -> None:
        if isinstance(message, StatusLine):
            if message.status in STATUS_REASONS:
                self._turn_off_if(worker, STATUS_REASONS[message.status])
            return
        if isinstance(message, GestureLine):
            self._on_reading(worker, message)

    def _on_reading(self, worker: GestureWorker | None, line: GestureLine) -> None:
        now = self.clock()
        with self._lock:
            if self._worker is None or self._worker is not worker:
                return
            self._stats.add(line, self.config.min_score)
            name = line.gesture if line.gesture is not None and line.score >= self.config.min_score else None
            if now < self._quiet_until and not (self._fired and name == self._candidate):
                name = None  # pausa tras un gesto: no cuenta nada, ni empieza a contar (salvo seguir con el mismo)
            if name is None:
                # Un fotograma flojo o sin gesto NO suelta el gesto: solo es un hueco. Con la mano real, entre
                # fotogramas de 0,9-1,0 salen sueltos de 0,5-0,7 (medido en una prueba real, 0.35.3) y reiniciaban la cuenta,
                # así que la palma casi nunca llegaba a 1 s. Soltarlo es pasar más de MAX_GAP sin verlo claro.
                return
            if name != self._candidate or now - self._last_seen > MAX_GAP:
                self._candidate, self._since, self._fired, self._hits = name, now, False, 0
            self._last_seen = now
            self._hits += 1
            hold = 0.0 if name in INSTANT else self.config.hold_seconds + EXTRA_HOLD.get(name, 0.0)
            hits = 1 if name in INSTANT else MIN_HITS
            if self._fired or now - self._since < hold or self._hits < hits:
                return
            self._fired = True  # mantenerlo más no lo repite: hay que soltarlo y volver a hacerlo
            self._quiet_until = now + COOLDOWN
            self._last_activity = now
            log.info("gestures: %s", name)
        # Fuera del candado: quien actúa con el gesto (GestureActions) puede tardar un poco y no debe frenar la
        # lectura de la cámara ni el apagado.
        self.bus.publish(Event("gesture", {"name": name}))

    def _reset_hold(self) -> None:
        self._candidate: str | None = None
        self._since = 0.0
        self._last_seen = float("-inf")
        self._fired = False
        self._hits = 0

    # --- apagado automático ------------------------------------------------------------------------------------

    def tick(self) -> None:
        """Apaga la cámara si la pantalla está bloqueada o lleva `idle_minutes` sin gestos."""
        if not self.active:
            return
        try:
            locked = self.is_locked()
        except Exception:
            log.exception("gestures: no he podido saber si la pantalla está bloqueada")
            locked = False
        if locked:
            self.turn_off("bloqueo")
        elif self.clock() - self._last_activity >= self.config.idle_minutes * 60:
            self.turn_off("inactividad")

    def _ticker(self, stop: threading.Event) -> None:
        while not stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception:
                log.exception("gestures: fallo en el apagado automático")


class SessionStats:
    """Recuento de lo que mandó la cámara en una sesión, para el registro al apagarla (solo nombres y números).

    Sirve para saber, sin scripts aparte, si un gesto no saltó porque no se vio, porque se vio flojo o porque se
    vio claro pero a ratos (lo que pasó con el 👎 en la 0.35.3).
    """

    def __init__(self) -> None:
        self.readings = 0
        self.clear: dict[str, int] = {}
        self.best: dict[str, float] = {}

    def add(self, line: GestureLine, min_score: float) -> None:
        self.readings += 1
        if line.gesture is None:
            return
        self.best[line.gesture] = max(self.best.get(line.gesture, 0.0), line.score)
        if line.score >= min_score:
            self.clear[line.gesture] = self.clear.get(line.gesture, 0) + 1

    def summary(self, min_score: float) -> str:
        seen = ", ".join(f"{name} {self.clear.get(name, 0)} ≥{min_score:g} (máx {best:.2f})"
                         for name, best in sorted(self.best.items()))
        return f"{self.readings} lecturas; {seen or 'ningún gesto visto'}"


LOCKERS = ("hyprlock", "swaylock", "gtklock")  # programas de pantalla de bloqueo que se reconocen por su nombre


def screen_locked(proc: Path = Path("/proc"), run: Callable[[list[str]], str] | None = None) -> bool:
    """¿Está la pantalla bloqueada? Solo lectura: mira si corre un programa de bloqueo y lo que dice logind.

    Dos pistas, porque según cómo se bloquee solo aparece una: `hyprlock` (o parecido) en marcha, o
    `LockedHint=yes` en logind (lo pone `loginctl lock-session` y algunas pantallas de bloqueo).
    """
    for comm in proc.glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() in LOCKERS:
                return True
        except OSError:
            continue  # el proceso terminó mientras mirábamos
    if run is None:
        from ...tools.base import ToolError
        from ...tools.programs import Programs

        def run(argv: list[str]) -> str:
            try:
                return Programs().run(argv, timeout=2)
            except ToolError:
                return ""  # sin loginctl o sin sesión: no se sabe, y no se inventa
    return run(["loginctl", "show-session", "auto", "-p", "LockedHint", "--value"]).strip() == "yes"


class GestureSwitch:
    """Interruptor de los gestos que usan la herramienta y el Orb.

    Se crea antes que el vigilante (que solo existe en modo servicio) y se le conecta después, como WakeSwitch.
    """

    def __init__(self) -> None:
        self.watcher: GestureWatcher | None = None

    def set(self, enabled: bool, *, by: str = "voz") -> str:
        if self.watcher is None:
            return "Los gestos solo funcionan con ARTHUR como servicio."
        return self.watcher.turn_on(by) if enabled else self.watcher.turn_off(by)

    def toggle(self, *, by: str = "atajo") -> str:
        """Enciende la cámara si está apagada y la apaga si está encendida (el atajo Super+Shift+G)."""
        if self.watcher is None:
            return "Los gestos solo funcionan con ARTHUR como servicio."
        return self.set(not self.watcher.active, by=by)
