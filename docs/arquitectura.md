# Arquitectura

> Esta guía describe las piezas de ARTHUR en un solo equipo (el «modo completo»: todo en tu ordenador). En el modo
> servidor + aparatos ([servidor.md](servidor.md)) las mismas piezas se reparten: el asistente, el cerebro y las
> herramientas de servidor van en el contenedor `core`, y cada aparato pone micrófono, altavoz, Orb, cámara y sus
> herramientas de escritorio.

## Flujo de una petición (Fase 2)

```text
Tú escribes ──► interfaces/cli.py
                    │ handle_text("¿qué hora es?")
                    ▼
               core/assistant.py ──► estado THINKING ──┐
                    │                                  │ cada cambio se publica
                    │ brain.reply(texto, historial)    │ en el bus de eventos
                    ▼                                  ▼
               brain/rules.py                     core/events.py ──► suscriptores
                    │ BrainReply("Son las 19:16.")       (hoy: CLI con --estados;
                    ▼                                     mañana: Orb, logs, voz…)
               core/assistant.py ──► SPEAKING ──► IDLE
                    │
                    ▼
               interfaces/cli.py muestra la respuesta
```

## Herramientas (Fase 3)

```text
cerebro ── propone ToolCall("write_file", {...}) ──► core/router.py
                                                       1. ¿existe?               → error
                                                       2. ¿argumentos válidos?   → error
                                                       3. tool.check (rutas/cmd) → DENEGADO
                                                       4. política (riesgo/modo) → DENEGADO
                                                       5. confirmación           → CANCELADO
                                                       6. ejecutar               → ok / error
                                                       7. auditoría + evento
cerebro ◄── resultado en texto ─────────────────────────┘
```

Si algo falla de forma inesperada ANTES de ejecutar (la comprobación, el cálculo del riesgo, el resumen o la
pregunta de confirmación), el router no ejecuta la herramienta, devuelve un error y lo anota en la auditoría; si
falla el resumen o la pregunta, cuenta como un NO.

El asistente repite «cerebro → herramientas → cerebro» como máximo 5 rondas por petición.
Estados: THINKING → EXECUTING → THINKING → SPEAKING → IDLE.

### Capas de seguridad de `run_command`

1. Lista de argumentos, sin shell: `;`, `|`, `&&` o `$( )` son texto literal.
2. Lista negra fija (`security/commands.py`): gana siempre a la lista permitida.
3. Lista permitida (`command_allowlist`).
4. Los argumentos con forma de ruta pasan por la política de rutas.
5. Confirmación obligatoria.
6. Sandbox `bwrap`: sistema en solo lectura, `$HOME` vacío salvo `read_roots`, `/run` y `/tmp` vacíos,
   sin red, sin variables de entorno y con límite de tiempo.

⚠ Si añades a `command_allowlist` un programa que lee ficheros (por ejemplo `cat`), podrá leer
cualquier fichero de `read_roots` que no esté tapado. Los nombres sensibles (`.env`, claves…) se bloquean
cuando aparecen como argumento, pero conviene mantener la lista corta.

## Cerebro LLM local (Fase 4)

`brain/ollama.py` habla con Ollama en `http://127.0.0.1:11434/api/chat` usando solo `urllib`.
Envía: mensaje de sistema + conversación + descripción de las herramientas. El modelo responde con
texto o con llamadas a herramientas, que pasan por el router como cualquier otra.

Decisiones tomadas tras medir en un portátil de pruebas (Ryzen 5 de 2022, solo CPU):

| Problema medido | Causa | Solución | Resultado |
|---|---|---|---|
| 1–3 min por respuesta | La hora estaba en el mensaje de sistema → la caché de Ollama no servía y reprocesaba ~3.000 tokens cada vez (~40 tokens/s) | Mensaje de sistema fijo; la fecha y hora van en cada mensaje del usuario | Procesado inicial: de ~60 s a 2–4 s |
| Cientos de tokens ocultos por respuesta | `qwen3:4b` es la variante *Thinking*: siempre razona antes (en inglés) y no se puede desactivar | Usar `qwen3:4b-instruct` | 5–10 s por respuesta |

Seguridad: `brain.ollama_url` solo admite este equipo (127.0.0.1, localhost o ::1), y se ignoran
los proxies del sistema. Así la conversación no puede salir del portátil por un error de configuración.

## Integración con el escritorio (Fase 5)

- **Apps:** se leen los `.desktop` (lo mismo que usa tu lanzador) y se abren con `uwsm app -- <id>.desktop`,
  igual que los atajos de Hyprland: cada app queda en su propio scope de systemd, independiente de ARTHUR.
  ARTHUR nunca ejecuta la línea `Exec=`. Las apps cuyo `Exec` usa `pkexec`/`sudo` son PELIGROSAS.
