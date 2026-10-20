"""Micrófono y altavoz en Windows, SOLO con la biblioteca estándar (docs/windows.md, aparato fase A).

En Linux el micro es pw-record y el altavoz pw-play. En Windows no existen, y no queremos instalar nada (ni
PortAudio ni sounddevice): Windows ya trae lo necesario.

- Micrófono: winmm.dll (waveIn), la API de audio clásica de Windows, llamada con ctypes. Pedimos lo mismo que a
  pw-record: PCM de 16 bits, 16 kHz, mono (lo que necesita whisper). Si el micro del PC no da 16 kHz, el
  «WAVE_MAPPER» de Windows convierte solo.
- Altavoz: winsound (viene con Python en Windows), reproduciendo el WAV desde MEMORIA: la voz de ARTHUR nunca se
  escribe en disco.

Las dos piezas reciben su «parte de Windows» desde fuera (`winmm`, `backend`) para que los tests usen falsos: en la
nube no hay Windows, y los tests nunca tocan el micro ni el altavoz de verdad.
"""

from __future__ import annotations

import array
import ctypes
import io
import logging
import sys
import threading
import time
import wave
from collections.abc import Callable

from .tts import SLICE_SECONDS, rms_level

log = logging.getLogger(__name__)

# --- tipos de Windows ------------------------------------------------------------------------------------------
# Con tamaños FIJOS (no ctypes.wintypes ni c_ulong): en Windows un DWORD son 4 bytes, y c_ulong en Linux son 8.
# Si las estructuras no miden lo mismo que en Windows, winmm escribiría fuera de su sitio.
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
UINT = ctypes.c_uint32
DWORD_PTR = ctypes.c_size_t

WAVE_MAPPER = 0xFFFFFFFF   # «el micro predeterminado de Windows»
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0          # sin avisos de winmm: miramos nosotros cada poco si un búfer está lleno (más simple)
WHDR_DONE = 0x00000001     # winmm ha llenado este búfer
MMSYSERR_NOERROR = 0

BUFFER_SECONDS = 0.1       # cada búfer, 100 ms de audio
BUFFERS = 8                # 800 ms en cola: aunque Python tarde un poco en leer, no se pierde audio
POLL_SECONDS = 0.01


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1  # en mmreg.h va empaquetada (18 bytes)
    _fields_ = [
        ("wFormatTag", WORD), ("nChannels", WORD), ("nSamplesPerSec", DWORD), ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD), ("wBitsPerSample", WORD), ("cbSize", WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p), ("dwBufferLength", DWORD), ("dwBytesRecorded", DWORD),
        ("dwUser", DWORD_PTR), ("dwFlags", DWORD), ("dwLoops", DWORD), ("lpNext", ctypes.c_void_p),
        ("reserved", DWORD_PTR),
    ]


WAVEIN_FUNCTIONS = ("waveInOpen", "waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer",
                    "waveInStart", "waveInStop", "waveInReset", "waveInClose")


def load_winmm():
    """winmm.dll con los tipos de cada función declarados (sin eso, ctypes podría pasar mal los punteros)."""
    winmm = ctypes.WinDLL("winmm")  # solo existe en Windows
    handle_p = ctypes.POINTER(ctypes.c_void_p)
    hdr_p = ctypes.POINTER(WAVEHDR)
    winmm.waveInOpen.argtypes = [handle_p, UINT, ctypes.POINTER(WAVEFORMATEX), DWORD_PTR, DWORD_PTR, DWORD]
    for name in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
        getattr(winmm, name).argtypes = [ctypes.c_void_p, hdr_p, UINT]
    for name in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
        getattr(winmm, name).argtypes = [ctypes.c_void_p]
    for name in WAVEIN_FUNCTIONS:
        getattr(winmm, name).restype = UINT
    return winmm


class WaveInSource:
    """El micrófono por waveIn. Cumple lo que espera Recorder: read(n) bloquea hasta tener n bytes (o b"" si se
    ha cerrado) y close() lo cierra. El micro solo está abierto entre la creación y close()."""

    def __init__(self, *, rate: int = 16_000, winmm=None, poll: float = POLL_SECONDS) -> None:
        """`winmm` solo se cambia en los tests (un winmm falso: nada abre el micro de verdad)."""
        self._winmm = winmm or load_winmm()
        self._poll = poll
        self._lock = threading.Lock()  # read() (hilo del grabador) y close() (otro hilo) no tocan los búferes a la vez
        self._closed = False
        self._opened = False
        self._pending = bytearray()
        fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, 1, rate, rate * 2, 2, 16, 0)
        self._handle = ctypes.c_void_p()
        self._check(self._winmm.waveInOpen(ctypes.byref(self._handle), WAVE_MAPPER, ctypes.byref(fmt), 0, 0,
                                           CALLBACK_NULL), "abrir el micrófono")
        self._opened = True
        size = int(rate * BUFFER_SECONDS) * 2
        self._buffers = [ctypes.create_string_buffer(size) for _ in range(BUFFERS)]
        self._headers = [WAVEHDR(lpData=ctypes.addressof(b), dwBufferLength=size) for b in self._buffers]
        self._prepared: list[WAVEHDR] = []
        self._next = 0  # el búfer que toca leer: winmm los llena EN ORDEN
        try:
            for header in self._headers:
                self._check(self._winmm.waveInPrepareHeader(self._handle, ctypes.byref(header),
                                                             ctypes.sizeof(WAVEHDR)), "preparar el micrófono")
                self._prepared.append(header)
                self._check(self._winmm.waveInAddBuffer(self._handle, ctypes.byref(header),
                                                         ctypes.sizeof(WAVEHDR)), "preparar el micrófono")
            self._check(self._winmm.waveInStart(self._handle), "empezar a grabar")
        except OSError:
            self.close()
            raise

    @staticmethod
    def _check(code: int, what: str) -> None:
        if code != MMSYSERR_NOERROR:
            # Lo típico: no hay micro, o Windows no deja usarlo (Configuración > Privacidad > Micrófono).
            raise OSError(f"Windows no me deja {what} (código {code}; mira si hay micro y si las apps de "
                          "escritorio tienen permiso en Configuración > Privacidad > Micrófono)")

    def _take_ready(self) -> None:
        """Pasa a `_pending` los búferes que winmm ya ha llenado y se los devuelve vacíos para que siga."""
        while True:
            header = self._headers[self._next]
            if not header.dwFlags & WHDR_DONE:
                return
            self._pending += ctypes.string_at(header.lpData, header.dwBytesRecorded)
            header.dwFlags &= ~WHDR_DONE
            header.dwBytesRecorded = 0
            self._winmm.waveInAddBuffer(self._handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR))
            self._next = (self._next + 1) % len(self._headers)

    def read(self, size: int) -> bytes:
        while True:
            with self._lock:
                if self._closed:
                    return b""
                self._take_ready()
                if len(self._pending) >= size:
                    chunk = bytes(self._pending[:size])
                    del self._pending[:size]
                    return chunk
            time.sleep(self._poll)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            if not self._opened:
                return
            # Reset devuelve todos los búferes; después ya se pueden soltar y cerrar el micro.
            self._winmm.waveInReset(self._handle)
            for header in self._prepared:
                self._winmm.waveInUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(WAVEHDR))
            self._winmm.waveInClose(self._handle)


