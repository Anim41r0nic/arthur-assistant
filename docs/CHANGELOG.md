# CHANGELOG

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
