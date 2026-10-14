# Creator Studio (rama `studio`)

> Estado: **plan aprobado por Marc el 2026-10-12**. Se construye por pasos pequeños; cada uno se prueba antes
> del siguiente. Esta rama vive en `~/Proyectos/arthur-studio` (git worktree) para no estorbar al trabajo
> que sigue en `revision-pr3`.

## Qué es

Un módulo de ARTHUR (no otra aplicación) para:

1. **Investigar** oportunidades de YouTube: comparar nichos, canales y vídeos públicos, con datos, fechas y
   fuentes, y puntuarlas.
2. **Producir** un vídeo a partir de una idea elegida: verificar la historia, guion original, voz, subtítulos,
   montaje, miniatura y control de calidad.
3. **Publicar** en YouTube (y más adelante Instagram y TikTok) solo con tu aprobación explícita.

Nada promete que un vídeo vaya a funcionar ni que pase los filtros de monetización de YouTube.

## Decisiones de Marc (2026-10-12)

| Tema | Decisión |
|---|---|
| Dónde se hace el trabajo pesado | En el servidor, en un contenedor `studio` aparte con límite de RAM y un trabajo a la vez |
| Cuenta | Una cuenta de Google **nueva** solo para el canal (también para Instagram y TikTok) |
| Dinero | **Todo gratis.** Ningún servicio de pago hasta que el canal genere ingresos |
| Rama | `studio`, desde `revision-pr3` (0.51.0) |

## Reglas de diseño

- **El modelo nunca publica.** No existe ninguna herramienta de publicar: solo un botón en la web, con una
  pantalla que enseña canal, título, descripción, miniatura y visibilidad. La aprobación va atada a la huella
  (sha256) del vídeo, la miniatura y los textos: si algo cambia después, hay que volver a aprobar.
- **Datos honestos:** cada dato lleva fuente, fecha y si es `observado`, `estimado` o `hipótesis`.
- **Derechos:** un recurso (imagen, música, clip) sin licencia registrada **bloquea** el montaje.
- **Voces:** Piper `davefx` (CC0, ya montada en `arthur-voz`). Descartadas XTTS y F5-TTS: sus licencias no
  permiten uso comercial.
- **Base de datos aparte:** `studio.sqlite3` (no se mezcla con tu memoria). El core sigue siendo solo
  biblioteca estándar; lo pesado lo hacen programas (ffmpeg…) en el contenedor `studio`.

## Pasos

| Paso | Qué | Estado |
|---|---|---|
| 1 | Base de datos `studio.sqlite3` (con versiones de esquema) y cola de trabajos por pasos que sobrevive a cortes | Hecho |
| 2 | Investigador: clave de YouTube Data API, canales/vídeos, «vídeos que destacan», puntuación e informes | Hecho (probado con la clave real) |
| 3 | Producir UN vídeo de principio a fin: guion con fuentes, voz, subtítulos, montaje (contenedor `studio` con ffmpeg), miniatura y control de calidad | Hecho: primer vídeo completo (falta decidir la voz) |
| 4 | Revisión: página `/studio` en la web (desde el móvil) con el vídeo, la miniatura y los textos; botones «Aprobar» / «Cambiar» / «Descartar»; aviso cuando haya uno listo | Hecho (falta el aviso al móvil: al juntar con el core) |
| 5 | Publicar en YouTube al aprobar (OAuth de la cuenta del canal); la aprobación va atada a la huella del vídeo | Hecho y probado: primera subida real el 2026-10-12 (privado, se puede poner público) |
| 6 | Piloto automático: cada día investiga ideas DENTRO del nicho del canal, elige una, produce el vídeo y te lo manda a revisar | Hecho; se publica solo a las 18:00 lo que no tenga dudas (falta la prueba real con Gemini) |
| 7 | Pedírselo a ARTHUR hablando (herramientas `studio_*` en el chat) | Pendiente |
| 8 | Instagram y TikTok (verificar antes sus requisitos: cuentas profesionales y apps revisadas) | Idea |

## Objetivo de Marc (2026-10-12): piloto automático con revisión

```text
cada día ─► investigar ideas del nicho ─► elegir una ─► producir vídeo ─► aviso a Marc ─► Marc lo ve en el móvil
                                                                              │
                                                     «Aprobar» ─► se publica solo │ «Cambiar» ─► se rehace │ «Descartar»
```

