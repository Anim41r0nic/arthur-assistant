# SPDX-License-Identifier: GPL-3.0-or-later
"""Proceso auxiliar de voz: carga Piper UNA vez y sintetiza las frases que le piden.

Se ejecuta con el Python del entorno virtual (.venv/bin/python), que es donde está
instalado piper-tts. Es el único fichero de ARTHUR que usa Piper; como Piper es
GPL-3.0, este fichero también lo es. El resto de ARTHUR habla con él por tuberías,
como con cualquier otro programa.

Protocolo:
  - Entrada (stdin): una línea JSON por petición: {"text": "...", "length_scale": 1.0}
  - Salida (stdout): mensajes binarios = 1 byte de tipo + 4 bytes de longitud + datos
      R  listo; datos = JSON {"sample_rate": 22050}
      A  bloque de audio (PCM de 16 bits, mono)
      E  fin de la frase pedida
      X  error; datos = mensaje en UTF-8
"""

import json
import struct
import sys


def send(kind: bytes, payload: bytes = b"") -> None:
    sys.stdout.buffer.write(kind + struct.pack(">I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def main() -> int:
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(sys.argv[1])
    send(b"R", json.dumps({"sample_rate": voice.config.sample_rate}).encode())
    for line in sys.stdin:
        try:
            request = json.loads(line)
            config = SynthesisConfig(length_scale=float(request.get("length_scale", 1.0)))
            for chunk in voice.synthesize(str(request["text"]), config):
                send(b"A", chunk.audio_int16_bytes)
            send(b"E")
        except Exception as e:  # una frase rota no debe matar al auxiliar
            send(b"X", str(e).encode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