- **`uwsm app` se convierte en la app** y no termina mientras esté abierta: se lanza sin esperar y se
  comprueba durante 2 s que no haya fallado.
- **Riesgo según los argumentos** (`Tool.risk_for`): abrir Firefox (de confianza) es de bajo riesgo,
  abrir otra app pide confirmación.
- **Programas fijos, sin sandbox:** `notify-send`, `wpctl`, `wl-copy`/`wl-paste`, `xdg-open`, `uwsm`
  necesitan tu sesión gráfica. Los elige el código (nunca el modelo), sin shell, con argumentos validados
  y `--` antes del texto libre. Los tests usan versiones falsas y nunca tocan el escritorio.
- **Enlaces:** solo `http`/`https`; nada de `file://`, `javascript:` u otros esquemas que abran programas.

### Hyprland: ventanas y escritorios

En Hyprland 0.56 las acciones son **Lua que se ejecuta dentro del compositor** (que puede lanzar cualquier
programa), y la wiki lo dice: *«If no window is provided, the active window is used»*. Se comprobó por las
malas: un `close` con una dirección inexistente cerró la ventana activa. Por eso:

- **Consultas** (`HyprlandReader`): solo `j/clients`, `j/workspaces`, etc.
- **Acciones** (`HyprlandController`): cuatro plantillas Lua fijas (cambiar escritorio, enfocar, mover, cerrar).
  El modelo elige la app y el escritorio; ARTHUR busca la ventana en la lista real y usa su **dirección
  exacta** (validada con `^0x[0-9a-f]+$`) y un escritorio entero del 1 al 10. El texto del modelo nunca llega a Hyprland.
- **Búsqueda y acción en un solo bloque Lua:** `local w = hl.get_window("address:0x…") if w == nil then
  error("ARTHUR:no-window") end hl.dispatch(…{ window = w })`. Si la ventana desaparece justo antes,
  Hyprland no hace nada (comprobado: `get_window` devuelve `nil` para direcciones inexistentes).
- **`close` es una petición educada** (como pulsar la X); nunca `kill`. Pide confirmación.
- **No puede cerrar la terminal donde se ejecuta ARTHUR** (se comprueba la cadena de procesos padre).
- **Varias ventanas iguales:** no adivina; pide el escritorio.
- **Validación real sin cambiar nada:** cambiar al escritorio actual, enfocar la ventana ya enfocada y mover
  una ventana a su propio escritorio; estado antes y después idéntico.

## El Orb (Fase 6)

```text
núcleo ── EventBus ──► api/server.py ── SSE (/events) ──► navegador: js/main.js
                       127.0.0.1:8765                       ├─ orb.js  (Three.js: partículas, anillos, bloom)
                       solo lectura                          └─ hud.js  (reloj, conversación, actividad)
```

- **Server-Sent Events en lugar de WebSocket.** El Orb solo necesita recibir eventos; SSE va sobre HTTP
  normal, lo sirve la librería estándar de Python y el navegador se reconecta solo. Cuando el Orb tenga que
  enviar cosas (confirmaciones, voz), ampliaremos.
- **Sin compilación ni npm.** JavaScript normal + una sola librería, Three.js, copiada en `ui/orb/vendor/three`
  desde el paquete oficial con su firma sha512 verificada (detalles en `VENDOR.md`).
- **Seguridad del servidor:** solo 127.0.0.1; token aleatorio por arranque (URL la primera vez, luego cookie
  HttpOnly + SameSite=Strict); comprobación de `Host` contra DNS rebinding; CSP estricta (nada externo);
  sin rutas de escritura; solo ficheros de `ui/orb` con extensiones conocidas.
  Una conexión que no envía su petición en 15 s se cierra (`REQUEST_TIMEOUT`): no se acumulan hilos. Los tokens
  se comparan en tiempo constante y con bytes (un `?t=é` da 403, no un fallo).
- **El texto del modelo nunca se interpreta como HTML** (`textContent`), así que no puede inyectar código.
- **Estados con tiempo mínimo en pantalla** (`states.js`): el núcleo cambia en milisegundos; la interfaz
  encola los cambios y muestra cada uno un mínimo para que se vean. SPEAKING dura según la longitud de la respuesta.
- **Rendimiento:** resolución interna limitada a 1,5×, bloom a resolución reducida, ~5.400 partículas.
- **Probado sin tocar tu pantalla:** Firefox headless con perfil temporal, controlado por Marionette.

## Voz: TTS (Fase 7)

