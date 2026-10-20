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
  se espera una vez (hasta 30 s). La identificación del programa lleva de contacto la dirección pública de
  `ARTHUR_STUDIO_CONTACTO` (tu canal o tu web), como pide su política.

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

**Canales:** uno en español y otro en inglés (pueden ser cuentas de marca de la misma cuenta de Google); su @
va en `ARTHUR_STUDIO_CANAL_ESTOICISMO_ES` y `ARTHUR_STUDIO_CANAL_ESTOICISMO_EN`. Al dar el permiso de
subida (paso 5), cada canal se autoriza por separado: así un vídeo nunca puede ir al canal del otro idioma.

**Dos canales (idea de Marc, 2026-10-12):** español e inglés con la misma historia, imágenes y fuentes; cambian
el guion (traducido) y la voz. Mejor dos canales que uno con dos idiomas (cada público por su lado).

## Perfiles de canal y apartado «Cuentas» (0.62.2, 2026-10-17)

Frutinovelas (un canal de solo Shorts, 2026-10-17) también es en español, así que **el idioma ya no decide el canal**: lo decide el **perfil**
(`arthur/studio/channels.py`).

| Perfil | En la página | Idioma | Canal (variable de `.env`) | Publicación |
|---|---|---|---|---|
| `estoicismo_es` | Estoicismo · español | es | `ARTHUR_STUDIO_CANAL_ESTOICISMO_ES` | sí (como siempre) |
| `estoicismo_en` | Estoicismo · inglés | en | `ARTHUR_STUDIO_CANAL_ESTOICISMO_EN` | sí (como siempre) |
| `frutinovelas` | Frutinovelas · español | es | `ARTHUR_STUDIO_CANAL_FRUTINOVELAS` | activa, un vídeo al día (reglas abajo) |

El @ de cada canal sale de su variable (pásalas al contenedor de Studio). Los valores por defecto son de ejemplo
(`@TuCanalEstoicismo`…): con ellos la comprobación del canal antes de subir falla y no se sube nada.

- **Guion**: campo opcional `perfil = "frutinovelas"`. Sin él, el de estoicismo de su idioma (todos los guiones
  de antes siguen igual). Un perfil cuyo idioma no es el del guion se rechaza.
- **Revisiones**: columna `profile` (esquema 6). Las que ya había se asignan por el @ que se aprobó, así que siguen
  yendo al mismo canal; la huella de los textos no cambia (el perfil no entra: su @ ya está dentro), así que las
  aprobaciones pendientes siguen valiendo.
- **Publicar**: el publicador pide el permiso DEL PERFIL de la revisión, comprueba que el permiso preparado es de
  ese perfil y, como antes, que YouTube dice que es ese @ (y el mismo id que se conectó). Un vídeo de Frutinovelas
  con el permiso de estoicismo, o al revés, falla antes de subir nada.
- **Permisos**: `youtube-oauth-<perfil>` y `youtube-canal-<perfil>.json`. Los de antes (`youtube-oauth-es`,
  `youtube-canal-es.json`, `…-en…`) **se siguen leyendo** mientras no haya uno nuevo: no se renombra nada del
  volumen. Al volver a conectar un canal de estoicismo se escribe el nombre nuevo y el viejo queda sin usar.
- **Conectar**: `--studio-conectar-youtube estoicismo_es|estoicismo_en|frutinovelas` (`es`/`en` siguen valiendo).
  El permiso nuevo se guarda primero aparte (`….pendiente`) y solo se queda si Google devuelve el canal esperado;
  si devuelve otra cuenta no se guarda nada y el permiso anterior sigue como estaba (antes se borraba).
- **Frutinovelas**: un Short nuevo al día, público a las 18:00 si el revisor no deja dudas. El trabajo diario solo
  se programa cuando el servidor tiene `ARTHUR_STUDIO_FRUTAS_HORA`. Los importados siguen fuera de esa aprobación
  automática: solo salen después de pulsar «Aprobar».
- **Un vídeo al día** (`per_day=1` en `channels.py`): lo aprobado de Frutinovelas no sube al pulsar «Aprobar» ni al
  reiniciar. Espera en cola y a las 18:00 sale el más antiguo, en **público** y declarado como contenido sintético,
  también si es importado. Una subida fallida (p. ej. sin cuota) gasta el día: no se tiran detrás los demás.
  Mientras haya aprobados en cola, la rutina diaria no fabrica episodios nuevos.

