# Panel de vida

## Qué es

Una página para el móvil (y el portátil) que te enseña **lo de hoy en un vistazo**:

- tus tareas, con una barra de progreso del día;
- las casillas de Obsidian que empiezan por `Yo:` en los ficheros elegidos (puedes marcarlas desde el panel);
- los eventos de hoy de Google Calendar y qué está pasando ahora y qué viene después;
- los correos importantes sin leer de Gmail;
- las tareas que deja Studio para publicar los cortos en Instagram y TikTok;
- «Cómo va hoy»: la revisión de la noche y de la semana, con puntos, nivel y racha (se puede ocultar). Todo en
  [`docs/revision.md`](revision.md).

Cada mañana, a una hora distinta según el día, ARTHUR escribe un **resumen de la mañana** y te avisa en el móvil.
Al tocar el aviso se abre el panel con el resumen, cuando el núcleo tiene configurada su dirección pública.
También puedes escribir tareas con fecha: «comprar pan mañana»,
«dentista el 25». Y desde el chat de ARTHUR: «¿qué tengo pendiente hoy?» o «apunta llamar a Ana».

Google se usa **solo para leer**. El panel no puede crear, enviar, borrar ni cambiar nada en tu calendario ni tu correo.

## 0. Ponerlo en marcha

El servicio `panel-web` está en `compose.yaml` (perfil `panel`). En el servidor, desde la carpeta de ARTHUR:

```
bash scripts/desplegar.sh panel
```

El script crea sin enseñarlas las llaves `panel_avisos` (el aviso del resumen) y `panel_core` (el chat de ARTHUR
pregunta al panel), genera la clave `panel_google_key` y conecta el panel con el núcleo. Pon también en `.env`
`ARTHUR_PANEL_PUBLIC_URL` (la dirección que abre tu móvil, p. ej. `http://100.64.0.10:8775`) y, si usas Obsidian,
`ARTHUR_BOVEDA` (la carpeta de tu bóveda en el servidor; ver el apartado 5).

## 1. Darle una llave al móvil

En el servidor, dentro del contenedor del panel:

```
docker compose exec panel-web python -m arthur --panel-dispositivo-nuevo movil
```

Te enseña una llave **una sola vez** (el servidor solo guarda su huella). Abre en el móvil la dirección del panel
(la de Tailscale, puerto 8775; si quieres, genera un QR con esa dirección y escanéalo), pega la llave y listo:
el móvil se acuerda de ella. Para otro aparato, repite con otro nombre.

## 2. Autorizar Google

1. Pon el fichero de cliente de Google en el volumen del panel (el mismo tipo de fichero de la guía `docs/google.md`:
   «Aplicación de escritorio») como `/data/config/arthur/google-client.json`.
2. Ejecuta:

```
docker compose exec panel-web python -m arthur --panel-google
```

3. Abre la dirección que te enseña en el navegador del móvil o del ordenador y acepta.
4. Al final el navegador irá a una página de `127.0.0.1` que **no carga**: es normal. Copia la dirección completa de
   la barra, pégala en la terminal y pulsa Enter.
5. Reinicia el panel (`docker compose restart panel-web`).

Hasta que lo hagas, las tarjetas de calendario y correo simplemente no salen. El permiso de Google se guarda
cifrado con AES-256-GCM en `google.enc` dentro del volumen del panel. La clave de cifrado vive **fuera** de ese
volumen, montada como secreto Docker en `/run/secrets/panel_google_key`. `scripts/desplegar.sh` genera una clave
aleatoria de 32 bytes (64 caracteres hexadecimales) en `secrets/panel_google_key`, con acceso solo para el usuario de
los contenedores. La imagen instala `cryptography` de PyCA (Apache-2.0/BSD-3) y sus dependencias
`cffi` y `pycparser`, con versiones y huellas fijas de `requirements-push.txt`; el núcleo sigue sin dependencias
Python nuevas. La biblioteca cifra y comprueba la integridad del permiso; no envía la clave a ningún servicio.
Si pierdes la clave, tendrás que autorizar Google de nuevo. No hagas copia de la clave junto al volumen cifrado.

## 3. Quién escribe el resumen (Workers AI y Ollama, sin Gemini)

El resumen ya **no usa Gemini**: sus condiciones solo dejan ofrecerlo en el Espacio Económico Europeo con un plan de
pago, y ARTHUR no usa APIs de pago. Lo escribe **Workers AI** de Cloudflare (la misma cuenta y el mismo token que el
chat de la web y el cerebro, dentro de su capa gratis) y, si no está configurado o falla, el modelo local (**Ollama**).
Si tampoco responde, sale una plantilla sencilla: nunca te quedas sin resumen.

Para usar Workers AI, pon tu id de cuenta en `ARTHUR_CF_ACCOUNT` (`.env`; no es secreto) y el token en
`secrets/workers_ai_token` (el mismo que el chat de la web, ver docs/webchat.md).

Sin cuenta, el resumen lo escribe Ollama. La orden antigua `--panel-clave-gemini` ya no pide
ni guarda ninguna clave; si te queda un fichero `gemini-key` en el volumen del panel, no se usa y puedes borrarlo.