```text
Assistant (SPEAKING) ── speak(texto) ──► voice/tts.py ── JSON por stdin ──► piper_worker.py (.venv, voz cargada)
                                              ▲                                  │
                                              └──── audio por stdout (tramas) ◄──┘
                                              │
                                              ├── trozos de 50 ms a ritmo real ──► pw-play ──► altavoces
                                              └── volumen de cada trozo ──► evento audio_level ──► Orb
```

- **Proceso auxiliar permanente:** arrancar Piper cuesta ~2,7 s (Python + modelo). El auxiliar lo
  carga una vez; después cada frase empieza a sonar casi al instante.
- **Licencias:** Piper es GPL-3.0. Solo `piper_worker.py` lo usa (y es GPL-3.0); ARTHUR habla con él
  por tuberías, como con cualquier programa, así que el resto del proyecto no depende de Piper.
- **El auxiliar no hereda las variables de entorno de ARTHUR** (no ve tokens ni contraseñas).
- **Ritmo real:** el audio se manda a `pw-play` en trozos de 50 ms con 150 ms de adelanto. Así el volumen
  que ve el Orb coincide con lo que suena y `stop()` corta la voz al momento.
- **Texto hablable:** se quitan markdown, emojis y enlaces; el código y los textos muy largos se dejan
  en pantalla.
- **La voz es un extra:** si falla, la respuesta escrita sigue valiendo. El texto aparece antes de hablar.
- **Tests sin sonido:** auxiliar y reproductor falsos.

## Escucha: STT (Fase 8)

```text
Enter ─► Recorder (pw-record, 16 kHz mono, en memoria) ─► nivel ─► Orb (LISTENING)
Enter ─► WhisperTranscriber: WAV en carpeta temporal privada ─► whisper-cli ─► texto ─► se borra la carpeta
                          ─► Assistant.handle_text(texto)  (igual que si lo hubieras escrito)
```

- **whisper.cpp del repositorio oficial** (`whisper-cpp`, MIT): C/C++, sin dependencias de Python.
- **Modelo `ggml-small-q5_1`** (190 MB). Medido en un portátil de pruebas: ~4 s por frase.
- **Prompt inicial con vocabulario** (apps, términos técnicos y las apps de confianza): con él, «Firefox» y
  «RAM» pasan de reconocerse mal («Zyphos», «AM») a reconocerse bien.
- **Búsqueda sencilla (beam 1) por defecto:** acertó igual que beam 5 en las pruebas y es ~30 % más rápida.
- **Contra las "alucinaciones" de Whisper:** el audio casi silencioso no se transcribe, y se descartan las frases
  típicas de subtítulos (p. ej. «Subtítulos realizados por la comunidad de Amara.org»).
- **Privacidad:** micrófono abierto solo entre los dos Enter, máximo 60 s; audio en memoria; whisper-cli sin
  nuestras variables de entorno.
- **Probado sin micrófono:** frases generadas con la voz de Piper y convertidas a 16 kHz.

## Memoria (Fase 9)

| Qué | Dónde | Cuándo se guarda |
|---|---|---|
| Configuración | `~/.config/arthur/config.toml` | La editas tú |
| Preferencias y datos | SQLite `memory.sqlite3` (600, carpeta 700) | Solo si pides «recuerda que…» y confirmas |
| Historial | Misma base de datos | Solo con `save_history = true`; caduca a los `history_days` |
| Conversación actual | RAM | Siempre; se pierde al cerrar |
| Contactos | Misma base de datos | Solo si pides «guarda a…» y confirmas |
| Recordatorios | Misma base de datos | Al pedirlos (máx. 100); cada uno se borra al avisar |

- **Nada de secretos:** se rechaza lo que parezca contraseña, clave, token o PIN, antes incluso de preguntarte.
- **Los recuerdos son datos, no órdenes:** van al modelo en un bloque marcado como tal; un recuerdo
  tipo «ignora tus normas» no cambia su comportamiento.
- **Caché:** el bloque de memoria solo cambia cuando guardas u olvidas algo, así que la caché de Ollama sigue sirviendo.
- **Protección del fichero:** las herramientas de ficheros no pueden leer ni escribir `~/.local/share/arthur`;
  la memoria solo se toca con sus herramientas (`remember`, `list_memories`, `forget`).
- **Comandos directos** (`/memoria`, `/olvidar N`, `/borrar-historial`): los escribes tú, no pasan por el modelo.

## Atajo global (Super+Shift+A)

```text
Hyprland (binds.lua) ─► scripts/arthur-ptt ─► socket $XDG_RUNTIME_DIR/arthur/control.sock ─► VoiceController.toggle()
```

- **Buzón privado:** carpeta 700, socket 600 y comprobación del usuario que se conecta (`SO_PEERCRED`).
  Solo entiende la orden fija `toggle-listen`: el atajo no sirve para mandarle texto a ARTHUR.