**Página «Cuentas»** (botón arriba: «Vídeos | Cuentas»). Siempre las tres filas, en ese orden, con nombre, @, id
público del canal (cuando se conoce), estado con símbolo y texto, fecha de la última comprobación y explicación.

| Estado | Texto | Cuándo |
|---|---|---|
| `disconnected` | Sin conectar | No hay permiso guardado (Frutinovelas: «Canal creado · pendiente de conectar») |
| `unverified` | Permiso guardado · sin comprobar | Hay permiso, pero no se ha preguntado a Google (o se reconectó después) |
| `connected` | Conectada | Google confirma que el permiso es de ese @ y del mismo id |
| `reconnect` | Requiere reconexión | Google ya no acepta el permiso (caducó o se retiró) |
| `wrong_account` | Cuenta equivocada | El permiso es de otro canal |
| `unavailable` | No se pudo comprobar | Sin red, sin cliente OAuth, error de YouTube… |

- `GET /api/accounts` y `POST /api/accounts/<perfil>/check` (con llave; con cookie, además X-Arthur). «Comprobar»
  solo hace `channels.list mine=true` (lectura): no sube nada ni lanza trabajos. El resultado se apunta en
  `youtube-estado-<perfil>.json` (estado, fecha y explicación). Hacia la página nunca va el correo, el permiso ni el
  cliente OAuth.
- «Conectar» / «Reconectar» despliega la explicación y la orden exacta: **no hay conexión desde la web** (Google
  vuelve a 127.0.0.1 del ordenador que pidió el permiso; eso solo lo resuelve la terminal). Studio nunca pide la
  contraseña de Google.
- Las tarjetas de vídeo son botones (se abren con Tab + Enter) y el campo de la llave tiene etiqueta visible.

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
- **Canal por perfil** (desde 0.62.2): el `perfil` del guion o, si no tiene, `es` → el canal de estoicismo en
  español y `en` → el de inglés (ver «Perfiles de canal»).
- **Entrar**: `--studio-dispositivo-nuevo movil` da una llave (se ve una vez); se pega en la página y queda en una
  cookie HttpOnly. Misma protección que la web de ARTHUR (X-Arthur + Origin para decidir, CSP sin código
  incrustado, todo el texto con `textContent`). El vídeo se sirve a trozos («Range»): Safari lo necesita.
- **Descargar**: botón «⬇ Descargar» (en los cortos, «para Instagram / TikTok»): `GET /media/<n>/download`, para
  subirlo a mano desde el móvil mientras no haya subida automática a esas redes (paso 8).
- **Arrancar**: `--studio-web` (127.0.0.1:8773; `ARTHUR_STUDIO_HOST`/`ARTHUR_STUDIO_PORT` en el servidor).
- **Elige canal** (0.63.1): al entrar, tres tarjetas (Estoicismo · español, Estoicismo · inglés, Frutinovelas) con
  su @, su estado (el mismo de «Cuentas», `GET /api/channels`) y cuántos vídeos esperan decisión. Al elegir una,
  «Vídeos» enseña solo los de ese canal: el filtro lo hace el servidor (`GET /api/reviews?perfil=<perfil>`, también
  `es`/`en`; un canal desconocido → 400) y el límite de 30 se cuenta dentro del canal. Arriba sale el canal y
  «Cambiar canal». La página recuerda el último canal en ESE aparato (`localStorage`, solo comodidad; si el
  navegador no deja guardar, vuelve a preguntar) y entra directa la vez siguiente. «Cuentas» sigue con los tres.
  Las revisiones antiguas sin perfil se colocan por su @ (o su idioma), como hace el publicador.
- **Aviso al móvil**: «vídeo nuevo», «rehecho con tus cambios», «subido» y «no se ha podido subir» llevan el vídeo
  y su canal. Studio solo pide el destino fijo `studio` con el número y el perfil; la dirección la pone el núcleo
  (`ARTHUR_STUDIO_PUBLIC_URL` + `#v12-frutinovelas`, p. ej. `http://100.64.0.10:8773`, validada como
  `ARTHUR_PANEL_PUBLIC_URL`) y solo la acepta de la llave `studio`. Tocar el aviso abre ese vídeo ya en su canal.
  Sin esa variable el núcleo rechaza el enlace (503) y Studio manda el aviso sin él.

## Publicar (paso 5)

`arthur/studio/publish.py`. Al pulsar «Aprobar», el publicador de `--studio-web` (un hilo, un vídeo cada vez) lo
sube; también a mano con `--studio-publicar N`.

