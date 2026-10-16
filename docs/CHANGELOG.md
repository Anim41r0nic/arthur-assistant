# CHANGELOG

## 1.2.0 · El PC con Windows como aparato (fase A, en pruebas)

Hasta ahora, desde Windows solo se podía usar la web de chat: el aparato «de verdad» (`arthur --servicio-cliente`,
con micro, altavoz, Orb, confirmaciones y herramientas) dependía de cosas de Linux: pw-record y pw-play para el
audio, notify-send para las confirmaciones, un socket Unix para el atajo y systemd para arrancarlo. Ahora hay una
pieza de Windows para cada una, **solo con la biblioteca estándar** (nada de pip):

- **Micrófono:** winmm (waveIn) por ctypes, 16 kHz mono 16 bits (`voice/win_audio.py`). `Recorder` ya no da por
  hecho pw-record: lee de una «fuente» (el programa de siempre en Linux, waveIn en Windows), así la detección de
  voz y silencio, el nivel para el Orb y el ruido de fondo son EL MISMO código en los dos.
- **Altavoz:** winsound con el WAV en memoria (nunca en disco), en su propio hilo para poder cortarlo («calla») y
  con el nivel calculado al ritmo real para que el Orb lata (`WinSoundPlayer`, la misma interfaz que `WavPlayer`).
- **Confirmaciones sin Orb:** una notificación de Windows con botones Sí/No (PowerShell + Windows.UI.Notifications).
  El script es una plantilla FIJA; el texto le llega por la entrada estándar en base64 y entra con CreateTextNode,
  que lo escapa: un resumen con comillas, XML u órdenes no puede cambiar el script. Cualquier fallo o no
  contestar cuenta como NO. Con el Orb abierto se pregunta en el Orb, como en Linux.
- **Buzón del atajo:** Windows no tiene sockets Unix ni SO_PEERCRED. Allí es 127.0.0.1 en un puerto libre, con una
  llave aleatoria nueva en cada arranque guardada en `%LOCALAPPDATA%\arthur\control.key` (solo tu usuario puede
  leerla); sin la llave no se atiende nada. La ruta decide qué buzón se usa, así que la variante de Windows se
  prueba entera en Linux.
- **Herramientas del aparato Windows** (perfil `windows`): abrir apps del menú Inicio (lista cerrada de accesos
  `.lnk`, sin desinstaladores ni herramientas de administración; el modelo elige un nombre, nunca una ruta), volumen
  y música con teclas multimedia (SendInput). Mismos nombres que en Linux (`open_application`, `volume`, `media`)
  para que el servidor las use igual. Nada PELIGROSO. Como Windows no deja leer el volumen sin COM, `volume` lo dice
  en vez de inventarlo, y el porcentaje se pone bajando a 0 y subiendo a pasos del 2 %.
- **Arranque:** `scripts\windows\instalar-aparato.ps1` (menús con la recomendada en verde, PowerShell 5.1, solo
  ASCII): comprueba Python 3.14, empareja con el servidor, crea una tarea programada «al iniciar sesión» de TU
  usuario sin administrador (si Windows no la deja, un acceso en la carpeta Inicio) y el atajo **Ctrl+Alt+A**
  (`scripts/windows/arthur-hablar.pyw`). `-Quitar` lo deshace.
- En Windows, la configuración va a `%APPDATA%\arthur` y el registro a `%LOCALAPPDATA%\arthur`; con pythonw (sin
  consola) el registro solo va al fichero.
- `scripts/instalar-servidor.ps1`: los dos `catch { }` vacíos (lo que señala PSScriptAnalyzer) ahora lo apuntan con
  `Write-Verbose`.

- **Arreglos de la revisión antes de fusionar:**
  - Una app de confianza ya no puede abrir OTRA app sin preguntar: el servidor se salta la confirmación si el nombre
    pedido está tal cual en `trusted_apps`, y la búsqueda del menú Inicio acepta trozos de nombre («chrome» →
    «google-chrome»). Ahora un nombre de confianza solo abre la app con ese nombre exacto; si no, se deniega.
  - La lista de apps mira TODAS las carpetas del acceso, no solo la última (una subcarpeta de «Administrative
    Tools» seguía apareciendo), y deja fuera la carpeta Inicio (Startup): lo que arranca solo, incluido el propio
    servicio de ARTHUR, no se abre por voz.

