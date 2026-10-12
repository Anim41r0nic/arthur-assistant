# ARTHUR en tu servidor

El **cerebro** de ARTHUR vive en un servidor tuyo encendido 24/7 (un ordenador viejo o un mini PC con Linux y
Docker). Cada **aparato** desde el que le hablas (portátil, Windows, móvil) pone lo que solo él tiene: micrófono,
altavoz, pantalla, cámara, ventanas, música y avisos. Puedes apagar tu ordenador y ARTHUR sigue vivo: recordatorios,
correo, calendario, memoria, y le puedes hablar desde el móvil.

Es **autoalojado**: no hay ningún servicio central. Tus conversaciones, tu memoria y tus cuentas se quedan en tu
servidor.

## Piezas

```text
                          ┌───────────────── tu servidor (Docker compose) ─────────────────┐
  portátil ─ aparato ───┐ │  core   ARTHUR: asistente, herramientas de servidor, API y web │
  Windows ── web ───────┼─┤  ollama la IA (el modelo de lenguaje)                          │
  móvil ──── web ───────┘ │  voz    oír (whisper.cpp) y hablar (Piper)                     │
   (red privada:          │  push   avisos al móvil (Web Push)                             │
    Tailscale)            └────────────────────────────────────────────────────────────────┘
```

| En el servidor (`core`) | En cada aparato Linux (`arthur-cliente`) |
|---|---|
| Asistente, IA, router de herramientas, política, auditoría | Micrófono, «Hey Arthur», doble palmada, atajo de teclado |
| Memoria, contactos, recordatorios, historial | Altavoz: dice las frases según llegan |
| Gmail y Google Calendar | El Orb a pantalla completa y las confirmaciones Sí/No |
| Oír (whisper) y hablar (Piper) | Cámara y gestos (la imagen nunca sale del aparato) |
| Avisos al móvil | Ventanas, apps, volumen, portapapeles, música |
| La web de chat (móvil, Windows, cualquier navegador) | Avisos de escritorio (p. ej. WhatsApp Web) |

## Lo que necesitas

- Un **servidor Linux** con Docker y el plugin compose (probado en Debian 13). **8 GB de RAM** como mínimo (16 GB
  van mucho mejor) y ~15 GB de disco.
- **Tailscale** (gratis para uso personal) en el servidor y en tus aparatos: una red privada para que lleguen al
  servidor desde cualquier sitio sin abrir puertos en el router. Recomendado.

## Instalar

En el servidor:

```bash
git clone https://github.com/<tu-usuario>/arthur-assistant.git
cd arthur-assistant
bash scripts/instalar-servidor.sh
```

Te pregunta cada opción, con la recomendada en verde (Enter la acepta):