- **Un solo ARTHUR:** si ya hay uno escuchando el atajo, el segundo no lo intenta; los sockets
  abandonados (de un cierre brusco) se sustituyen.
- **Mismo controlador** para Enter/Enter y para el atajo. Pulsar mientras habla corta la voz y escucha;
  mientras piensa, avisa de que está ocupado.
- **Lo pedido por atajo no confirma acciones** (no estás mirando la terminal): se rechazan con un aviso.
  Funcionan consultas, apps de confianza, volumen y notificaciones.
- **Configuración de Hyprland:** una línea en `binds.lua` de los dotfiles (copia `.bak`, CHANGELOG y chuleta).

## Confirmaciones desde el Orb

**Se confirma donde estás:** si lo escribiste en la terminal, ahí; si lo pediste con el atajo, en el Orb
(`RoutingConfirmer`). Sin Orb abierto, o sin respuesta en 60 s, la respuesta es NO.

```text
router ─► RoutingConfirmer ─(hilo del atajo)─► ConfirmationBroker ─ evento confirmation_pending ─► Orb (Sí / No)
                                                      ▲                                               │
                                                      └──── POST /api/confirm {id, answer} ◄──────────┘
```

Lo único que el Orb puede enviar es la respuesta a una pregunta abierta, identificada por un número
aleatorio de 128 bits. La ruta `/api/confirm` exige, por capas:
cookie HttpOnly+SameSite=Strict (nunca el token en la URL) · `Origin` del propio Orb · cabecera `X-Arthur: 1`
(otra web necesitaría un permiso CORS que nunca se da) · `Host` correcto · `Content-Type: application/json` ·
como mucho 1 KB · `answer` booleano. Una pregunta ya contestada no se puede cambiar.
El botón seleccionado por defecto es «No»; el texto se muestra como texto plano.
El resumen de cada confirmación pasa por `visible_text` (`security/confirm.py`): lo que no se dibuja (retorno de
carro, secuencias de escape, inversión de texto, ancho cero) se muestra escrito, porque el resumen incluye texto
del modelo y, en una terminal, podría pisar la pregunta y enseñar otra acción distinta de la que se va a ejecutar.

## Mensajes y avisos

- **Preparar, no enviar.** `compose_email` abre `mail.google.com/mail/?view=cm&to=…&su=…&body=…` y
  `compose_whatsapp` abre `web.whatsapp.com/send?phone=…&text=…`, ambos en Firefox con tu sesión. ARTHUR no
  tiene credenciales y no puede enviar nada: el botón lo pulsas tú. Por eso son de bajo riesgo.
- **Responder correos** (`reply_email`, riesgo bajo como `compose_email`): recibe el id de un correo (de `gmail_inbox`)
  y el texto. Con el permiso de solo lectura que ya existe pide a Gmail **solo las cabeceras De y Asunto** (nunca el
  texto del correo: no llega al modelo ni a la respuesta) y abre la ventana de redactar con el remitente original,
  «Re: asunto» y el texto. Se descarta el `Reply-To` (lo puede poner cualquiera). Seguridad: el modelo no puede elegir
  destinatario (no existen `to`/`cc`/`bcc` en la herramienta); el remitente debe ser UNA dirección que pase la
  expresión `EMAIL` y aparezca literal en la cabecera (el analizador de Python «arregla» algunas rarezas); el
  asunto, que escribe un tercero, se pasa a una línea sin caracteres de control y se corta a 200. La URL solo lleva
  `view`, `fs`, `to`, `su` y `body`. No se usa `gmail.compose`: ese permiso también permitiría ENVIAR.
  **Limitación conocida:** la URL de redactar no puede llevar los datos que Gmail usa para enlazar un correo con su
  conversación (`In-Reply-To`/`References`, o el identificador del hilo, que no está documentado); se abre como correo
  nuevo con el asunto «Re: …» y Gmail puede o no juntarlo. Hacerlo bien exigiría crear un borrador por la API
  (permiso `gmail.compose`, que también envía): descartado por «ARTHUR prepara, tú envías».
- **WhatsApp no tiene API oficial para cuentas personales.** Las librerías no oficiales incumplen sus normas
  (riesgo de baneo) y dan a código de terceros acceso total a los chats: no se usan.
- **Contactos** en la base de datos de la memoria; teléfonos normalizados a formato internacional
  (`612 34 56 78` → `34612345678`). Guardar o borrar pide confirmación.
- **Avisos:** `dbus-monitor` (paquete oficial `dbus`) observa las llamadas `Notify` al servicio de
  notificaciones. Se guardan las últimas 50 **solo en RAM**. Por defecto ARTHUR dice quién escribe, no qué;
  leer el texto (`recent_messages` con `include_text`) es PRIVADO y pide confirmación. Como mucho un aviso por
  persona y minuto, y nunca interrumpe a ARTHUR si está ocupado.
