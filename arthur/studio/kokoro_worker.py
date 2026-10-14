"""Proceso aparte que tiene cargada la voz Kokoro (paso 3, voz de Creator Studio).

Se ejecuta con el Python del entorno de Kokoro (<datos>/studio/kokoro/venv, requirements-kokoro.txt), NO con el
del core: así sus 33 dependencias no se mezclan con ARTHUR. No importa nada de `arthur`.

Protocolo (una línea JSON por petición y por respuesta, por la entrada y salida estándar):
  → {"text": "…", "out": "/ruta/trozo.wav", "voice": "ef_dora", "lang": "es", "speed": 0.95}
  ← {"ok": true, "seconds": 3.1}        o        {"ok": false, "error": "…"}

El modelo se carga UNA vez (tarda unos segundos: pesa 326 MB) y luego cada frase sale en ~1/3 de lo que dura.
El audio se guarda en WAV de 16 bits para que el core lo pueda unir con el módulo `wave` de la biblioteca estándar.
"""

import json
import sys
from pathlib import Path


def main() -> int:
    base = Path(sys.argv[1])
    import soundfile
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(base / "kokoro-v1.0.onnx"), str(base / "voices-v1.0.bin"))
    print(json.dumps({"ok": True, "ready": True}), flush=True)
    for line in sys.stdin:
        try:
            job = json.loads(line)
            text, out = str(job["text"]), Path(job["out"])
            samples, rate = kokoro.create(text, voice=str(job["voice"]), speed=float(job.get("speed", 1.0)),
                                          lang=str(job.get("lang", "es")))
            soundfile.write(str(out), samples, rate, subtype="PCM_16", format="WAV")
            print(json.dumps({"ok": True, "seconds": round(len(samples) / rate, 3)}), flush=True)
        except Exception as error:  # el core decide qué hacer; este proceso sigue vivo para la siguiente frase
            print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"[:500]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