- **Solo lo aprobado**, y antes de subir se recomprueban las huellas (`ReviewBook.ready_to_publish`).
- **Canal correcto**: con el permiso se pide `channels.list mine=true`; si el @ no es el del perfil del vídeo, o el
  canal no es el mismo que se conectó (id guardado en `youtube-canal-<perfil>.json`, o el viejo
  `youtube-canal-<idioma>.json`), no se sube nada.
- **Siempre en privado**, categoría Educación, idioma del guion. Marc lo pone público en YouTube Studio.
- **Permisos mínimos**: `youtube.upload` + `youtube.readonly`. Sin `youtube.force-ssl` (gestionar la cuenta) → los
  subtítulos no se suben solos (quedan en `salida/subtitulos.srt`; YouTube genera los suyos).
- **Subida reanudable** a trozos de 8 MB; si se corta al final, primero pregunta si el vídeo ya llegó (sin
  duplicados). Si sube el vídeo pero no la miniatura: «publicado» con aviso (no «falló», o se subiría dos veces).
- **Falló** (red, límite diario, permiso caducado): queda «falló» con el motivo; «Reintentar» en la página lo vuelve
  a poner en cola si el vídeo no ha cambiado.
- **Primera subida real (2026-10-12):** revisión #1 aprobada en la página → subida privada al canal en español
  en 13 s con miniatura. **YouTube deja ponerla pública**: el proyecto sin verificar NO queda bloqueado en privado
  (como dice ahora la página oficial de `videos.insert`). No hace falta la auditoría de Google.
- **App OAuth en modo «Prueba»**: para «Publicar la aplicación» Google pide página principal y política de
  privacidad públicas (no las tenemos). En prueba, el permiso puede caducar a los 7 días: si pasa,
  `--studio-conectar-youtube <perfil>` otra vez (o crear una página de privacidad en la web de Cloudflare y publicarla).
- **Enlaces de la descripción**: YouTube solo los hace pulsables tras la verificación de «funciones avanzadas» del
  canal (Studio → Configuración → Canal → Disponibilidad de funciones). Sin ella salen como texto (los créditos
  siguen ahí, que es lo que piden las licencias).

Conectar (una vez por canal):

```text
arthur --studio-cliente-youtube ~/Descargas/client_secret_….json   # cliente OAuth «Aplicación de escritorio»
arthur --studio-conectar-youtube estoicismo_es                      # elegir tu canal de estoicismo en español (o «es»)
arthur --studio-conectar-youtube estoicismo_en                      # elegir el de inglés (o «en»)
arthur --studio-conectar-youtube frutinovelas                       # elegir el de Frutinovelas
```

En el servidor (desde la carpeta de ARTHUR, sin pantalla: al final pide pegar la dirección de la página que no carga):
`docker compose --profile studio run --rm studio --studio-conectar-youtube frutinovelas`.

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

## Importar Shorts hechos fuera de Studio (0.63.3, Frutinovelas)

`arthur/studio/importer.py`. Los episodios de Frutinovelas se montan fuera de Studio (Kokoro + ffmpeg en el
portátil). Esta orden los trae a la página de revisión para verlos en su canal, aprobarlos y que Studio los suba por
el camino de siempre (`publish.py`, con todas sus comprobaciones de huellas y de canal):

```text
arthur --studio-importar-short PERFIL VIDEO.mp4 --titulo "…" [--descripcion "…"] [--srt subtitulos.srt]
```

- **Solo a un canal de cortos** (`Profile.product == "corto"`: hoy solo `frutinovelas`). A estoicismo no: allí
  cada corto sale de su vídeo largo y lleva sus fuentes. Un perfil desconocido también se rechaza.
- **Debe ser un Short**: `ffprobe` (la misma orden del control de calidad) tiene que decir vertical 1080×1920,
  60 s como mucho, vídeo H.264 y sonido AAC. El volumen se mide y se apunta, pero no bloquea. Título de 1 a 100
  letras y descripción de hasta 5.000 bytes, sin `<` ni `>` (YouTube los rechaza). Si algo falla, no se copia nada.
- **Se copia** (el original no se toca) a `proyectos/importado-<huella>/salida/corto/` como un corto montado por
  Studio: `video.mp4`, `miniatura.jpg` (un fotograma, solo para la página), `control.json` y, si se da,
  `subtitulos.srt` (no se sube: YouTube pide el permiso grande). Los textos y de dónde vino van en
  `importado.json`; la revisión lleva el origen en la columna `origin` (esquema 7) y sale como «Importado».