## 4. Cambiar las horas del resumen

Se editan en `ajustes.toml`, dentro del volumen del panel (`/data/share/arthur/panel/ajustes.toml`; se crea solo la
primera vez). Formato `HH:MM`, una línea por día:

```
[resumen]
lunes = "08:30"
martes = "07:00"
miercoles = "08:30"
jueves = "10:00"
viernes = "07:00"
sabado = "10:00"
domingo = "10:00"

[obsidian]
carpeta = "/vault"
ficheros = ["00-Inicio/Tareas.md"]
```

Los días van sin tilde. `ficheros` contiene las notas de las que el panel lee casillas `Yo:`; puedes añadir
otras rutas `.md` relativas a la bóveda. Si te equivocas en una hora, el panel no arranca y te dice qué línea
está mal. La zona es `Europe/Madrid` en esta versión. Después de cambiar algo: `docker compose restart panel-web`.

Si el servidor estaba apagado a la hora del aviso, al arrancar lo manda igualmente, pero solo antes de las 13:00.
El botón de rehacer el resumen en la página escribe uno nuevo **sin** volver a avisar al móvil.

## 5. Obsidian con Syncthing

El panel lee la bóveda de Obsidian desde el servidor, así que la bóveda tiene que estar allí:

1. Instala Syncthing en el servidor y en el portátil/móvil donde usas Obsidian.
2. Comparte la carpeta de la bóveda entre ellos y acepta en cada lado.
3. En el servidor, pon la ruta de esa carpeta en `ARTHUR_BOVEDA` (`.env`): compose la monta en el panel como
   `/vault`. Después, `docker compose up -d panel-web`.
4. Solo cuentan las casillas de los ficheros elegidos cuyo texto empieza por `Yo:`, por ejemplo
   `- [ ] Yo: entregar el trabajo`.
   Al marcarla en el panel, se marca en el fichero `.md` de la bóveda y Syncthing lo lleva a tus aparatos. Si
   mientras tanto cambiaste esa línea en Obsidian, el panel te pide recargar en vez de pisarla.

## 6. La revisión de la noche y de la semana

A las 21:30 (el domingo, el resumen semanal) ARTHUR cuenta lo que hiciste y te avisa en el móvil; en el calendario,
los eventos que empiezan por «Estudio» llevan botones ✓ · ½ · ✗. Cómo cuenta, el juego y cómo cambiar la hora:
[`docs/revision.md`](revision.md).

## Variables del contenedor

| Variable | Qué es | Por defecto |
|---|---|---|
| `ARTHUR_PANEL_PORT` | puerto | `8775` |
| `ARTHUR_PANEL_HOST` | IP donde escucha (la de Tailscale) | `127.0.0.1` |
| `ARTHUR_PANEL_VAULT` | carpeta de la bóveda | `/vault` |
| `ARTHUR_PANEL_AVISOS_URL` | el núcleo, p. ej. `http://core:8770` (sin ella, sin avisos al móvil) | vacío |
| `ARTHUR_PANEL_AVISOS_TOKEN_FILE` | llave del aparato `panel` en el núcleo | `/run/secrets/panel_avisos` |
| `ARTHUR_PANEL_STUDIO_URL` | Studio, para sus tareas (con el secreto `panel_studio`) | vacío |
| `ARTHUR_WEBCHAT_CF_ACCOUNT` | id de cuenta de Cloudflare para Workers AI (sin él, el resumen lo escribe Ollama) | vacío |
| `ARTHUR_PANEL_CF_TOKEN_FILE` | token de Workers AI | `/run/secrets/workers_ai_token` |
| `ARTHUR_OLLAMA_URL` | Ollama, el motor de respaldo del resumen | `http://127.0.0.1:11434` |
| `TZ` | zona horaria | `Europe/Madrid` |

Para el aviso al móvil, en el núcleo: `--dispositivo-nuevo panel` y esa llave es el secreto `panel_avisos`
(lo hace `scripts/desplegar.sh`).
Las tareas de Instagram y TikTok necesitan que Creator Studio tenga también esta versión del código: el panel
consulta su ruta `/api/panel/shorts` para usar la fecha real de publicación. Antes de actualizar Studio, esa
tarjeta indicará que Studio no está disponible.
Configura también `ARTHUR_PANEL_PUBLIC_URL` **en el núcleo** con la dirección que abre tu móvil, por ejemplo
`http://100.64.0.10:8775` por Tailscale (sustituye esa IP de ejemplo por la tuya) o la dirección HTTPS protegida
por Cloudflare Access. Debe ser la raíz
del panel, sin ruta ni parámetros. El panel solo pide el destino fijo `panel`: el núcleo elige esta dirección y
rechaza el aviso dirigido al panel si falta o no es válida.

Para el chat de ARTHUR, en el núcleo: `ARTHUR_PANEL_URL` (p. ej. `http://panel-web:8775`) y su llave, creada en el
panel con `--panel-dispositivo-nuevo core`, como secreto `panel_core` (también lo hace `scripts/desplegar.sh`).