1. **Modelo de IA:** `qwen3-vl:4b-instruct` (texto, herramientas y fotos en 8 GB), `qwen3:4b-instruct` (solo
   texto) u otro de [Ollama](https://ollama.com/library) que escribas tú.
2. **¿Siempre cargado?** Que se descargue si no lo usas en 30 min (RAM libre; la primera pregunta después tarda
   más: ~2 min en un servidor de 8 GB solo con procesador) o siempre cargado (rápido siempre; ~4 GB ocupados).
   Con menos de 14 GB de RAM recomienda lo primero.
3. **Gráfica integrada** (Intel o AMD): si la encuentra, ofrece que la IA la use por Vulkan.
4. **Tailscale:** publicar ARTHUR en tu red privada (recomendado) o solo en el propio servidor.
5. **Zona horaria** y **quién manda los avisos** (lo exigen Apple y Google: la dirección de tu ARTHUR o un correo).

Después crea las llaves internas (`secrets/`, permisos 600), descarga los modelos de voz comprobando su huella
sha256, construye las imágenes (la primera vez compila whisper.cpp para tu procesador), descarga el modelo de IA
y da de alta tu primer aparato. Puedes volver a ejecutarlo: respeta lo que ya existe.

Todo lo que eliges queda en ficheros normales que puedes editar: `.env` (ver `.env.example`) y
`config/servidor.toml` (ver `config/servidor.example.toml`). Tras cambiarlos:
`docker compose up -d && docker compose restart core`.

### La web por HTTPS

El micrófono y los avisos del navegador exigen HTTPS. Con Tailscale es una orden, en el servidor:

```bash
sudo tailscale serve --bg 8770
```

La web queda en `https://<tu-servidor>.<tu-red>.ts.net`, solo para tus aparatos de Tailscale.

## Tus aparatos

Cada aparato tiene su **propia llave**, que se puede retirar sin tocar las demás. En el servidor:

```bash
docker compose exec core python -m arthur --dispositivo-nuevo movil     # la llave sale UNA vez: guárdala
docker compose exec core python -m arthur --dispositivos                # lista
docker compose exec core python -m arthur --revocar movil               # retirarla
```

- **Móvil (iPhone o Android):** abre la web, pega la llave y añádela a la pantalla de inicio (en el iPhone:
  compartir → «Añadir a pantalla de inicio»; los avisos solo funcionan así, iOS 16.4 o más). El botón 🔕/🔔 activa
  los avisos de los recordatorios.
- **Windows:** copia `scripts/windows/arthur.cmd` a `%LOCALAPPDATA%\Microsoft\WindowsApps`; la primera vez
  `arthur configurar https://<tu-servidor>.<tu-red>.ts.net` y después `arthur` abre la web. En Edge o Chrome,
  «Instalar ARTHUR» la deja como una app.
- **Linux con escritorio** («Hey Arthur», Orb, gestos, avisos de mensajes, ventanas y apps): en ese ordenador,
  desde la carpeta del repositorio, `bash scripts/instalar-aparato.sh`. Sin servidor de IA en el ordenador: solo
  lo ligero (~250 MB de RAM).

## Seguridad

- **Ningún puerto abierto a internet.** `core` escucha en 127.0.0.1 y, si lo eliges, en tu IP de Tailscale. Los
  demás contenedores solo hablan por la red interna de Docker, con llave. `push` sale a internet solo para
  entregar avisos a Apple, Google, Mozilla o Microsoft (no admite otras direcciones).
- **Contenedores sin privilegios:** usuario no root, sistema de ficheros de solo lectura salvo su volumen,
  `cap_drop: ALL`, `no-new-privileges`, memoria limitada.
- **Una llave por aparato** (en el servidor solo se guarda su huella). La web la guarda en una cookie HttpOnly.
- **Herramientas de cada aparato, con doble control.** Al conectarse, el aparato anuncia lo que sabe hacer en él
  (ventanas, apps, volumen…). El servidor las comprueba (argumentos, política, confirmación, auditoría) y el
  aparato **vuelve a validar** con sus propias reglas antes de ejecutar: un servidor comprometido no puede hacer
  en tu ordenador nada que tu ordenador no permita. Nada PELIGROSO viaja por la red. Si el riesgo depende de los
  argumentos (p. ej. «quién me ha escrito» frente a «qué me ha dicho»), el aparato lo anuncia y el servidor pide
  la misma confirmación que pediría el aparato.
- **Confirmaciones donde estás:** en el Orb (Sí/No, o un gesto para lo de riesgo bajo), en una notificación o
  en la web.
- **Honestidad:** ARTHUR no puede decir que ha hecho algo si no ha usado la herramienta y ha salido bien
  (`arthur/core/honesty.py`); con la voz a trozos, cada frase pasa esa comprobación antes de decirse.

## Velocidad

Medido en un portátil de 2019 usado como servidor (i7 de 10.ª generación, 8 GB, gráfica Intel Iris Plus),
con `qwen3-vl:4b-instruct`:

| | Solo procesador | Con la gráfica integrada |
|---|---|---|
| Escribir con 5.500 palabras de contexto | 1,6 palabras/s | 3,3 palabras/s |
| Respuesta típica de una frase | 22-27 s | ~14 s |
| Por voz: hasta que empieza a hablar | — | ~10 s (dice cada frase en cuanto la tiene) |

- La IA reaprovecha lo ya leído (caché de Ollama): de cada pregunta solo lee lo nuevo.
- Si un aparato se desconecta un momento, sus herramientas se guardan 10 minutos: al volver no hay que volver a
  preparar nada.
- Con 16 GB y el modelo siempre cargado, nunca hay «primera pregunta lenta».

## Google (opcional)

Calendar y Gmail, con tu propia aplicación de Google Cloud (gratis): [docs/google.md](google.md). En el servidor no
hay navegador; el permiso se da así:

```bash
docker compose cp ~/client_secret_XXX.json core:/data/config/arthur/google-client.json
docker compose exec -it core python -m arthur --conectar-google
```

Te enseña un enlace: ábrelo en tu ordenador o móvil, acepta, y cuando el navegador vaya a una página de
`127.0.0.1` que no carga, copia su dirección completa y pégala en la terminal del servidor.

## Mantenimiento

```bash
git pull && docker compose build && docker compose up -d     # actualizar
docker compose logs -f core                                   # registro (sin el texto de tus conversaciones)
docker compose ps                                             # estado
```

Copias de seguridad: los volúmenes `core-data` (memoria, aparatos, permiso de Google) y `push-data` (la llave de
los avisos). `ollama` y `modelos/` se pueden volver a descargar.