Aún no en Windows (fase B): «Hey Arthur», el Orb a pantalla completa, ventanas, avisos de WhatsApp y gestos.
Todo lo anterior está probado con falsos (en la nube no hay Windows): falta probarlo en un PC real.

## 1.1.0 · Creator Studio y Apuntes

- **Creator Studio** (`arthur/studio`, [docs/studio.md](studio.md)): investiga nichos de YouTube con la Data API y
  produce vídeos solos: guion con Gemini (capa gratis) y un revisor que marca las dudas, imágenes de Wikimedia
  Commons con su licencia comprobada, voz Kokoro, montaje con ffmpeg, vídeo largo + Short vertical, página de
  revisión con llave por aparato y subida a YouTube por OAuth. Piloto automático diario: lo que sale sin dudas del
  revisor se publica solo a su hora; con dudas espera a que escribas PUBLICAR. Avisos por ARTHUR.
- **Apuntes** (`arthur/notes`, [docs/apuntes.md](apuntes.md)): fotos o PDF de apuntes → texto con fórmulas en
  LaTeX (Gemini, qwen3-vl local o el texto del PDF) → PDF limpio con Typst o escaneo con OCRmyPDF; carpetas por
  asignatura e importar una carpeta entera. «Preguntar a la IA»: explicar algo o inventar ejercicios como los de
  tus exámenes a partir de los apuntes que marques, y guardar la respuesta como apunte.
- **Los tres comunicados:** desde el chat de ARTHUR se pide «prepara un vídeo», se mira cómo va Studio, se crean
  carpetas en Apuntes o se guarda una foto en una carpeta (`arthur/tools/services.py`). Publicar desde el chat solo
  cuando el revisor no tiene dudas. `POST /api/v1/notice` para que Studio avise por ARTHUR (solo con su llave).
- Gemini: se aceptan las claves con el formato nuevo de Google; si un modelo está saturado o se cuelga se pasa al
  siguiente y se aparta un rato; nunca se usan modelos «Pro» (de pago).

## 1.0.0 · Primera versión pública

ARTHUR, un asistente personal propio y autoalojado. Lo que trae:

- **Servidor + aparatos:** el cerebro en tu servidor (Docker: `core`, `ollama`, `voz`, `push`), cada aparato pone
  micrófono, altavoz, pantalla, cámara y sus herramientas. Instaladores con menús (`scripts/instalar-servidor.sh`,
  `scripts/instalar-aparato.sh`) y la opción recomendada en verde.
- **IA local** con Ollama (por defecto `qwen3-vl:4b-instruct`, que también ve fotos), con caché del prefijo,
  precarga y, si hay, la gráfica integrada por Vulkan.
- **Voz:** «Hey Arthur» (Vosk), doble palmada y atajo; oír con whisper.cpp y hablar con Piper en el servidor; la
  respuesta se dice a frases según se escribe.
- **Web de chat** (móvil, Windows, cualquier navegador): texto, voz, fotos, historial, instalable como app, avisos
  al móvil con Web Push.
- **El Orb** (Three.js): estados, conversación, actividad y confirmaciones Sí/No; a pantalla completa con «Hey Arthur».
- **Herramientas** con router único (validar → comprobar → política → confirmar → ejecutar → auditar), sandbox
  bubblewrap y regla de honestidad: escritorio (apps, ventanas de Hyprland, volumen, música, portapapeles),
  ficheros, memoria, contactos, recordatorios, Google Calendar y Gmail, mensajes «ARTHUR prepara, tú envías»,
  instalar programas.
- **Herramientas remotas** de cada aparato con doble control y riesgo que depende de los argumentos.
- **Gestos con la cámara** (OpenCV + MediaPipe en una burbuja sin red): ✋ 👎 ✊ 👍 👌 ☝️ ✌️ 👋.
- 778 tests, todos con programas y servidores falsos.

Las referencias a versiones 0.x en comentarios del código son del desarrollo anterior a la publicación.