- **Firefox no dice de qué web viene un aviso** (comprobado con un diagnóstico que nunca ve título ni texto:
  solo manda `desktop-entry=firefox`). Por eso los avisos de navegadores se clasifican como «web» y ARTHUR dice
  «Tienes un mensaje de Laura», sin afirmar que sea de WhatsApp. Diagnóstico: orden `notifications` del buzón.

## Google: calendario y Gmail

- OAuth 2.0 de escritorio con la librería estándar (`integrations/google.py`): navegador + servidor local de un
  solo uso en 127.0.0.1, PKCE y `state`. El *refresh token* se guarda cifrado en gnome-keyring (`secret-tool`).
- Permisos: `calendar.readonly`, `calendar.events` (crear, cambiar y borrar eventos) y `gmail.readonly`. Gmail: nada de enviar,
  borrar ni modificar.
- **Crear eventos** (`create_event`, riesgo WRITE → confirmación con resumen exacto): solo crea, en el calendario
  principal, con `sendUpdates=none` y solo los campos título, lugar y fechas (`CalendarClient.create_event` rechaza
  cualquier otro, p. ej. `attendees`). Google no tiene un permiso «solo crear», así que la limitación está en el
  código. La herramienta valida con fechas exactas AAAA-MM-DD (fecha real, no pasada, ≤ 2 años, hora de Madrid) porque
  con el modelo pequeño es mejor que compruebe la herramienta; el resumen muestra el día de la semana para
  que se note un error de cálculo. Si el token no trae el permiso (Google dice qué permisos tiene en cada
  renovación) o la API responde `insufficientPermissions`, el aviso es «ejecuta `arthur --conectar-google`» y no se
  reintenta nada. Un fallo de red al enviar nunca se repite solo: avisa de que quizá se creó.
- **Cambiar eventos** (`update_event`, riesgo WRITE → confirmación). Mismo diseño que crear, más:
  - **El evento se identifica por un NÚMERO** (`#1`, `#2`… en `calendar_events`, solo en los editables), nunca por texto
    del modelo. Qué pasaba de verdad (audit.jsonl): `qwen3:4b` llamó a `update_event` con `id="1"` (inventado), y
    `create_event` no devolvía ningún id, así que no podía referirse a lo recién creado. Como con `install_package`,
    lo fiable es que la herramienta compruebe y traduzca, no que el modelo deduzca. La herramienta guarda la tabla
    **número → id de Google** de la ÚLTIMA lista mostrada (cada `calendar_events` la renueva, también si no hay
    eventos o falla); `create_event` añade el evento nuevo a la tabla y devuelve `(#N)`; `delete_event` quita el número.
    `update_event`/`delete_event` aceptan `#N` o `N` (1–4 cifras) y también un id real de Google (`^[a-v0-9_]{5,100}$`;
    mínimo 5 caracteres, así que no se solapa con los números). Número que no está (o tabla vacía): «Llama primero a
    calendar_events y usa el número #N de la lista». La tabla vive en la herramienta (no es global: dos conjuntos de
    herramientas no se pisan) y está protegida con un `Lock`. **Lo resuelto se fija al comprobar** (`pinned`): si otra
    llamada a `calendar_events` renovara la tabla mientras decides, lo que confirmas sigue siendo el mismo evento.
    La confirmación nunca enseña solo el número: título, fecha y día de la semana. `check` lo consulta en Google
    **antes** de preguntar: si no existe, error sin molestarte.
  - **Qué se toca:** solo el calendario `primary`, eventos sueltos (no `recurringEventId`), sin otros invitados, de tipo
    `default` y de un solo día. Lo demás se rechaza con el motivo. Así `sendUpdates=none` es de verdad «nadie se entera».
  - **Antes y después:** la confirmación enseña el evento actual y cómo quedaría (título, fecha, día de la semana, horas).
  - **Lo que confirmas es lo que se cambia:** el evento que enseña la confirmación se guarda (`seen`) y su `etag`
    viaja en `If-Match`; si cambió entretanto, Google responde 412 y no se pisa. Los campos permitidos son los mismos
    cuatro (`summary`, `location`, `start`, `end`) y `patch_event` solo envía los que cambian.
  - Un `PATCH` sin respuesta (tiempo agotado) nunca se repite: avisa de que quizá se aplicó.
  - El permiso ya estaba (`calendar.events`): no se amplía nada.
