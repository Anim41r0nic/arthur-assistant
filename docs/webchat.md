# Asistente de la web

## Qué es

El chat de tu web pública (en los ejemplos, `ejemplo.com`). Un visitante pregunta por los servicios y el asistente le
contesta con lo que dice `conocimiento.md` (y solo con eso). Si la persona quiere hablar contigo, el chat le ofrece
un formulario (nombre, correo, teléfono opcional, qué necesita y consentimiento); el contacto se entrega al
**registro de clientes** (`docs/clientes.md`) y ARTHUR avisa al móvil: «Cliente nuevo C-0012» o «C-0012 ha vuelto a
escribir». Tocar el aviso abre la ficha de ese cliente.

No tiene herramientas: el modelo solo puede escribir texto. Contesta con **Cloudflare Workers AI** (plan gratuito,
por su API REST desde el servidor); si falla, responde 429 o se acaba el cupo del día, con Ollama; y si los dos fallan
o se pasa del tope duro, con un mensaje fijo con tu correo (`ARTHUR_WEBCHAT_CORREO`). El campo `engine` de cada respuesta dice quién
contestó: `workers-ai`, `local` o `fijo`.

**Sin Gemini** (decisión de 2026-10-17): las condiciones de la API de Gemini dicen «You may use only Paid
Services when making API Clients available to users in the European Economic Area», y con la clave gratuita no se
puede ofrecer a visitantes. El Gemini de Apuntes y Studio no cambia: solo los usa el dueño del servidor.

## Cómo está montado

El contenedor `webchat` **no publica ningún puerto** (nada de `ports:`). Solo se une a la red interna de Docker que
comparte con nginx, y nginx es lo único que le habla. Desde fuera solo se llega por Cloudflare, que protege nginx.

## Secretos

| Secreto Docker | Qué es |
|---|---|
| `workers_ai_token` | token de API de Cloudflare con permiso **Workers AI** (lectura y edición; ver abajo) |
| `turnstile_secret` | la clave SECRETA de Cloudflare Turnstile (la pública va en la web, ver abajo) |
| `webchat_avisos` | llave del aparato `webchat` en el núcleo (`--dispositivo-nuevo webchat`), para el aviso al móvil |
| `clientes_webchat` | llave del aparato especial `webchat` de clientes (`--clientes-llave-webchat`): SOLO sirve para entregar contactos, no para leerlos |

El conocimiento no es un secreto, pero se monta aparte: `conocimiento.md` en solo lectura, en
`/conocimiento/conocimiento.md`. Se recarga solo al cambiar el fichero, sin reiniciar. Si tiene el marcador
`[PRECIO]` sin rellenar, el asistente no inventa precios.

## Ponerlo en marcha

El servicio `webchat` está en `compose.yaml` (perfil `webchat`). Con el registro de clientes:

```
bash scripts/desplegar.sh clientes webchat
```

El script crea las llaves `webchat_avisos` y `clientes_webchat` sin enseñarlas, conecta el chat con clientes y, si no
existe, deja un `conocimiento.md` de ejemplo para que lo escribas. Antes (o después, y vuelves a lanzarlo):

- en `.env`: `ARTHUR_WEBCHAT_ORIGIN` (la dirección de tu web), `ARTHUR_WEBCHAT_NOMBRE`, `ARTHUR_WEBCHAT_CORREO` y
  `ARTHUR_CF_ACCOUNT`;
- en `secrets/`: `workers_ai_token` y `turnstile_secret` (ver abajo). Son ficheros del anfitrión montados tal cual,
  con sus permisos: el usuario del contenedor (uid 1000) tiene que poder leerlos.

Para que tu proxy (nginx, cloudflared…) le hable, ponlo en la misma red de Docker: por ejemplo, añádelo como otro
servicio en un `compose.override.yaml`. El chat no publica ningún puerto.

## Workers AI: cuenta y token

1. En el panel de Cloudflare, **Workers AI** → **Use REST API**.
2. **Create a Workers AI API Token** (la plantilla ya trae los permisos `Workers AI - Read` y `Workers AI - Edit`;
   si lo creas a mano, pon los dos). Cópialo a `secretos/workers_ai_token`.
3. Copia el **Account ID** en `ARTHUR_WEBCHAT_CF_ACCOUNT` (32 caracteres; no es un secreto).

El token no se escribe nunca en el registro ni en los mensajes de error. El modelo por defecto es
`@cf/google/gemma-4-26b-a4b-it`, que está en el plan gratis (algunos modelos grandes piden Workers Paid y en el plan
gratis contestan 403: el asistente lo anota y contesta Ollama). Se cambia con `ARTHUR_WEBCHAT_CF_MODEL`.

