# Modelos de manos para los gestos

Dos modelos ONNX de MediaPipe Hands convertidos por el proyecto **OpenCV zoo** (`github.com/opencv/opencv_zoo`,
licencia Apache-2.0). Se ejecutan con `cv2.dnn` de `python-opencv` (repositorio oficial de Arch), sin red.

| Fichero | Para qué | Tamaño | sha256 |
|---|---|---|---|
| `palm_detection_mediapipe_2023feb.onnx` | dónde hay una palma en la imagen | 3.905.734 B | `78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c` |
| `handpose_estimation_mediapipe_2023feb.onnx` | los 21 puntos de esa mano | 4.099.621 B | `db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c` |

- Origen: `models/palm_detection_mediapipe/` y `models/handpose_estimation_mediapipe/` de `opencv/opencv_zoo`
  (rama `main`, versión «2023feb», sin cuantizar: la cuantizada a int8 «puede dar resultados inválidos» según su README).
- Los `sha256` y tamaños salen del puntero de Git LFS del propio repositorio y se comprobaron contra lo descargado
  el 2026-10-08 (coinciden). Esto garantiza que el fichero es el publicado, no que sea de fiar por sí mismo.
- Guardados en `~/.local/share/arthur/gestos/` (fuera del repo, como las voces y el modelo de Vosk). Nada se
  descarga solo: si se borran, se vuelven a bajar a mano y se comprueba el `sha256`.
- Medida de la cámara sola (paso 0, sin modelo): 640x480 a 8 fps = 11,4 % de un núcleo; a 3 fps = 4,4 %; 320x240 a
  3 fps = 3,7 %. Por eso el modo reposo va a ~3 fps hasta que aparezca una mano.
