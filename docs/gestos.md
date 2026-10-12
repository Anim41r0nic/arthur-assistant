# Gestos con la cámara

ARTHUR entiende unos pocos gestos de la mano delante de la cámara de tu ordenador (Linux, con el aparato instalado:
`scripts/instalar-aparato.sh`). La imagen **nunca sale del ordenador**: al servidor solo llega, como mucho, la
respuesta Sí/No de una confirmación.

## Qué hace cada gesto

| Gesto | Acción (tabla fija en `arthur/interfaces/gestos/actions.py`) | Límites |
|---|---|---|
| ✋ palma | calla: corta la voz de ARTHUR y pausa la música | — |
| 👎 pulgar abajo, ✊ puño | «No» a la confirmación abierta en el Orb | sin pregunta abierta, nada |
| 👍 pulgar arriba, 👌 círculo | «Sí» | mantenerlo 1,5 s; UNA sola confirmación abierta y de riesgo bajo o de escritura; nunca lo privado ni lo peligroso (eso, solo con el clic) |
| ☝️ índice arriba | repite la última respuesta (sin volver a pensar) | si ARTHUR está ocupado, la repite al quedar libre |
| ✌️ dos dedos | pausa / sigue la música | — |
| 👋 saludo | ARTHUR te saluda (un texto fijo según la hora) | solo si está libre; saluda despacio (~1 ida y vuelta por segundo) |

Cada gesto reconocido sale en la lista de actividad del Orb, con lo que hizo o por qué no («👋 hola · ARTHUR está
ocupado: no saluda»). La regla de partida: **un gesto nunca hace nada peligroso**. La cámara ve cosas que no son
órdenes (un saludo en una videollamada, un pulgar arriba en un vídeo), así que cada gesto hace algo pequeño y
reversible, y hay que mantenerlo (1 s, o 1,5 s para el «Sí»).

## Privacidad

- **La cámara empieza APAGADA** en cada sesión. Solo la enciendes tú: el botón del Orb, un atajo de teclado
  (`scripts/arthur-camara`) o pidiéndoselo a ARTHUR (eso pide confirmación siempre). Con
  `[gestures] enabled = false` no se puede encender de ninguna forma.
- Se **apaga sola** tras 5 minutos sin gestos (`idle_minutes`), al bloquear la pantalla y al parar ARTHUR.
- Mientras está encendida: punto rojo en el Orb, notificación al encender y al apagar, y una línea en la
  auditoría (`audit.jsonl`).
- La cámara la lee un **proceso aparte dentro de una burbuja** (`bwrap`): sin red, con la carpeta personal vacía
  salvo los modelos, y solo puede devolver nombres de gestos de una **lista blanca** (`names.py`, `protocol.py`).
  Ninguna imagen se guarda ni sale de esa burbuja. Si ARTHUR se para, la burbuja muere con él.
- Nada de lo que ve la cámara llega al modelo de IA: los gestos no pasan por el prompt.

## Instalar

1. **OpenCV** de los repositorios de tu sistema (Arch: `sudo pacman -S python-opencv bubblewrap`; Debian/Ubuntu:
   `sudo apt install python3-opencv bubblewrap`).
2. **Los dos modelos de manos** (MediaPipe Hands convertidos por OpenCV zoo, Apache-2.0; ver
   [gestos-modelos.md](gestos-modelos.md)). ARTHUR comprueba su huella sha256 antes de usarlos:

   ```bash
   mkdir -p ~/.local/share/arthur/gestos && cd ~/.local/share/arthur/gestos
   Z=https://github.com/opencv/opencv_zoo/raw/main/models
   curl -fLO $Z/palm_detection_mediapipe/palm_detection_mediapipe_2023feb.onnx
   curl -fLO $Z/handpose_estimation_mediapipe/handpose_estimation_mediapipe_2023feb.onnx
   sha256sum *.onnx
   # 78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c  palm_detection_mediapipe_2023feb.onnx
   # db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c  handpose_estimation_mediapipe_2023feb.onnx
   ```

3. En `~/.config/arthur/config.toml`: `[gestures] enabled = true` (el instalador del aparato lo pregunta) y
   reinicia: `systemctl --user restart arthur-cliente`.

Gasto medido: en reposo ~3 fotogramas/s hasta que aparece una mano (unos pocos % de un núcleo); con la mano
delante, 6 fotogramas/s.

## Si no te reconoce bien

Los umbrales (`arthur/interfaces/gestos/classify.py` y `motion.py`) se ajustaron con manos y cámaras reales, pero
cada mano y cada cámara son distintas:

- Pon la mano a unos 40-80 cm, con luz de frente y la palma (o el dorso) mirando a la cámara.
- Mantén el gesto quieto 1-2 s; el saludo, despacio (a ~5 fotogramas/s una mano rápida sale borrosa).
- Al apagar la cámara, el registro resume lo que vio en esa sesión: cuántas lecturas, y para cada gesto cuántas
  claras y la seguridad máxima (`journalctl --user -u arthur-cliente | grep "camera off"`). Si un gesto sale con
  seguridad alta pero pocas veces, mantenlo más quieto; si nunca pasa de ~0,7, sus umbrales no encajan con tu mano
  y se pueden ajustar en `classify.py` (cada umbral está comentado con lo que mide).