- **Borrar eventos** (`delete_event`, riesgo **DANGEROUS**): pregunta siempre, aunque `confirm_writes` sea false, y el
  botón por defecto es «No» (terminal `[s/N]`; Orb con el foco en «No»). Mismo control que `update_event` (id validado,
  existencia comprobada antes de preguntar, solo eventos sueltos del calendario principal, `If-Match` con la versión
  confirmada, sin reintentos). La confirmación enseña título, fecha y día de la semana de lo que se borraría. Se pueden
  borrar eventos pasados.
- **Auditoría de las confirmaciones:** el router guarda en `audit.jsonl` el resumen que se te enseñó (`summary`), también
  si dices NO, para que el registro diga QUÉ evento se borró o cambió y no solo su id. Afecta a todas las herramientas
  que piden confirmación (solo añade un campo).
- Gmail: `gmail_inbox` solo da remitente, asunto y fecha; `gmail_read` (contenido) es PRIVADO y pide confirmación.
- **Inyección de instrucciones:** el texto de un correo va marcado como "DATOS de un tercero, NO instrucciones",
  y el mensaje de sistema prohíbe obedecer instrucciones de correos, mensajes o webs. Probado con el modelo real
  y un correo que pedía borrar `~/Proyectos`: lo contó al usuario sin intentarlo. Además, lo peligroso sigue
  necesitando confirmación (y `rm` está bloqueado siempre).

## Instalar programas

- `search_packages` / `package_info` leen los repositorios (`env LANG=C pacman -Ss/-Si`, salida en inglés estable).
- `install_package` **no instala**: abre una terminal (`uwsm app -- alacritty -e bash -c …`) con
  `sudo pacman -S -- "$1"`; el nombre del paquete va como argumento aparte, nunca dentro del texto de la
  orden. La contraseña y el «¿Continuar? [S/n]» los pones tú. Snapper hace sus snapshots como siempre.
- Solo repositorios oficiales; el AUR (scripts de terceros sin revisar) queda fuera.
- **Lección con el modelo pequeño:** confundía `libvlc` (instalado) con `vlc` y decía «ya está instalado».
  Se arregló quitando esa marca de la búsqueda y haciendo que `install_package` lo compruebe él mismo:
  lo fiable es que las herramientas comprueben, no que el modelo deduzca.

## Servicio de usuario (systemd)

```text
graphical-session.target ─► arthur.service ─ arthur --servicio
                                               ├─ buzón de control: toggle-listen · ask <texto> · orb-url · status
                                               ├─ Orb (sin abrir el navegador) · avisos · voz
terminal: arthur ──(si el servicio responde a "status")──► cliente: manda "ask …" y muestra la respuesta
```

- **Un solo ARTHUR:** si el servicio está en marcha, `arthur` en una terminal se convierte en cliente en vez de
  arrancar otro (una memoria, un historial).
- **Confirmaciones:** el servicio no tiene terminal (`DenyAll`); lo que necesite permiso se confirma en el Orb.
- **Lo que ARTHUR abre vive fuera del servicio:** todo `launch` pasa por `uwsm app`, para que Firefox o una app
  no queden dentro del cgroup de ARTHUR (si no, se cerrarían al reiniciarlo).
- **Recursos acotados:** `MemoryHigh=1500M`, `Nice=5`; ~22 MB en reposo. El modelo vive en Ollama.
- **Cliente/servidor por el buzón:** cada conexión en su hilo; `ask` lleva texto, solo para tu usuario
  (socket 600 + `SO_PEERCRED`).

## Palabra de activación («Hey Arthur»)

- **Vosk** (`python-vosk`, repositorios oficiales, Apache-2.0) con el modelo pequeño de inglés (39 MB, md5
  verificado), porque el de español no tiene «arthur». Gramática limitada a la frase: lo demás es `[unk]`,
  no se transcriben conversaciones ni se guarda audio. Coste medido: ~1 % de un núcleo, ~170 MB.
- **No se escucha a sí mismo:** solo detecta con ARTHUR en reposo (IDLE) y sin grabar.
- **Manos libres:** tras la frase, ding y grabación hasta 1,2 s de silencio (`Recorder.wait_for_end`);
  si no dices nada en 5 s, se cancela.
- **Calibración con tu voz** (`--calibrar-activacion`): guarda solo el texto que entiende el modelo.
  Resultado real: 4/5 con la frase exacta; la variante «arthur» cubre el 5.º. Sin falsos positivos con frases
  normales en español. Con la voz sintética de Piper funcionaba peor (oía «have to»): calibrar con la voz real importa.
- Pausa por voz (`set_wake_word`); el interruptor físico es silenciar el micrófono.

## Recordatorios

- `set_reminder` (`minutes` = dentro de X minutos, o `at` HH:MM + `date` AAAA-MM-DD), `list_reminders` y
  `cancel_reminder`: riesgo bajo, sin confirmación. «A las 8» dicho por la noche es mañana a las 8.
