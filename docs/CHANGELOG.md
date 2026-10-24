# CHANGELOG

## 1.3.0 · Segundo cerebro, panel y chat para la web

- **Segundo cerebro:** busca en notas de Obsidian, apuntes y PDF, cita las fuentes y permite una nota diaria
  opcional. El acceso y los límites de uso se configuran por separado.
- **Apuntes y Repaso:** preguntas de estudio, sesiones, repaso espaciado, puntos y racha; panel de revisión diaria y
  semanal.
- **Panel y Clientes:** vista del día, avisos y registro de contactos con control de duplicados y borrado.
- **Creator Studio:** cuentas y canales separados, perfil de Frutinovelas, importación de Shorts y validaciones
  para preparar y revisar los vídeos.
- **Chat para webs:** responde desde un fichero de conocimiento, permite dejar un contacto y lo entrega a Clientes.
  Puede usar Workers AI o una IA local de respaldo.
- Plantillas de Docker Compose, documentación y pruebas para estos servicios. Las funciones externas necesitan sus
  propias claves y configuración; no se activan al instalar el repositorio.

## 1.2.0 · Servidor en Windows (en pruebas)

- Instalador de servidor para Windows 10/11 con Docker Desktop y documentación de sus límites. El aparato Windows
  sigue usando la web de ARTHUR.

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
