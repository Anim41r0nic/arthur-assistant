# CHANGELOG

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
- Nota: la rama `claude/aparato-windows` (aparato Windows, fase A) se numerará 1.2.0 al fusionarla.

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