# --- altavoz ---------------------------------------------------------------------------------------------------

class WinsoundBackend:
    """Lo único que toca Windows al reproducir: winsound. play() bloquea hasta que termina (o hasta stop())."""

    def __init__(self) -> None:
        import winsound  # solo existe en Windows

        self._winsound = winsound

    def play(self, wav: bytes) -> None:
        # SND_MEMORY: desde la memoria (Python no deja combinarlo con SND_ASYNC: por eso va en su propio hilo).
        # SND_NODEFAULT: si algo falla, sin el «ding» de error de Windows.
        self._winsound.PlaySound(wav, self._winsound.SND_MEMORY | self._winsound.SND_NODEFAULT)

    def stop(self) -> None:
        self._winsound.PlaySound(None, 0)  # None corta el sonido que esté sonando en este programa


def scale_volume(pcm: bytes, volume: float) -> bytes:
    """El volumen de la voz de ARTHUR (config [voice] volume), como hacía `pw-play --volume`."""
    if volume >= 0.999:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":  # el WAV va en little-endian
        samples.byteswap()
    factor = max(0.0, volume)
    scaled = array.array("h", (max(-32768, min(32767, int(s * factor))) for s in samples))
    if sys.byteorder != "little":
        scaled.byteswap()
    return scaled.tobytes()


def _wav(pcm: bytes, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


class WinSoundPlayer:
    """Como WavPlayer (interfaces/voz_cliente.py), pero en Windows: play() bloquea hasta que suena entero o hasta
    stop(); `playing` dice si suena; `on_level` recibe el volumen de lo que va sonando para que el Orb lata."""

    STOP_GRACE = 1.0  # tras stop(), lo que esperamos a que winsound suelte el altavoz

    def __init__(self, *, volume: float = 1.0, on_level: Callable[[float], None] | None = None,
                 backend=None) -> None:
        """`backend` solo se cambia en los tests (un altavoz falso: nada debe sonar)."""
        self.volume = volume
        self.on_level = on_level or (lambda level: None)
        self._backend = backend
        self._lock = threading.Lock()
        self._current: threading.Event | None = None  # el «corte» de lo que suena ahora

    @property
    def backend(self):
        if self._backend is None:
            self._backend = WinsoundBackend()
        return self._backend

    @property
    def playing(self) -> bool:
        return self._current is not None

    def play(self, wav_bytes: bytes) -> None:
        """Bloquea hasta que termina de sonar (o hasta stop())."""
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError("Audio de ARTHUR en un formato inesperado")
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        pcm = scale_volume(pcm, self.volume)
        data = _wav(pcm, rate)
        with self._lock:
            self.stop()  # una sola voz a la vez
            cut = self._current = threading.Event()
        finished = threading.Event()

        def sound() -> None:
            try:
                self.backend.play(data)
            except Exception as e:  # p. ej. sin altavoz: no tumba el turno de voz
                log.warning("No he podido reproducir la voz: %s", e)
            finally:
                finished.set()

        threading.Thread(target=sound, name="winsound", daemon=True).start()
        # winsound no dice por dónde va: el nivel para el Orb se calcula aquí, al mismo ritmo que suena.
        step = int(rate * SLICE_SECONDS) * 2
        start = time.monotonic()
        for i in range(0, len(pcm), step):
            if cut.is_set() or finished.is_set():
                break
            wait = start + i / 2 / rate - time.monotonic()
            if wait > 0 and cut.wait(wait):
                break
            self.on_level(rms_level(pcm[i:i + step]))
        while not finished.wait(0.05):  # que acabe de sonar, o que el corte lo pare
            if cut.is_set():
                finished.wait(self.STOP_GRACE)
                break
        self.on_level(0.0)
        with self._lock:
            if self._current is cut:
                self._current = None

    def stop(self) -> None:
        cut, self._current = self._current, None
        if cut is not None:
            cut.set()
            try:
                self.backend.stop()
            except Exception as e:
                log.warning("No he podido cortar la voz: %s", e)