- Se guardan en la tabla `reminders` de la memoria SQLite (máximo 100, hasta 366 días, texto de 200 caracteres como
  máximo y sin secretos), así que sobreviven a reinicios.
- Solo en modo servicio: `ReminderScheduler` mira cada segundo, saca los vencidos de la base de datos (**se borran al
  sacarlos: se avisa una sola vez**) y avisa con un ding, una notificación de escritorio (15 s) y la voz, esta última
  cuando ARTHUR esté libre. Los que vencieron con el portátil apagado se avisan al arrancar como «atrasado».

## Música

- `media` (riesgo bajo): `play`, `pause`, `toggle`, `next`, `previous`, `volume_up`/`volume_down` (volumen del reproductor,
  pasos de 0,1), `forward`/`back` (±10 s) y `status` (qué suena) con `playerctl` (MPRIS), Spotify primero
  (`--player=spotify,%any`). Las órdenes son fijas (nunca texto del modelo).
- `search_music` (riesgo bajo): abre la búsqueda en Spotify (`spotify:search:…`, vía `spotify-launcher`) o en YouTube
  (Firefox) con `xdg-open`. La dirección es fija y el texto va siempre codificado. No reproduce nada: eliges tú.
- Lo que devuelve `status` (artista y título) viene del reproductor: si suena un vídeo de la web, lo escribe un tercero.
- Solo en modo servicio, `MusicDucker` pausa la música cuando ARTHUR sale de reposo (te escucha o te habla) si estaba
  sonando, y la reanuda al volver a reposo. Lo que pides durante la conversación («siguiente») se aplica al terminar,
  para no hablar por encima de la música.

## El Orb al decir «Hey Arthur»

- `integrations/orb_window.py` busca la ventana del Orb (clase `firefox` y título «ARTHUR»). Si no existe, abre una
  ventana nueva de Firefox con la dirección del Orb (solo si empieza por `http://127.0.0.1:`); la trae a tu escritorio,
  le da el foco y la pone a pantalla completa.
- Al terminar (8 s después de la respuesta, si nadie ha vuelto a llamar) la manda al escritorio oculto `special:arthur`
  y devuelve el foco a tu ventana anterior.
- Solo usa las plantillas fijas de `HyprlandController` (`set_fullscreen`, `hide_window`, `focus_window`,
  `move_window`), con direcciones de ventana validadas. Limitación: cualquier ventana de Firefox cuyo título sea
  «ARTHUR» se toma por el Orb (solo cambiaría de sitio una ventana; no ejecuta nada).
- Se desactiva con `[orb] fullscreen_on_wake = false`.

## Registro y auditoría

| Qué | Dónde | Permisos | Qué contiene |
|---|---|---|---|
| Auditoría | `~/.local/state/arthur/audit.jsonl` | 600 | Cada herramienta que se intenta: argumentos (recortados a 200 caracteres, secretos con forma conocida tapados), riesgo, decisión y resultado |
| Registro | `~/.local/state/arthur/arthur.log` (+3 copias de 1 MB) | 600 | Qué ocurre: herramientas usadas, estados, errores. No escribe lo que dices ni lo que ARTHUR responde, salvo con `level = "DEBUG"` (un mensaje de error sí puede citar una ruta o un nombre) |
| Journal (servicio) | `journalctl --user -u arthur` | — | Lo mismo que el registro, más las líneas de estado y el resumen de las confirmaciones pendientes («Esperando tu confirmación en el Orb: …») |

- La auditoría se escribe al terminar cada herramienta y no puede borrarse con las herramientas de ficheros
  (`~/.local/state/arthur` está protegido).
- Los argumentos de herramientas que llevan texto (`body`, `text`, `content`…) quedan en la auditoría hasta 200
  caracteres: el texto de lo que ARTHUR prepara o recuerda es legible por tu usuario en ese fichero.

## Velocidad (medido en un portátil de pruebas, solo CPU)

- **El coste no es responder, es leer las instrucciones:** mensaje de sistema + 30 herramientas ≈ 4.400 tokens,
  que la CPU procesa a ~43 tokens/s (≈ 100 s). La respuesta corta en sí son ~2 s.
- **Caché de Ollama:** si el principio de la petición es idéntico, no se reprocesa. Por eso: mensaje de sistema
  fijo, y **precalentamiento** al arrancar el servicio y al empezar a escucharte (`warm_up`): una petición con
  las mismas instrucciones y el mismo `num_ctx` (si no coincide, Ollama recarga el modelo).