- **El nicho lo elige Marc una vez** (un canal necesita un tema fijo para que YouTube sepa a quién enseñárselo);
  lo que ARTHUR elige cada día es la idea del vídeo dentro de ese nicho.
- **Nunca se publica sin el «Aprobar» de Marc** (regla de diseño de arriba). Motivos: el modelo local (4B) se
  equivoca en datos, y YouTube no monetiza el contenido repetitivo o producido en masa sin aporte propio.
- **Antes del paso 5, comprobar en la documentación de Google:** los vídeos subidos por la API desde un proyecto
  de Google Cloud no verificado se quedan en privado hasta pasar una auditoría de Google; mirar los requisitos
  vigentes y el coste en cuota de subir un vídeo.
- El orden cambia (antes el chat iba primero): primero que salga UN vídeo bueno a mano; automatizar algo que aún
  no sale bien solo produciría vídeos malos más deprisa.

## La cola de trabajos (paso 1)

`arthur/studio/jobs.py` + `arthur/studio/store.py`.

```text
submit("video", {...}) ──► jobs: queued ──► hilo «studio-jobs» coge UNO ──► paso 1 ─► paso 2 ─► … ─► done
                                                   │ cada paso guarda su resultado en job_steps
                                    fallo pasajero ┤──► queued otra vez, espera 30 s, 60 s, 120 s… (máx. 15 min)
                                  fallo definitivo ┤──► failed (StepFailed: repetirlo daría lo mismo)
                                          cancelar ┘──► cancelled (en marcha: al acabar el paso actual)
```

- **Cortes:** al arrancar, lo que estaba «running» vuelve a la cola y sigue desde el primer paso sin terminar.
  Probado con un proceso real y `kill -9` a mitad de un paso: lo terminado no se repitió.
- **Costes:** cada paso puede anotar lo que costó; `None` significa «no se sabe» (no es lo mismo que 0 €).
- **Fichero:** `<datos>/studio/studio.sqlite3`, carpeta 700 y fichero 600. Si el fichero es de una versión
  más nueva de ARTHUR, no se toca.

## El investigador (paso 2)

`arthur/studio/youtube.py` (cliente de solo lectura) + `arthur/studio/research.py` (pasos, cuentas e informe).

```text
arthur --studio-investigar "gatos" "coches eléctricos" [--studio-dias 90 --studio-region ES --studio-idioma es]

buscar ─► videos ─► canales ─► referencias ─► informe      (un trabajo «investigar» de la cola del paso 1)
 200/nicho   1/50      1/50     ~1 por canal     0          unidades de cuota (10.000 al día, gratis)
```

- **Dos formatos, dos notas:** por cada nicho se buscan los vídeos cortos (menos de 4 min) y los largos (4 a 20
  min) por separado. Un Short y un vídeo de 15 minutos no compiten en lo mismo ni pagan lo mismo; en la primera
  prueba real, «curiosidades de historia» salía arriba solo por sus Shorts (48 de 50).
- **Idiomas:** `--studio-idioma` es solo una preferencia para YouTube y se cuelan otros idiomas. No se tiran:
  van a «En otros idiomas» (pueden ser ideas para adaptar), pero no cuentan en la nota. Para estudiar otro
  idioma: `--studio-idioma en --studio-region US`.

- **Clave:** `arthur --studio-clave-youtube` la pide sin enseñarla y la guarda en `<datos>/studio/youtube-key`
  (600). Va en la cabecera `X-Goog-Api-Key`, nunca en la dirección. Con una clave solo se leen datos públicos.
- **Cuota:** se apunta en `studio.sqlite3` (tabla `api_usage`) ANTES de cada petición, también si falla. Se para
  al llegar a 9.000 (1.000 de reserva). El día de cuota empieza a las 9:00 de España (medianoche de California).
- **Caché:** cada respuesta se guarda 12 horas (`api_cache`): repetir un informe no gasta.
- **Lo normal de un canal:** mediana de las visitas de sus últimos 10 vídeos con más de 7 días.
  «Destaca» = 3 veces o más esa mediana. Si la mediana es menor de 1.000 visitas no se compara (la prueba real
  dio «×15.165» en un canal que suele tener 40 visitas).
