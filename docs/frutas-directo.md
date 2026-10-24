# Montaje directo de Frutinovelas (pieza de biblioteca)

`arthur.studio.frutas_directo` monta un Short vertical desde **8 a 12 fotos y voces WAV ya preparadas**.
No consulta Gemini, no crea personajes ni voces, no publica y **no está conectado al piloto automático**.
El resultado se lleva a la página de revisión con la orden de importar que ya existe (0.63.3):

```sh
python -m arthur.studio.frutas_directo ~/ruta/episodio/guion.json ~/ruta/episodio/salida \
    --catalogo docs/frutas-catalogo-ejemplo.json
arthur --studio-importar-short frutinovelas ~/ruta/episodio/salida/video.mp4 --titulo "…" \
    --srt ~/ruta/episodio/salida/subtitulos.srt
```

Frutinovelas tiene `publishing=False`: aprobar en la página no sube nada hasta que se active en `arthur/studio/channels.py`.

## Dos formas de guion

**Con catálogo (la buena).** Cada escena da `plano`, `personaje`, `gesto`, `objetos`, `hechos`,
`estado_antes` y `cambios` (el contrato de `frutas-validation.md`), más `voz`, `texto` y `duracion`. Antes de
llamar a ffmpeg el guion pasa por `validar_guion`; si algo no cuadra (plano inexistente, objeto que no se ve,
continuidad rota, personaje fuera del reparto), no se monta nada y el error lista los motivos. La foto sale
del `archivo` del plano dentro de `raiz_recursos`: una escena no puede poner otra `imagen` a mano.

**Sin catálogo (pruebas).** Cada escena da `imagen`, `voz`, `texto` y `duracion`; no hay validación editorial.

```json
{"escenas": [
  {"numero": 1, "personaje": "fina", "plano": "F_SONRISA", "gesto": "sonrisa", "objetos": [], "hechos": [],
   "estado_antes": {}, "cambios": [], "voz": "voz/01.wav", "texto": "Quiero cocinar contigo, Mora.", "duracion": 4}
]}
```

## Reglas que se comprueban antes de montar

- 8–12 escenas de 2 a 6 s; 60 s como mucho en total (lo que acepta la importación).
- Foto PNG, JPG o WebP **de verdad** (se miran sus primeros bytes, no solo la extensión). No se admite vídeo.
- Voz WAV PCM con sonido, de 3,35 s como mucho, con 0,35 s antes y 0,20 s después dentro de la escena.
- Texto de una línea, 1–70 caracteres, que quepa en tres líneas de 25 en pantalla.
- Rutas: las voces (y las fotos sin catálogo) deben estar dentro de la carpeta del guion; las fotos del
  catálogo, dentro de `raiz_recursos`. Se resuelven los enlaces simbólicos antes de comprobarlo.

## Qué sale

`video.mp4` (H.264/AAC, 1080 × 1920, 24 fps, voz a -14 LUFS en dos pasadas como el resto de Studio),
`subtitulos.srt` y `control.json` (tamaño, códecs, duración, volumen, escenas usadas y si se validó con
catálogo). El vídeo se comprueba con la misma función que usa la importación (`importer.check_video`) y la
duración debe coincidir con la suma de escenas. Si ya existe `video.mp4`, no se sobrescribe.

Cada foto recibe un acercamiento lento de cámara hacia el centro (6 %): el personaje no mueve el cuerpo.

## Lo que NO hace

No genera voces ni imágenes, no escribe guiones, no mira los píxeles (identidad, manos, objetos) ni escucha
las voces: eso sigue siendo la revisión humana o una pieza futura.