- **`keep_alive = "3h"`**: el modelo (≈ 3,9 GB) y su caché siguen en RAM entre preguntas.
- Resultado: «¿qué hora es?» de 104 s a 3,5 s; «¿qué tengo mañana?» ≈ 26 s (herramienta + lista larga).
- **Gráfica integrada (Vulkan):** `ollama-vulkan` (repositorio oficial) + `OLLAMA_IGPU_ENABLE=1` en
  `/etc/systemd/system/ollama.service.d/igpu.conf`. Modelo entero en la Radeon (37/37 capas, memoria compartida).
  Medido: entrada 54 → 105 tokens/s, respuesta 10 → 13 tokens/s; «¿qué hora es?» 2,7 s, «¿qué tengo mañana?» 19 s,
  precalentamiento en frío 56 s. Además libera la CPU mientras piensa.
- **Las cifras de abajo se midieron con 32 herramientas; hoy hay 38** (con memoria, avisos, Google y la palabra de
  activación activados). Cada herramienta nueva alarga el principio de la petición y el precalentamiento: conviene
  volver a medir antes de añadir más.
- **Instrucciones compactas:** mensaje de sistema de 2.796 a 1.107 caracteres y herramientas de 35 (13.789 car.) a
  32 (8.650 car.): se juntaron las que se solapaban (`list_windows` → `list_workspaces`; `get_volume`+`set_volume`
  → `volume`; fuera `package_info`) y se quitó envoltura del esquema. Precalentamiento en frío: 56 → 24 s.
  Verificado con una batería de peticiones reales; dos ajustes tras ella: la búsqueda de paquetes indica la
  coincidencia exacta, y la regla de redactar mensajes lleva un ejemplo.
- **Fin de frase adaptativo:** el umbral de voz se calcula con el ruido de fondo de los primeros 300 ms;
  con el umbral fijo, el ruido de la habitación hacía grabar siempre hasta el máximo (20 s).

## Ideas clave

1. **El núcleo no conoce la interfaz.** `Assistant` recibe texto y devuelve texto.
   Todo lo demás se entera por el bus de eventos. La voz y el Orb se conectarán así,
   sin tocar el núcleo.
2. **Cerebros intercambiables.** Todos cumplen `Brain.reply()` (ver `brain/base.py`).
   Para pasar del cerebro de reglas a un LLM basta con cambiar `brain.provider`.
3. **Máquina de estados estricta.** Solo se permiten ciertas transiciones
   (`core/state.py`). Si el cerebro falla: THINKING → ERROR → IDLE, y ARTHUR sigue vivo.
4. **Un suscriptor roto no tumba el sistema.** Si el Orb falla al recibir un evento,
   el error se registra y el núcleo continúa.
5. **Configuración estricta.** Una clave desconocida o un tipo incorrecto es un error,
   no se ignora en silencio.

## Eventos actuales

| Evento | Datos | Cuándo | ¿Llega al Orb? |
|---|---|---|---|
| `state_changed` | `from`, `to` | En cada cambio de estado | Sí |
| `user_message` | `text` | Al recibir una entrada | Sí |
| `assistant_message` | `text` (y `announcement` si es un aviso espontáneo) | Al responder o avisar | Sí |
| `error` | `message` | Si el cerebro falla | Sí |
| `confirmation_requested` | `tool`, `summary` | Antes de preguntarte | Sí |
| `tool_started` | `call_id`, `tool` | Al empezar a ejecutar | Sí |
| `tool_finished` | `call_id`, `tool`, `status` | Al terminar (ok/denied/cancelled/error) | Sí |
| `confirmation_pending` | `id`, `tool`, `risk`, `summary`, `timeout` | Una pregunta abierta en el Orb | Sí |
| `confirmation_resolved` | `id`, `answer`, `by` (`orb` o `timeout`) | Se contestó o caducó | Sí |
| `audio_level` | `level`, `source` (`mic` o `tts`) | Mientras suena o te escucha | Sí |
| `message_received` | `source`, `from` | Llega una notificación de mensaje | No |

## Decisiones

- **bwrap para el sandbox.** Ya estaba instalado (lo usa Flatpak), funciona sin root y es
  de los mismos autores que Flatpak (proyecto `containers`, LGPL-2.1+). Si falta, `run_command`
  se niega a ejecutar: nunca se ejecuta sin aislamiento.
- **Sin dependencias en la Fase 2.** Usamos `tomllib` en lugar de PyYAML y `unittest`
  en lugar de pytest. El venv se creará cuando llegue la primera dependencia real.
- **TOML en lugar de YAML.** Viene incluido en Python, es más simple y no tiene
  las ambigüedades de YAML (por ejemplo, `no` convertido en `False`).
- **Paquete en la raíz (`arthur/`) en lugar de `src/arthur/`.** Así `python -m arthur`
  funciona sin instalar nada. Lo revisaremos si algún día lo empaquetamos.
- **Historial solo en RAM.** La memoria persistente llegará en su fase, con su política.