- **Puntuación (hipótesis, 0-100):** demanda (40: mediana de visitas/día, 10.000 o más = máximo) + hueco (30:
  parte de vídeos de canales con menos de 100.000 suscriptores) + sorpresa (30: parte de vídeos que destacan;
  la mitad o más = máximo). Va escrita en el propio informe.
- **Errores:** clave mala, API sin activar o cuota agotada → el trabajo falla sin reintentar. Red o Google
  ocupado → se reintenta más tarde.
- **Informe:** se imprime y se guarda en `<datos>/studio/informes/nichos-<n>.md`.

## Producir un vídeo (paso 3)

Canal elegido por Marc (2026-10-12): **estoicismo contado como historias reales**, con fuentes, en vídeos largos
(y cortos sacados de ellos para Shorts, Reels y TikTok).

**3a, guion e imágenes** (`arthur/studio/project.py`, `arthur/studio/assets.py`):

```text
<datos>/studio/proyectos/<nombre>/guion.toml  ──  arthur --studio-preparar <nombre>  ──►  recursos/img-<huella>.jpg
                                                         │                               + licencia en studio.sqlite3
                                                         └─► descripción para YouTube con fuentes y créditos
```

- **Guion** en TOML (se edita a mano): título, descripción, fuentes y escenas (texto, imagen de Commons,
  fuentes). Una escena sin fuente no se acepta salvo que se marque `opinion = true`. Ejemplo real:
  `docs/studio-guion-ejemplo.toml` (el primer vídeo: Marco Aurelio y la rebelión de Avidio Casio).
- **Licencias** (tabla `assets`, esquema 3): dominio público, CC0 y CC BY sí; CC BY-SA, NC, ND, sin dato o con
  «restricciones», bloqueadas. Solo se descarga de los servidores de Wikimedia y se guarda el sha256.