La capa gratis son **10 000 neuronas al día** y se renueva a las **00:00 UTC** (las 01:00 o 02:00 en Madrid). Si se
acaba, Cloudflare responde 429 (código 3036): el asistente deja de llamar a Workers AI hasta las 00:00 UTC y contesta
Ollama. Aparte, `ARTHUR_WEBCHAT_CF_DIA` pone un tope propio para no llegar a eso.

## nginx (ejemplo)

```
# Exacto para el chat: con «location /api/chat/» (con barra), nginx contestaría a POST /api/chat con un 301 hacia
# /api/chat/; el navegador repite la petición como GET sin cuerpo y el chat da «no disponible».
location = /api/chat {
    proxy_pass http://webchat:8776;
    proxy_read_timeout 90s;
    proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
}
# El resto de rutas (/api/chat/session, /api/chat/contact, /api/chat/health). No vale un solo
# «location /api/chat» sin barra: también atraparía /api/chatX o /api/chat-loquesea.
location /api/chat/ {
    proxy_pass http://webchat:8776;
    proxy_read_timeout 90s;
    proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
}
```

`proxy_read_timeout 90s` porque una respuesta puede tardar: Workers AI hasta 20 s y, si falla, Ollama lo que quede hasta 62 s (mínimo 15 s). El chat de la web espera 70 s. Ollama recibe un prompt corto (reglas, los trozos de `conocimiento.md` que tienen que ver con la pregunta y el último intercambio): con el completo tardaba ~100 s en el servidor. El asistente limita por la IP de `CF-Connecting-IP`, que
pone Cloudflare; nginx solo es accesible por el túnel, así que nadie puede falsearla.

## La web que muestra el chat

- La política de contenido (CSP) puede ser una etiqueta `<meta>` en `index.html`. Si nginx también envía
  una cabecera CSP, hay que fusionarlas: con dos, el navegador aplica las dos y gana la más restrictiva.
- La clave PÚBLICA de Turnstile (sitekey) va en el atributo `data-sitekey` de la etiqueta `<script>` de `chat.js`.
- Si la web vive en otro repositorio, publícala con `git archive` para que no se cuele nada que no esté en git
  (`.git`, borradores, `contenido/`).

## Contactos: van al registro de clientes

El chat **no guarda contactos**. Valida el formulario y lo entrega a `clientes`
(`POST http://clientes:8777/api/interno/contacto`), que aplica las reglas de duplicados (mismo correo o teléfono →
mismo cliente) y lo borra a los 365 días del último contacto. Se ven, se contestan y se borran en la **página de
clientes** (`docs/clientes.md`); las órdenes `--webchat-contactos` y `--webchat-borrar` ya no existen.

- **Sin `ARTHUR_WEBCHAT_CLIENTES_URL`** (o sin poder leer la llave): el formulario contesta «no disponible» (503) y
  la web enseña tu correo. Nada se pierde en silencio.
- **`clientes` caído o lento (más de 5 s)**: la visita ve «recibido» igual; el contacto queda en una cola en el
  volumen (`/data/share/arthur/webchat/cola/`, un fichero por contacto, permisos 600) y se reintenta cada minuto.
  Mientras, el aviso al móvil dice «Contacto nuevo en cola (clientes no responde)»; al entregarse llega el aviso
  normal con el número de cliente.
- **Un dato que `clientes` rechaza** (p. ej. un teléfono que no le vale): la visita recibe el error de ese campo.
  Si eso pasa con uno que ya estaba en la cola, se aparta como `*.rechazado`; un fichero de la cola ilegible, como
  `*.corrupto`. Ninguno de los dos frena a los demás. Llevan datos personales: míralos y bórralos a mano.
- **Migración (una vez)**: al arrancar con `clientes` configurado, los contactos que hubiera en la base vieja
  (`contactos.sqlite3`) pasan a la cola y el fichero se borra. Llegan a `clientes` con la fecha del día de la
  migración, no la original.

Para que tocar el aviso abra la ficha, el **núcleo** necesita `ARTHUR_CLIENTES_PUBLIC_URL` (la raíz de la página de
clientes, p. ej. `http://100.64.0.10:8777`, validada como `ARTHUR_PANEL_PUBLIC_URL`). El chat solo pide el destino
fijo `clientes` y el número; la dirección la pone el núcleo (`…#c12`), y solo acepta ese destino de la llave
`webchat`. Sin esa variable, el núcleo rechaza el aviso con destino (503).