- **Nunca se aprueba sola**: la regla de las 18:00 se salta los importados aunque su carpeta tenga un
  `comprobacion.json`. Solo con «Aprobar» en la página (el mismo botón y la misma huella del vídeo visto).
- **Sube en PRIVADO** si su canal no tiene límite diario. En Frutinovelas (un vídeo al día) sale en público, uno
  cada día a las 18:00, en el orden en que se aprobaron (ver «Un vídeo al día» arriba).
- **Con la publicación del canal activa** (`publishing=True` desde 0.66.0), lo aprobado antes de activarla tampoco
  sube de golpe al reiniciar: entra en la misma cola de uno al día. El script de despliegue dice cuántos esperan. «Pedir cambios» solo apunta el texto: no hay guion que
  rehacer; se rehace fuera y se vuelve a importar.
- **Importar dos veces** el mismo vídeo no duplica nada (misma carpeta, misma revisión). Con otro título o
  descripción, la revisión nueva sustituye a la que esperaba.

**En el servidor.** El contenedor `studio` solo ve su volumen `studio-data` (y el sistema es de solo lectura), así
que el vídeo se le presta con un `-v` de solo lectura en esa misma orden; no hace falta tocar `compose.yaml`.
Por ejemplo, con el episodio en `studio-importar/ep02/` dentro de la carpeta de ARTHUR del servidor:

```text
docker compose --profile studio run --rm -v "$PWD/studio-importar:/importar:ro" studio \
  --studio-importar-short frutinovelas /importar/ep02/episodio-02.mp4 \
  --titulo "Título del episodio | Frutinovelas 2" \
  --descripcion "…" --srt /importar/ep02/subtitulos.srt
```

La copia queda dentro de `studio-data`; la carpeta `studio-importar` se puede vaciar después. La página
(`studio-web`) la ve al momento: canal «Frutinovelas», tarjeta «▮ Corto · Importado».

**Rutina diaria de Frutinovelas.** `studio-piloto` prepara desde la hora de `ARTHUR_STUDIO_FRUTAS_HORA` (p. ej.
10) un episodio de 8–10 escenas con Workers AI, planos de la biblioteca comprobados por huella, voces Kokoro y
montaje vertical. Si falta un plano o la revisión deja dudas, no se aprueba solo. A las 18:00 `studio-web` aprueba
y sube en público únicamente el episodio nuevo de Studio con `frutinovela.json`, sin dudas y sin cambios tras
revisar; YouTube recibe la marca de contenido sintético. Los importados conservan la aprobación manual. Todo sale
de uno en uno, un vídeo al día. Kiro no entra en la rutina diaria porque su voz Piper no está en la imagen de Studio.

Lo que necesita `studio-piloto` en tu `compose.override.yaml` (sin la hora, la rutina no se programa):

```yaml
    environment:
      ARTHUR_STUDIO_FRUTAS_HORA: "10"                       # hora de empezar (sin ella, apagada)
      ARTHUR_STUDIO_FRUTAS_BIBLIOTECA: /frutas              # planos y referencias, solo lectura
      ARTHUR_STUDIO_CF_ACCOUNT: ${ARTHUR_CF_ACCOUNT:-}      # id de tu cuenta de Cloudflare (no es secreto)
      ARTHUR_STUDIO_CF_TOKEN_FILE: /run/secrets/workers_ai_token
      ARTHUR_STUDIO_FRUTAS_NEURONAS: "1500"                 # tope diario de Neurons de Frutinovelas
    volumes:
      - ./frutas-biblioteca:/frutas:ro
    secrets: [workers_ai_token]
```

Para desactivar la publicación de Frutinovelas: en `arthur/studio/channels.py`, en la línea del perfil
`frutinovelas`, cambiar `publishing=True` por `publishing=False`; tests como siempre, y
`docker compose --profile studio build studio && docker compose up -d studio-web`. Antes de publicar nada, el canal
tiene que estar conectado (`--studio-conectar-youtube frutinovelas`, «Conectada» en «Cuentas»).

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
- **Pedir cambios** (desde 2026-10-14): el texto de la revisión pone en la cola un trabajo «cambios» (lo hace el servicio
  `studio-piloto`): Gemini rehace el guion de ESE vídeo con lo pedido, con el mismo catálogo y el mismo revisor;
  las imágenes que ya tenía se quedan y las nuevas se buscan con licencia. El guion viejo queda en
  `guion.anterior.toml`, se vuelve a montar (largo y corto) y el vídeo nuevo sale en la página como «esperando».
  ARTHUR avisa al terminar o si falla. Un guion hecho a mano sigue siendo «a mano» (no se publica solo).
  Antes de esto el texto se guardaba y no pasaba nada.
