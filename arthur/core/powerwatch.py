"""Vigila el perfil de energía del portátil (solo en modo servicio).

- Al ENCHUFARLO (o si ARTHUR arranca ya enchufado) pone el perfil `performance`; al DESENCHUFARLO vuelve al que
  tenías antes (solo si lo cambió ARTHUR). Se actúa una vez por cambio de enchufe, no cada minuto: si después
  eliges tú otro perfil, ARTHUR no se lo discute.
- Con `power-saver` el procesador y la GPU van frenados y el modelo genera casi la mitad de rápido (medido:
  ~13 → 7-9 tokens/s), así que se avisa UNA vez cada vez que se entra en ahorro.
Solo órdenes fijas a powerprofilesctl con perfiles de una lista cerrada; nada de texto del modelo.
"""

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

SAVER, BALANCED, PERFORMANCE = "power-saver", "balanced", "performance"
PROFILES = (SAVER, BALANCED, PERFORMANCE)
SAVER_MESSAGE = ("Estás en modo ahorro de energía: voy más despacio. "
                 "Con «powerprofilesctl set balanced» (o performance) voy más rápido.")


class PowerWatcher:
    def __init__(self, read_profile: Callable[[], str | None], notify: Callable[[str], None], *,
                 read_ac: Callable[[], bool | None] = lambda: None,
                 set_profile: Callable[[str], None] | None = None, interval: float = 60.0) -> None:
        self.read_profile = read_profile  # None = no se pudo saber (sin powerprofilesctl): no se hace nada
        self.notify = notify
        self.read_ac = read_ac            # True enchufado, False con batería, None = no se sabe
        self.set_profile = set_profile    # None = solo avisar, no cambiar nada
        self.interval = interval
        self._was_saver = False
        self._was_ac: bool | None = None
        self._previous: str | None = None  # el perfil que había antes de que ARTHUR pusiera performance
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="powerwatch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> None:
        profile = self.read_profile()
        if profile is None:
            return
        ac = self.read_ac()
        if self.set_profile and ac is not None:
            profile = self._follow_plug(profile, ac)
        saver = profile == SAVER
        if saver and not self._was_saver:
            log.info("Perfil de energía: ahorro (avisado)")
            self.notify(SAVER_MESSAGE)
        self._was_saver = saver

    def _follow_plug(self, profile: str, ac: bool) -> str:
        changed, self._was_ac = ac != self._was_ac, ac
        if not changed:
            return profile
        if ac and profile != PERFORMANCE and profile in PROFILES:
            if self._apply(PERFORMANCE):
                self._previous = profile
                self.notify("Enchufado: he puesto el modo rendimiento. Al desconectar vuelvo a «%s»." % profile)
                return PERFORMANCE
        elif not ac and self._previous in PROFILES:
            previous, self._previous = self._previous, None
            if self._apply(previous):
                self._was_saver = previous == SAVER  # volver al ahorro que tenías no es una novedad que avisar
                return previous
        return profile

    def _apply(self, profile: str) -> bool:
        try:
            self.set_profile(profile)
        except Exception as e:
            log.warning("No he podido poner el perfil de energía %s: %s", profile, e)
            return False
        log.info("Perfil de energía: %s", profile)
        return True

    def _loop(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                log.exception("Fallo mirando el perfil de energía")
            if self._stop.wait(self.interval):
                return
