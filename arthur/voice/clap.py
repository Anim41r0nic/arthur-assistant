"""Doble palmada: otra forma de despertar a ARTHUR, además de «Hey Arthur».

Usa el MISMO micrófono y los mismos trozos de 100 ms que la palabra de activación (no abre nada nuevo), solo
biblioteca estándar y nada de audio guardado. Una palmada es un golpe CORTO y FUERTE: dentro de un trozo de 100 ms
su pico es muchísimo mayor que el volumen medio (factor de cresta alto), cosa que no pasa con la voz ni con la
música. Se activa con DOS golpes separados por entre 0,2 y 0,8 s; si siguen más golpes (un aplauso, un golpeteo en
la mesa) se descarta, y por eso se espera medio segundo tras el segundo antes de dar la señal.

⚠ Los umbrales son estimaciones sin calibrar con TU micrófono (que además está al 45 %): cada golpe que parece una
palmada se apunta en el registro con su pico y su cresta (solo cifras) para poder ajustarlos.
"""

import array
import logging
import math

log = logging.getLogger(__name__)

MIN_CREST = 6.0     # pico / volumen medio del trozo: voz ≈ 3-5, una palmada ≳ 8
MIN_GAP = 2         # trozos (100 ms) mínimos entre palmadas: el eco de la primera no cuenta como segunda
MAX_GAP = 8         # y máximos: más de 0,8 s ya son dos golpes sueltos
QUIET_AFTER = 5     # trozos sin más golpes tras la segunda palmada (0,5 s) para dar la señal
LOG_PEAK, LOG_CREST = 0.15, 4.0  # lo que se apunta en el registro para calibrar (no activa nada)


def chunk_stats(pcm: bytes) -> tuple[float, float]:
    """(pico, cresta) de un trozo s16: pico de 0 a 1 de la escala completa; cresta = pico / RMS."""
    samples = array.array("h", pcm)
    if not samples:
        return 0.0, 0.0
    peak = max(max(samples), -min(samples)) / 32768
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return peak, (peak / rms if rms > 0 else 0.0)


class ClapDetector:
    def __init__(self, min_peak: float = 0.3) -> None:
        self.min_peak = min_peak
        self.reset()

    def reset(self) -> None:
        self._index = 0
        self._claps: list[int] = []  # en qué trozo cayó cada palmada de la serie en curso
        self._blocked_until = 0      # tras un aplauso, los golpes siguientes se ignoran hasta que haya calma

    def feed(self, chunk: bytes) -> bool:
        """Un trozo de audio (100 ms). True cuando acaba de sonar una doble palmada limpia."""
        peak, crest = chunk_stats(chunk)
        index, self._index = self._index, self._index + 1
        clap = peak >= self.min_peak and crest >= MIN_CREST
        if peak >= LOG_PEAK and crest >= LOG_CREST:
            log.info("Golpe: pico %.2f, cresta %.1f%s", peak, crest, " (cuenta como palmada)" if clap else "")
        if clap:
            if self._claps and index - self._claps[-1] < MIN_GAP:
                return False  # el mismo golpe repartido en dos trozos (o su eco)
            if index < self._blocked_until:
                self._blocked_until = index + MAX_GAP + 1  # el aplauso sigue: la calma se cuenta desde este golpe
                return False
            if self._claps and index - self._claps[-1] > MAX_GAP:
                self._claps = []  # la anterior quedó sola: esta empieza una serie nueva
            self._claps.append(index)
            if len(self._claps) > 2:
                self._claps = []  # tres o más golpes seguidos = aplauso o golpeteo: ni dos ni uno
                self._blocked_until = index + MAX_GAP + 1
            return False
        if len(self._claps) == 2 and index - self._claps[-1] >= QUIET_AFTER:
            self._claps = []
            return True
        if len(self._claps) == 1 and index - self._claps[-1] > MAX_GAP:
            self._claps = []
        return False