- **Gemini saturado** (el 2026-10-13, 3.8-flash dio 503 toda la mañana): se pasa al siguiente Flash gratuito de la
  lista; el piloto reintenta hasta 6 veces, esperando de 2 a 30 minutos.

## En el servidor (contenedor `studio`)

Imagen aparte (`Dockerfile.studio`): Python 3.14 + ffmpeg, letras Noto de Debian y Kokoro en `/opt/kokoro`.
`studio` produce bajo demanda y se detiene al acabar; `studio-web` mantiene la página de revisión. Configura
la dirección de escucha para tu red privada y conserva los proyectos en el volumen `studio-data`.

```text
# producir (desde el servidor, en la carpeta de ARTHUR):
docker compose --profile studio run --rm studio --studio-producir NOMBRE
# actualizar el código: git pull y
docker compose --profile studio build studio && docker compose up -d studio-web
```

Bloque del `studio` para tu `compose.override.yaml` (el de `studio-web` usa `command: ["--studio-web"]`,
`restart: unless-stopped`, `ARTHUR_STUDIO_HOST: 0.0.0.0` y un puerto ligado a tu IP privada de Tailscale):

```yaml
  # Creator Studio (docs/studio.md): vídeos para tus canales. No arranca con «up»:
  # se usa con «docker compose run --rm studio --studio-producir NOMBRE». Sin «ports»: nadie le habla.
  studio:
    build:
      context: .
      dockerfile: Dockerfile.studio
    image: arthur-studio:local
    profiles: ["studio"]
    user: "1000:1000"
    environment:
      TZ: Europe/Madrid
      ARTHUR_STUDIO_CANAL_ESTOICISMO_ES: ${ARTHUR_STUDIO_CANAL_ESTOICISMO_ES:-}   # el @ de cada canal (.env)
      ARTHUR_STUDIO_CANAL_ESTOICISMO_EN: ${ARTHUR_STUDIO_CANAL_ESTOICISMO_EN:-}
      ARTHUR_STUDIO_CANAL_FRUTINOVELAS: ${ARTHUR_STUDIO_CANAL_FRUTINOVELAS:-}
      ARTHUR_STUDIO_CONTACTO: ${ARTHUR_STUDIO_CONTACTO:-}                         # contacto para Wikimedia
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
- **Marca (2026-10-13):** colores de un kit de marca propio. Miniatura: franja obsidiana `#111113`, filo de bronce `#C29A5B` y, si hay dos líneas, la segunda en
  bronce. Tarjeta final de los cortos: velo obsidiana, mensaje en mármol `#EDE7DB` centrado línea a línea y el canal en
  bronce. Antes, el mensaje de dos líneas salía alineado a la izquierda. 2 tests nuevos (853 en total).
- **Formatos (2026-10-13):** el piloto alterna los formatos del kit de marca por día de la semana:
  «Una carta de Séneca» los miércoles y domingos (`autopilot.CARTA_DAYS`; experimento, debe citar `seneca-cartas`)
  y «La prueba» (historia real) el resto. El formato viaja con la idea hasta el prompt del guion. «¿Lo dijo de
  verdad?» queda **manual**: afirmar que una cita no está en una obra exige buscar en el texto completo, y Gemini
  no lo lee. 3 tests nuevos (856).
- **Pedir cambios (2026-10-14):** el botón ya hace algo: trabajo «cambios» (guion → producir) con
  `Writer.revise`, aviso por ARTHUR. 6 tests nuevos (870 en total).
- **Cortos que se pasan (2026-10-14):** si las escenas de `[corto]` suman más de 59 s, se quitan por el final en
  vez de no hacer el corto (pasó con el vídeo #12: Gemini eligió 94 s). 1 test nuevo (871).
- **Descripción demasiado larga (2026-10-14):** los créditos solo citan las imágenes que siguen en el guion (al
  rehacer el #12 se colaban las viejas: 5.206 bytes y error 400 de YouTube al publicar), y una descripción de más de
  5.000 bytes no llega a revisión. 2 tests nuevos (873).