- **Wikimedia frena** (error 429) a quien pide seguido: una petición por segundo como mucho y, si pide esperar,
  se espera una vez (hasta 30 s). La identificación del programa lleva de contacto la dirección del canal
  (https://www.youtube.com/@ArthuroEstoicismo), como pide su política.

**3b, voz y subtítulos** (`arthur/studio/voice.py`, orden `--studio-voz <nombre>`):

- Piper (`.venv/bin/piper`, voz `es_ES-davefx-medium`, CC0) lee el guion **frase a frase** (trozos de ≤ 84
  caracteres): así se sabe cuánto dura cada frase y los subtítulos salen sincronizados sin transcribir.
- Cada trozo se guarda con un nombre sacado de su texto y de los ajustes: cambiar una escena solo regenera
  lo que cambió. Pausas: 0,15 s entre frases y 0,6 s entre escenas.
- Salida: `voz/narracion.wav`, `voz/linea_tiempo.json` (cuándo empieza y acaba cada escena, para el montaje) y
  `salida/subtitulos.srt`. El primer guion: 3 min 34 s de voz, 70 frases, generado en 80 s en el portátil.
- En el worktree `arthur-studio`, `.venv` es un enlace al de `~/Proyectos/arthur` (excluido de git en local).

**3c, montaje, miniatura y control** (`arthur/studio/montage.py`, orden `--studio-montar <nombre>`):

- Antes de montar se vuelve a comprobar cada imagen: licencia «ok» en el registro y mismo sha256 que al bajarla.
- Cada escena: su imagen con un acercamiento (o alejamiento) del 6 % y fundido; las verticales van enteras sobre
  ellas mismas desenfocadas. Se guardan por huella: cambiar una escena solo rehace esa.
- Voz a -14 LUFS (lo que usa YouTube): compresión suave de voz y `loudnorm` en dos pasadas (mide y luego
  ajusta). En una pasada, la narración con pausas se quedaba en -15,8. AAC 48 kHz; vídeo 1920×1080, 30 fps, H.264.
- Miniatura 1280×720: recorte desde arriba (cabezas), franja oscura en el tercio inferior y `miniatura_texto`
  (si falta, el principio del título). El texto va en un fichero (`textfile=`), nunca dentro del filtro.
- Control: tamaño, sonido, duración esperada y volumen; resultado en `salida/control.json` con el sha256 del
  vídeo (la aprobación del paso 5 irá atada a esa huella).
- **Calor:** ffmpeg va con prioridad mínima (`nice 19`) y 2 núcleos. Con todos, el primer montaje tuvo el
  portátil al 100 % durante 9 minutos.
- Primer vídeo: 3 min 35 s, control superado. Marc no tiene reproductor: `ffplay -autoexit <vídeo>`.

**Voz (decisión de Marc, 2026-10-12): lo más barato y lo menos «IA» posible.** Tras escuchar muestras de Piper y
de 7 voces de Kokoro, eligió **Kokoro: Dora (español) y Heart (inglés)**. Son las voces por defecto de cada idioma
(campo `voz` del guion para cambiarla: `kokoro:ef_dora`, `kokoro:af_heart`, `piper:es_ES-davefx-medium`).
ElevenLabs (unos 5 $/mes para uso comercial) queda para cuando el canal gane dinero.

**Voz Kokoro** (`arthur/studio/kokoro_worker.py`, motor `KokoroEngine` en `voice.py`):

- Modelo Kokoro-82M (hexgrad, Apache 2.0) con kokoro-onnx (MIT). Dependencias: `requirements-kokoro.txt`, 33
  paquetes con versión fija y sha256 (phonemizer-fork es GPL-3: afecta a redistribuir el programa, no al audio).
- Va en su **propio entorno**, sin mezclarse con la voz de ARTHUR, y en un proceso aparte que carga el modelo
  una vez por vídeo (unas 3 veces más rápido que el tiempo real en el portátil).
- Instalación (lo que se hizo en el portátil el 2026-10-12; en el servidor, igual dentro del contenedor):

```text
python -m venv ~/.local/share/arthur/studio/kokoro/venv
~/.local/share/arthur/studio/kokoro/venv/bin/pip install --require-hashes --only-binary=:all: \
    --no-deps --ignore-requires-python -r requirements-kokoro.txt
# modelo (GitHub, thewh1teagle/kokoro-onnx, versión model-files-v1.0) en ~/.local/share/arthur/studio/kokoro/:
#   kokoro-v1.0.onnx  sha256 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
#   voices-v1.0.bin   sha256 bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
```

- `--ignore-requires-python`: kokoro-onnx 0.6.1 es Python puro pero aún declara «< 3.14»; funciona en 3.14.
  Las huellas son de las ruedas para Linux x86_64 con Python 3.14: en otro Python habrá que regenerarlas.
- El propio autor avisa de que el español de Kokoro tiene menos entrenamiento que el inglés.

**Canales (2026-10-12, creados y verificados por Marc, misma cuenta de Google):**
[@ArthuroEstoicismo](https://www.youtube.com/@ArthuroEstoicismo) en español y
[@ArthuroStoicEN](https://www.youtube.com/@ArthuroStoicEN) en inglés (cuenta de marca). Al dar el permiso de
subida (paso 5), cada canal se autoriza por separado: así un vídeo nunca puede ir al canal del otro idioma.

**Dos canales (idea de Marc, 2026-10-12):** español e inglés con la misma historia, imágenes y fuentes; cambian
el guion (traducido) y la voz. Mejor dos canales que uno con dos idiomas (cada público por su lado).

## Revisión (paso 4)

`arthur/studio/review.py` (registro, tabla `reviews`, esquema 4) + `arthur/studio/web.py` y `arthur/studio/web/`
(la página, pensada para el móvil).

```text
--studio-producir NOMBRE ──► control de calidad ok ──► revisión «esperando» (canal según el idioma del guion)
                                                         │  página: vídeo, miniatura, descripción, fuentes
                                      ✓ Aprobar ─────────┤  (manda la huella del vídeo que has visto)
                                      ✎ Pedir cambios ───┤  (texto: qué cambiar)
                                      ✕ Descartar ───────┘
```

- **La aprobación vale para ESE vídeo**: se guardan las huellas del vídeo, la miniatura y los textos (título,
  descripción completa, etiquetas, idioma, canal). Al aprobar se comprueba que coinciden con lo que hay en disco
  y con lo que viste; antes de publicar, otra vez. Un montaje nuevo sustituye a la revisión anterior.
- **Canal por idioma**: `es` → @ArthuroEstoicismo, `en` → @ArthuroStoicEN (`review.CHANNELS`).
- **Entrar**: `--studio-dispositivo-nuevo movil` da una llave (se ve una vez); se pega en la página y queda en una
  cookie HttpOnly. Misma protección que la web de ARTHUR (X-Arthur + Origin para decidir, CSP sin código
  incrustado, todo el texto con `textContent`). El vídeo se sirve a trozos («Range»): Safari lo necesita.
- **Descargar**: botón «⬇ Descargar» (en los cortos, «para Instagram / TikTok»): `GET /media/<n>/download`, para
  subirlo a mano desde el móvil mientras no haya subida automática a esas redes (paso 8).
- **Arrancar**: `--studio-web` (127.0.0.1:8773; `ARTHUR_STUDIO_HOST`/`ARTHUR_STUDIO_PORT` en el servidor).
- **Aviso al móvil**: pendiente. Las suscripciones las guarda el core (`integrations/push.py`, rama
  revision-pr3); `ReviewBook(on_new=…)` es el punto donde se conectará al juntar las ramas.

## Publicar (paso 5)

`arthur/studio/publish.py`. Al pulsar «Aprobar», el publicador de `--studio-web` (un hilo, un vídeo cada vez) lo
sube; también a mano con `--studio-publicar N`.

- **Solo lo aprobado**, y antes de subir se recomprueban las huellas (`ReviewBook.ready_to_publish`).
- **Canal correcto**: con el permiso se pide `channels.list mine=true`; si el @ no es el del idioma del vídeo, o el
  canal no es el mismo que se conectó (id guardado en `youtube-canal-<idioma>.json`), no se sube nada.
- **Siempre en privado**, categoría Educación, idioma del guion. Marc lo pone público en YouTube Studio.
- **Permisos mínimos**: `youtube.upload` + `youtube.readonly`. Sin `youtube.force-ssl` (gestionar la cuenta) → los
  subtítulos no se suben solos (quedan en `salida/subtitulos.srt`; YouTube genera los suyos).
- **Subida reanudable** a trozos de 8 MB; si se corta al final, primero pregunta si el vídeo ya llegó (sin
  duplicados). Si sube el vídeo pero no la miniatura: «publicado» con aviso (no «falló», o se subiría dos veces).
- **Falló** (red, límite diario, permiso caducado): queda «falló» con el motivo; «Reintentar» en la página lo vuelve
  a poner en cola si el vídeo no ha cambiado.
- **Primera subida real (2026-10-12):** revisión #1 aprobada en la página → subida privada a @ArthuroEstoicismo
  en 13 s con miniatura. **YouTube deja ponerla pública**: el proyecto sin verificar NO queda bloqueado en privado
  (como dice ahora la página oficial de `videos.insert`). No hace falta la auditoría de Google.
- **App OAuth en modo «Prueba»**: para «Publicar la aplicación» Google pide página principal y política de
  privacidad públicas (no las tenemos). En prueba, el permiso puede caducar a los 7 días: si pasa,
  `--studio-conectar-youtube es|en` otra vez (o crear una página de privacidad en la web de Cloudflare y publicarla).
- **Enlaces de la descripción**: YouTube solo los hace pulsables tras la verificación de «funciones avanzadas» del
  canal (Studio → Configuración → Canal → Disponibilidad de funciones). Sin ella salen como texto (los créditos
  siguen ahí, que es lo que piden las licencias).

Conectar (una vez por canal):

```text
arthur --studio-cliente-youtube ~/Descargas/client_secret_….json   # cliente OAuth «Aplicación de escritorio»
arthur --studio-conectar-youtube es                                 # elegir @ArthuroEstoicismo
arthur --studio-conectar-youtube en                                 # elegir @ArthuroStoicEN
```

## Cortos verticales (Shorts, Reels, TikTok)

`arthur/studio/shorts.py`, orden `--studio-corto NOMBRE` (y al final de `--studio-producir`). Petición de Marc.

- **No genera nada nuevo**: recorta la voz del vídeo largo (las escenas de `[corto] escenas = […]`, o las primeras
  hasta ~55 s) y vuelve a montar sus imágenes en **1080×1920**. Mismas fuentes y licencias que el largo.
- **Subtítulos grandes quemados** (en los cortos casi todos lo ven sin sonido): un `drawtext` por frase, cada texto
  en su fichero. **Rótulo final** de 2,5 s: «Historia completa en el canal @…» (lleva al largo, que es el que paga).
- Menos de 59 s; mismo volumen (-14 LUFS, dos pasadas). Control en `salida/corto/control.json`.
- **Revisión aparte** (columna `kind` = `corto`, esquema 5): se aprueba y publica por separado. Título de `[corto]
  titulo`, descripción con «Historia completa en el canal …», etiqueta `shorts`. **Sin miniatura propia** (la API
  no lo permite en un Short): el fotograma es solo para la página. YouTube lo trata como Short por ser vertical y
  de menos de 3 minutos. El mismo fichero sirve para Reels y TikTok (paso 8).

## Piloto automático (paso 6)

`arthur/studio/writer.py` (Gemini escribe, el código pone las reglas) + `arthur/studio/autopilot.py` (trabajo
«piloto» de la cola). Cada día, a partir de las 6:00 (`ARTHUR_STUDIO_PILOTO_HORA`):

```text
idea ─► guion (es) ─► guion_en ─► producir_es ─► producir_en ─► 4 vídeos «esperando» en la página
```

- **Fuentes**: Gemini solo puede citar ids de un catálogo cerrado de obras clásicas de dominio público; el título,
  autor y dirección los pone el código. Cada escena con hechos lleva el lugar exacto (libro, capítulo), que sale
  también en la descripción. La última escena es la lección, marcada como opinión.
- **Revisor**: una segunda pasada de Gemini (temperatura 0) busca datos dudosos. Si encuentra algo, se reescribe
  UNA vez; lo que siga dudoso se guarda en `comprobacion.json` y la página lo enseña en un recuadro «⚠» encima
  del vídeo. Eso no sustituye la revisión de Marc: le dice dónde mirar.
- **Imágenes**: Gemini sugiere qué buscar en Commons; se usa la primera con licencia válida (o un busto de Marco
  Aurelio de reserva). Las licencias se vuelven a comprobar al preparar, como en un guion hecho a mano.
- **Gemini gratis**: unas 4-6 peticiones por día (idea, guion, revisión, a veces reescritura + revisión,
  traducción), lejos del límite de 18 por modelo (Google lo pone por modelo; agotado uno, se sigue con el siguiente Flash gratuito). Si Google dice «ocupado», la cola reintenta; si dice «no» (clave mala,
  límite del día), el trabajo falla y no se repite hasta el día siguiente.
- **Una vez al día**: el día cuenta aunque el trabajo falle, para no gastar las peticiones gratis en bucle. Para
  repetir a mano: `--studio-piloto-ahora`. Si el servidor estaba apagado a las 6, lo lanza al encender.
- **Publicar (decisión de Marc, 2026-10-13):** a las 18:00 (`ARTHUR_STUDIO_PUBLICAR_HORA`, «no» lo apaga) la
  página de revisión aprueba SOLA, y sube en PÚBLICO, lo del piloto que siga esperando, sea de las últimas 24 h y
  no tenga NINGUNA duda del revisor. Con dudas, espera al «Aprobar» de Marc. «Descartar» o «Pedir cambios» antes
  de esa hora lo paran. Se comprueban las huellas como al pulsar «Aprobar»; queda firmado como
  `piloto-automatico`. Los guiones hechos a mano siguen subiendo en privado. El modelo sigue sin poder publicar:
  lo decide código con reglas fijas.
- **Gemini saturado** (el 2026-10-13, 3.8-flash dio 503 toda la mañana): se pasa al siguiente Flash gratuito de la
  lista; el piloto reintenta hasta 6 veces, esperando de 2 a 30 minutos.

## En el servidor (contenedor `studio`)

Imagen aparte (`Dockerfile.studio`): Python 3.14 + ffmpeg y letras Noto de Debian + Kokoro en `/opt/kokoro` con
las mismas huellas que en el portátil. **No se queda en marcha**: se usa cuando hace falta y se va. Medido en el
portátil (2026-10-12): Kokoro ~575 MB y el montaje ~545 MB, nunca a la vez → límite de 1,2 GB.

**Desplegado el 2026-10-12** (commit 9b34715 del repo del servidor). Dos servicios con la misma imagen:
`studio` (la fábrica, bajo demanda) y `studio-web` (página de revisión + publicador, siempre encendida, solo por
Tailscale en http://100.64.0.10:8773, 256 MB). Datos en el volumen `studio-data` (copiados del portátil: el
servidor es ahora el principal). Prueba completa en el servidor: un vídeo de 9 s en 51 s, control superado.

```text
# producir (desde el servidor, en ~/arthur-server):
docker compose --profile studio run --rm studio --studio-producir NOMBRE
# actualizar el código: copiar la rama a studio-src (git archive) y
docker compose --profile studio build studio && docker compose up -d studio-web
```

Bloque del `studio` en `~/arthur-server/compose.yaml` (el de `studio-web` es igual pero con `command: ["--studio-web"]`,
`restart: unless-stopped`, `ports: ["100.64.0.10:8773:8773"]`, `ARTHUR_STUDIO_HOST: 0.0.0.0` y `mem_limit: 256m`):

```yaml
  # Creator Studio (docs/studio.md): vídeos para @ArthuroEstoicismo y @ArthuroStoicEN. No arranca con «up»:
  # se usa con «docker compose run --rm studio --studio-producir NOMBRE». Sin «ports»: nadie le habla.
  studio:
    build:
      context: ./studio-src             # copia de la rama «studio» (no la de core-src)
      dockerfile: Dockerfile.studio
    image: arthur-studio:local
    profiles: ["studio"]
    user: "1000:1000"
    environment:
      TZ: Europe/Madrid
    volumes:
      - studio-data:/data               # proyectos, imágenes, studio.sqlite3, clave de YouTube
      - ./studio-models:/models:ro      # kokoro/kokoro-v1.0.onnx y voices-v1.0.bin (huellas comprobadas)
    read_only: true
    tmpfs:
      - /tmp                            # sin ejecutar nada (noexec)
      - /kokoro-tmp:exec,size=33554432  # SOLO para la copia de eSpeak que hace phonemizer (ARTHUR_KOKORO_TMP)
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 1200m                    # Kokoro ~0,6 GB y el montaje ~0,55 GB, nunca a la vez
    cpus: 2                             # no le quita el procesador a ARTHUR (ni calienta)
# y en «volumes:» al final: studio-data:
```

Orden de todo: `--studio-producir NOMBRE` (preparar + voz + montar; cada paso reutiliza lo ya hecho).

Piloto automático: un tercer servicio con la misma imagen y el mismo bloque que `studio`, pero sin `profiles`,
con `command: ["--studio-piloto-diario"]` y `restart: unless-stopped` (se pasa casi todo el día dormido; la
RAM la usa solo mientras produce). Probarlo una vez a mano:
`docker compose --profile studio run --rm studio --studio-piloto-ahora`.

## Versiones

Mientras viva en esta rama, Studio **no** sube la versión de ARTHUR (`revision-pr3` sigue avanzando en paralelo y
saldrían dos 0.52.0 distintas). Su historial está aquí abajo; al fusionar se le da su número y su entrada en el
CHANGELOG.

### Historial de la rama

- **Paso 1 (2026-10-12):** `StudioStore` (tablas `jobs` y `job_steps`, `PRAGMA user_version` para migrar) y
  `JobRunner` (un trabajo a la vez, reintentos con espera doble, fallos definitivos, cancelar, retomar tras
  un corte, avisos para la web). 17 tests nuevos y la prueba real con `kill -9`.
- **Paso 2 (2026-10-12):** investigador. Cliente de YouTube Data API v3 de solo lectura (clave en fichero 600 y
  en cabecera, cuota vigilada con reserva, caché de 12 h), trabajo «investigar» de 5 pasos, puntuación de
  nichos y «vídeos que destacan», informe con observado/estimado/hipótesis, órdenes `--studio-clave-youtube`
  y `--studio-investigar`. Esquema de `studio.sqlite3` a la versión 2. 18 tests nuevos con YouTube falso.
  Primera prueba real (252 unidades) bien; con lo que se vio en ella: cortos y largos por separado, otros idiomas
  aparte en vez de mezclados y sin «veces lo normal» para canales de menos de 1.000 visitas habituales.
- **Paso 3a (2026-10-12):** guion en TOML con fuentes obligatorias, imágenes de Wikimedia Commons con licencia
  comprobada y registrada (esquema 3), orden `--studio-preparar`, primer guion real (`marco-casio`, 14 escenas,
  10 imágenes de dominio público o CC0). 12 tests nuevos.
- **Paso 3b (2026-10-12):** voz frase a frase con Piper, narración unida con pausas, línea de tiempo y
  subtítulos SRT; orden `--studio-voz`. 7 tests nuevos (Piper falso).
- **Paso 3c (2026-10-12):** montaje con ffmpeg, miniatura, descripción y control de calidad; orden
  `--studio-montar`; campos `miniatura_texto` y `miniatura_imagen` en el guion. Primer vídeo completo.
  6 tests nuevos (ffmpeg falso).
- **Voz Kokoro (2026-10-12):** motores de voz intercambiables (`PiperEngine`, `KokoroEngine`), campo `voz` en el
  guion, Dora y Heart por defecto (elección de Marc), Kokoro en su propio entorno con huellas
  (`requirements-kokoro.txt`) y proceso aparte; volumen en dos pasadas con compresión (-14,4 LUFS). Vídeo de
  Marco Aurelio rehecho con Dora; guion en inglés `marco-casio-en` (Heart) para el futuro segundo canal.
- **Servidor y paso 4 (2026-10-12):** `Dockerfile.studio`, `--studio-producir`; revisión con aprobación atada a
  las huellas (esquema 4), página para el móvil con llaves por aparato, órdenes `--studio-revisar`, `--studio-web`
  y `--studio-dispositivo-nuevo`. 10 tests nuevos (servidor real en 127.0.0.1).
- **Paso 5 (2026-10-12):** subida a YouTube en privado del vídeo aprobado (reanudable, sin duplicados, canal
  comprobado, permisos mínimos), miniatura, publicador en la página, «Reintentar», órdenes
  `--studio-cliente-youtube`, `--studio-conectar-youtube` y `--studio-publicar`. 10 tests nuevos (YouTube falso).
- **Cortos (2026-10-12):** `shorts.py`, sección `[corto]` del guion, revisiones por tipo (esquema 5), orden
  `--studio-corto`. 8 tests nuevos.
- **Paso 6 (2026-10-13):** escritor de guiones con Gemini (catálogo cerrado de fuentes, revisor, una reescritura,
  imágenes con licencia, traducción al inglés) y trabajo «piloto» (idea → guion → guion_en → producir_es →
  producir_en); avisos del revisor en la página; órdenes `--studio-piloto-ahora` y `--studio-piloto-diario`.
  15 tests nuevos (Gemini y Commons falsos).
- **Publicar solo (2026-10-13):** aprobación automática a las 18:00 de lo del piloto sin dudas, subida en público
  de lo del piloto, aviso fijo en la página, cambio de modelo de Gemini cuando está saturado. 4 tests nuevos.
- **Marca (2026-10-13):** colores del kit de marca de Business Lab (`05-Marketing-y-Ventas/marca-arthuro-estoicismo.md`,
  aprobado por Marc). Miniatura: franja obsidiana `#111113`, filo de bronce `#C29A5B` y, si hay dos líneas, la segunda en
  bronce. Tarjeta final de los cortos: velo obsidiana, mensaje en mármol `#EDE7DB` centrado línea a línea y el canal en
  bronce. Antes, el mensaje de dos líneas salía alineado a la izquierda. 2 tests nuevos (853 en total).
- **Formatos (2026-10-13):** el piloto alterna los formatos del kit de marca por día de la semana:
  «Una carta de Séneca» los miércoles y domingos (`autopilot.CARTA_DAYS`; experimento, debe citar `seneca-cartas`)
  y «La prueba» (historia real) el resto. El formato viaja con la idea hasta el prompt del guion. «¿Lo dijo de
  verdad?» queda **manual**: afirmar que una cita no está en una obra exige buscar en el texto completo, y Gemini
  no lo lee. 3 tests nuevos (856).