Para mirar la cola sin abrir los contactos (solo cuántos hay):

```
docker compose exec webchat sh -c 'ls /data/share/arthur/webchat/cola | wc -l'
```

## Ajustar los límites

Variables de entorno del servicio (valores por defecto entre paréntesis; un valor roto o no positivo se ignora):

| Variable | Qué limita |
|---|---|
| `ARTHUR_WEBCHAT_POR_HORA` (20) | mensajes por IP y hora |
| `ARTHUR_WEBCHAT_PASES` (3) | pases de sesión por IP cada 10 min |
| `ARTHUR_WEBCHAT_CONTACTOS` (3) | contactos por IP y día |
| `ARTHUR_WEBCHAT_CF_DIA` (400) | respuestas con Workers AI al día, contadas por día UTC (luego solo Ollama) |
| `ARTHUR_WEBCHAT_TOPE_DIA` (600) | respuestas al día; después, mensaje fijo |

`CF_DIA` sale de la capa gratis: con Gemma 4 una respuesta del chat gasta unas 22 neuronas (unos 2000 tokens de
entrada con `conocimiento.md` y ~200 de salida), y 400 × 22 = 8800 de 10 000. Si cambias de modelo o
`conocimiento.md` crece mucho, mira el gasto real en el panel de Workers AI y ajústalo. El cupo de Workers AI se
reinicia a las 00:00 UTC (como Cloudflare); el tope duro, a medianoche de Madrid. Los contadores viven en memoria:
al reiniciar, empiezan de cero (si eso pasa con el cupo casi gastado, el 429 de Cloudflare hace de red de seguridad).

## Otras variables

| Variable | Qué es | Por defecto |
|---|---|---|
| `ARTHUR_WEBCHAT_HOST` | IP donde escucha (el Dockerfile pone `0.0.0.0`) | `127.0.0.1` |
| `ARTHUR_WEBCHAT_PORT` | puerto | `8776` |
| `ARTHUR_WEBCHAT_ORIGIN` | `Origin` aceptado en los POST | `https://ejemplo.com` |
| `ARTHUR_WEBCHAT_NOMBRE` | en nombre de quién contesta («contactar con Ana Ruiz»); sin él, «quien lleva la web» | vacío |
| `ARTHUR_WEBCHAT_CORREO` | el correo que dan los mensajes fijos | `hola@ejemplo.com` |
| `ARTHUR_WEBCHAT_CONOCIMIENTO` | ruta de `conocimiento.md` | `/conocimiento/conocimiento.md` |
| `ARTHUR_WEBCHAT_CF_ACCOUNT` | id de cuenta de Cloudflare (no es secreto; sin él, solo Ollama) | vacío |
| `ARTHUR_WEBCHAT_CF_TOKEN_FILE` | token de Workers AI | `/run/secrets/workers_ai_token` |
| `ARTHUR_WEBCHAT_CF_MODEL` | modelo de Workers AI | `@cf/google/gemma-4-26b-a4b-it` |
| `ARTHUR_WEBCHAT_TURNSTILE_FILE` | secreto de Turnstile | `/run/secrets/turnstile_secret` |
| `ARTHUR_WEBCHAT_AVISOS_URL` | el núcleo, p. ej. `http://core:8770` (sin ella, sin avisos al móvil) | vacío |
| `ARTHUR_WEBCHAT_AVISOS_TOKEN_FILE` | llave del aparato `webchat` | `/run/secrets/webchat_avisos` |
| `ARTHUR_WEBCHAT_CLIENTES_URL` | el registro de clientes, p. ej. `http://clientes:8777` (solo red interna; sin ella, el formulario da «no disponible») | vacío |
| `ARTHUR_WEBCHAT_CLIENTES_TOKEN_FILE` | llave de webchat en clientes | `/run/secrets/clientes_webchat` |
| `ARTHUR_OLLAMA_URL` | Ollama, el motor de respaldo | `http://127.0.0.1:11434` |
| `ARTHUR_OLLAMA_NUM_CTX` | contexto de Ollama; el MISMO que `brain.context_tokens` del core (si no, recarga el modelo) | `8192` |
| `ARTHUR_OLLAMA_KEEP_ALIVE` | cuánto sigue el modelo en RAM; el MISMO que `brain.keep_alive` del core | `30m` |

Sin cuenta o token de Workers AI el asistente arranca igual y contesta con el modelo local. Sin secreto de Turnstile, el chat
responde «no disponible» en vez de dejar pasar a todo el mundo.
