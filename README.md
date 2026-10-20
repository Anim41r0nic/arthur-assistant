# ARTHUR

**A**nalytical **R**eal-time **T**echnological **H**elper for **U**niversal **R**esponses.

Un asistente personal **propio**, como Siri o ChatGPT, pero que vive en **tu** servidor: le hablas («Hey Arthur»),
te responde con voz, maneja tu escritorio, te lee el calendario y el correo, te recuerda cosas y te avisa de tus
mensajes. La IA principal puede correr en tu casa con modelos abiertos y gratuitos, sin suscripción. Algunas
funciones opcionales usan servicios externos si los configuras; se detallan en «Privacidad».

Gratis y de código abierto (licencia [MIT](LICENSE)).

*English version below: [English](#english).*

## Qué hace

- **Voz natural:** «Hey Arthur» (o un atajo, o una doble palmada) y le hablas; empieza a responder en cuanto tiene la
  primera frase, mientras sigue pensando el resto.
- **Web de chat** para el móvil y cualquier navegador: escribir, hablar, mandarle fotos («¿qué hay en esta foto?»)
  y recibir avisos aunque la web esté cerrada.
- **El Orb:** una esfera animada a pantalla completa que escucha, piensa y habla contigo, con los botones Sí/No de
  las confirmaciones.
- **Tu escritorio Linux:** abrir apps, mover y cerrar ventanas, escritorios, volumen, música, portapapeles.
- **Google Calendar y Gmail:** «¿qué tengo hoy?», «apúntame dentista el jueves a las 5», «¿tengo correos nuevos?».
- **Mensajes:** «mándale un WhatsApp a Laura diciendo que llego tarde» lo deja escrito y lo envías tú; te avisa en
  voz alta de los mensajes que llegan.
- **Memoria y recordatorios:** «recuerda que…», «avísame en 20 minutos», con avisos al móvil.
- **Gestos con la cámara:** ✋ calla, 👍/👎 sí/no, ☝️ repite, ✌️ pausa la música, 👋 saluda. La cámara la
  enciendes tú y la imagen no sale del ordenador ([docs/gestos.md](docs/gestos.md)).
- **Instalar programas:** te abre una terminal con la orden preparada; la contraseña la pones tú.
- **Creator Studio:** fábrica de vídeos de YouTube (largo + Short) con piloto automático diario: guion, imágenes con
  licencia, voz, montaje y subida ([docs/studio.md](docs/studio.md)).
- **Apuntes:** fotos o PDF de clase → texto con fórmulas → PDF limpio; y «explícame esto» o «hazme ejercicios como
  los de mis exámenes» ([docs/apuntes.md](docs/apuntes.md)). Con la pestaña **Repaso**: preguntas cortas de tus
  apuntes con repaso espaciado, racha y aviso diario.
- **Segundo cerebro:** pregúntale a tus notas de Obsidian, tus apuntes y tus PDF y te contesta con un enlace a la
  fuente, sin inventar ([docs/cerebro.md](docs/cerebro.md)).
- **Panel de vida:** lo de hoy en un vistazo (tareas, calendario, correos importantes) y un resumen cada mañana en el
  móvil ([docs/panel.md](docs/panel.md)).
- **Para tu negocio:** un asistente para el chat de tu web pública (Cloudflare Workers AI gratis, con Ollama de
  respaldo) que contesta solo con lo que tú le escribes, y un registro de clientes sin duplicados
  ([docs/webchat.md](docs/webchat.md), [docs/clientes.md](docs/clientes.md)).

## Cómo funciona

```text
   tus aparatos                                  tu servidor (Docker)
 ┌──────────────────────┐                  ┌──────────────────────────────────┐
 │ portátil Linux:      │                  │ core   asistente, memoria, Google │
 │ «Hey Arthur», Orb,   │◄── Tailscale ───►│ ollama la IA (modelos abiertos)   │
 │ gestos, ventanas     │   (red privada)  │ voz    oír y hablar               │
 │ móvil / Windows: web │                  │ push   avisos al móvil            │
 └──────────────────────┘                  └──────────────────────────────────┘
```

El cerebro está en el servidor; cada aparato pone lo que solo él tiene (micrófono, altavoz, pantalla, cámara,
ventanas). Todo el detalle en [docs/servidor.md](docs/servidor.md).

## Instalar

**El servidor** (un ordenador Linux encendido 24/7, con 8 GB de RAM o más). Antes de empezar necesita
[Docker](https://docs.docker.com/engine/install/) con el plugin `compose` (y tu usuario en el grupo `docker`),
`git`, `curl` y `python3`; el instalador comprueba que estén y te dice cuál falta.

```bash
git clone https://github.com/Anim41r0nic/arthur-assistant.git
cd arthur-assistant
bash scripts/instalar-servidor.sh
```

El instalador te pregunta cada decisión con la opción **recomendada en verde** (Enter la acepta):

| Decisión | Opciones |
|---|---|
| Modelo de IA | `qwen3-vl:4b-instruct` (texto, herramientas y fotos en 8 GB), `qwen3:4b-instruct` (solo texto) u otro de Ollama |
| ¿Siempre cargado? | Que se descargue si no lo usas (RAM libre; la primera pregunta tras un rato tarda más) o siempre cargado (rápido siempre; ~4 GB ocupados) |
| Gráfica integrada | Si tienes una Intel o AMD, la IA puede usarla: con contexto largo, ~2 veces más rápida |
| Red | Publicar ARTHUR en tu red privada Tailscale (recomendado) o solo en el propio servidor |

**Tus aparatos:** el móvil y Windows usan la web (instrucciones en [docs/servidor.md](docs/servidor.md#tus-aparatos)).
Un ordenador Linux con escritorio, para «Hey Arthur», el Orb y los gestos:

```bash
bash scripts/instalar-aparato.sh     # en ese ordenador, desde la carpeta del repositorio
```

**¿Y Windows?**

| | Hoy | Próximamente |
|---|---|---|
| Servidor en Windows 10/11 | Con Docker Desktop: `scripts\instalar-servidor.ps1` ([docs/windows.md](docs/windows.md)) — **nuevo, en pruebas** | Que lo pruebe más gente |
| Aparato Windows | La web de chat: escribir, hablar con el micro, fotos, avisos (`scripts/windows/arthur.cmd` la abre) | «Hey Arthur», el Orb, voz, apps, volumen, música y ventanas |

**Todo en un solo ordenador**, sin servidor: también se puede (con [Ollama](https://ollama.com) instalado en él).
`python -m arthur --voz --orb` para probarlo y `scripts/systemd/arthur.service` como servicio; las piezas están
descritas en [docs/arquitectura.md](docs/arquitectura.md).

## Privacidad

- La IA, el reconocimiento de voz y la voz corren en tu servidor. Nada se manda a servicios de IA externos, salvo lo
  que actives tú: Gemini en Studio y Apuntes, y Cloudflare Workers AI en el chat de tu web, el resumen del panel y el
  cerebro (este solo manda los trozos de notas que necesita cada pregunta). Sin ellos, contesta Ollama.
- Ningún puerto abierto a internet: tus aparatos llegan por tu red privada (Tailscale).
- Cada aparato tiene su propia llave, revocable.
- Lo que dices y lo que responde **no** va a los registros (salvo que actives el nivel DEBUG).
- Tus cuentas (Google…) usan **tu** propia aplicación y tus permisos; nunca se guarda una contraseña.
- «ARTHUR prepara, tú envías»: correos y WhatsApp se dejan escritos, nunca se envían solos.

## Seguridad

| Riesgo | Herramientas | ¿Pide confirmación? | ¿En modo seguro? |
|---|---|---|---|
| lectura | `get_system_info`, `list_directory`, `read_file`, `list_applications`, `list_workspaces`, `search_packages`, `list_memories`, `list_contacts`, `list_reminders`, `gmail_inbox`, `recent_messages` (solo quién escribe, sin el texto) | No | Sí |
| bajo riesgo | `send_notification`, `volume`, `media`, `search_music`, `set_wake_word`, `set_gestures` (solo apagar la cámara), `switch_workspace`, `focus_window`, `move_window`, `set_reminder`, `cancel_reminder`, `compose_email`, `compose_whatsapp`, `reply_email`, `install_package` (solo abre una terminal: la contraseña la pones tú), `open_application` (apps de confianza) | No | No |
| escritura | `write_file`, `write_clipboard`, `open_url`, `close_window`, `remember`, `forget`, `save_contact`, `forget_contact`, `open_application` (las demás apps) | Sí (configurable con `confirm_writes`) | No |
| PRIVADA | `read_clipboard`, `gmail_read`, `recent_messages` con el texto de los mensajes, `set_gestures` (encender la cámara) | Siempre | No |
| PELIGROSA | `run_command`, `open_application` de una app que pide permisos de administrador | Siempre | No |

Las herramientas del calendario de Google se describen en la sección «Calendario y Gmail» y en
[docs/google.md](docs/google.md). La lista real y su riesgo está siempre en `/herramientas` (dentro del chat), y
un test (`tests/test_docs.py`) comprueba que esta tabla no se queda atrás.

- Solo lee y escribe en las carpetas de `read_roots` y `write_roots`. Nunca toca rutas sensibles
  (`.ssh`, llaveros, navegadores, `.env`, claves…), ni su propia configuración, registro o código.
- `run_command`: sin shell, solo programas de `command_allowlist`, nunca los de la lista negra
  (`rm`, `sudo`, shells…), y dentro de un sandbox de solo lectura, sin red y sin variables de entorno.
- `safe_mode = true` deja solo las herramientas de lectura.
- Cada acción queda registrada en `~/.local/state/arthur/audit.jsonl` (permisos 600): herramienta, argumentos, decisión
  y resultado. Los textos se recortan a 200 caracteres y se tapan los secretos con formas conocidas (claves de GitHub,
  `sk-…`, AWS, claves privadas, `password=…`); lo que no tiene una forma reconocible no se puede tapar.

## Documentación

- [docs/servidor.md](docs/servidor.md): instalar el servidor, conectar tus aparatos, seguridad, velocidad y mantenimiento.
- [docs/arquitectura.md](docs/arquitectura.md): cómo están hechas las piezas (asistente, router, voz, Orb…).
- [docs/google.md](docs/google.md): conectar Google Calendar y Gmail.
- [docs/gestos.md](docs/gestos.md): los gestos con la cámara.
- [docs/studio.md](docs/studio.md): Creator Studio (vídeos de YouTube).
- [docs/apuntes.md](docs/apuntes.md): Apuntes (fotos y PDF de clase a texto y PDF) y la pestaña Repaso.
- [docs/cerebro.md](docs/cerebro.md): el segundo cerebro y el Repaso.
- [docs/panel.md](docs/panel.md): el panel de vida.
- [docs/webchat.md](docs/webchat.md) y [docs/clientes.md](docs/clientes.md): el chat de tu web y el registro de clientes.
- [docs/windows.md](docs/windows.md): servidor en Windows 10/11 y el PC con Windows como aparato.
- [config/config.example.toml](config/config.example.toml): todas las opciones.

Tests: `python -m unittest` desde la carpeta del repositorio, con Python 3.11 o más y sin instalar nada (todo con
programas, cámaras y servidores **falsos**: nunca tocan tu escritorio). GitHub los pasa solo en cada cambio, con
Python 3.14 ([.github/workflows/tests.yml](.github/workflows/tests.yml)).

## Dependencias y licencias

ARTHUR está escrito en Python con **solo la biblioteca estándar** en su núcleo. Lo que usa alrededor:

| Pieza | Para qué | Licencia |
|---|---|---|
| [Ollama](https://ollama.com) + Qwen3 / Qwen3-VL | la IA | MIT / Apache-2.0 |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | oír | MIT |
| [Piper](https://github.com/OHF-Voice/piper1-gpl) + voz `es_ES-davefx` | hablar | GPL-3.0 (en su propio contenedor) / CC0 |
| [Vosk](https://alphacephei.com/vosk) | «Hey Arthur» | Apache-2.0 |
| [Three.js](https://threejs.org) (incluido en `ui/orb/vendor`) | el Orb | MIT |
| [OpenCV](https://opencv.org) + modelos MediaPipe de OpenCV zoo | gestos | Apache-2.0 |
| [cryptography](https://cryptography.io) (contenedor `push`) | cifrar los avisos al móvil | Apache-2.0 / BSD |
| [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) vía kokoro-onnx (contenedor `studio`) | la voz de los vídeos | Apache-2.0 / MIT |
| [FFmpeg](https://ffmpeg.org) (contenedor `studio`) | montar los vídeos | LGPL/GPL |
| [Typst](https://typst.app), [pandoc](https://pandoc.org) (contenedor `apuntes`) | el PDF limpio de los apuntes | Apache-2.0 / GPL-2.0 |
| [OCRmyPDF](https://ocrmypdf.readthedocs.io) + Tesseract (contenedor `apuntes`) | el PDF escaneado con texto | MPL-2.0 / Apache-2.0 |
| [KaTeX](https://katex.org) 0.19.0 (incluido en `arthur/notes/web/katex`) | las fórmulas de la pestaña Repaso | MIT |
| [NumPy](https://numpy.org) (contenedor `cerebro`) | la búsqueda por significado | BSD-3 |
| [poppler-utils](https://poppler.freedesktop.org) (contenedor `cerebro`) | el texto de los PDF | GPL-2.0 |
| [Cloudflare Workers AI](https://developers.cloudflare.com/workers-ai/) (opcional, capa gratis) | chat de tu web, resumen del panel y cerebro | servicio externo |

## Apoyar el proyecto

ARTHUR es gratis. Si te resulta útil, dale una ⭐ al repositorio y cuéntaselo a alguien. Si prefieres que te lo
dejen instalado y configurado (servidor, red privada, cuentas), el autor ofrece ese servicio: mira su perfil de GitHub.

## Licencia

[MIT](LICENSE): puedes usarlo, cambiarlo y compartirlo, también con fines comerciales, manteniendo el aviso de
copyright.

---

## English

ARTHUR is a personal assistant, like Siri or ChatGPT, that runs on **your own** server: you talk to it («Hey Arthur»),
it answers out loud, drives your Linux desktop, reads your calendar and email, sets reminders and tells you about new
messages. The main AI can run at home on free, open models without a subscription. Optional features use external
services when you configure them; see “Privacidad” below. Free and open source ([MIT](LICENSE)). The program, its
messages and the rest of the docs are in Spanish.

**What it does:** natural voice, a chat web app for your phone and any browser (text, voice, photos, notifications),
the animated Orb, Linux desktop control, Google Calendar and Gmail, WhatsApp and email drafts that *you* send,
memory and reminders, camera gestures, installing programs, Creator Studio (YouTube videos) and Apuntes (class
photos and PDFs to clean text and PDF).

### Install

**The server** (a Linux computer that stays on 24/7, with 8 GB of RAM or more). It needs
[Docker](https://docs.docker.com/engine/install/) with the `compose` plugin (and your user in the `docker` group),
`git`, `curl` and `python3`; the installer checks for them and tells you which one is missing.

```bash
git clone https://github.com/Anim41r0nic/arthur-assistant.git
cd arthur-assistant
bash scripts/instalar-servidor.sh
```

The installer asks each question with the **recommended option in green** (Enter accepts it): the AI model, whether
it stays loaded in memory, whether to use the integrated GPU, and whether to publish ARTHUR on your private
Tailscale network (recommended) or only on the server itself.

**Your devices:** phones and Windows use the web app ([docs/servidor.md](docs/servidor.md#tus-aparatos)). On a
Linux desktop, for «Hey Arthur», the Orb and gestures, run this from the repository folder:

```bash
bash scripts/instalar-aparato.sh
```

**Windows server** (Windows 10/11 with Docker Desktop, still being tested): `scripts\instalar-servidor.ps1`, see
[docs/windows.md](docs/windows.md).

### Privacy and security

The AI, speech recognition and the voice all run on your server, and no port is opened to the internet. Each device
has its own revocable key. What you say is not written to the logs. Emails and WhatsApp messages are drafted, never
sent on their own. Risky actions ask for confirmation first. The full table of tools and risks is in the Spanish
«Seguridad» section above.

### Tests

`python -m unittest` from the repository folder, with Python 3.11 or newer and nothing to install. Everything uses
**fake** programs, cameras and servers. GitHub runs them on every push and pull request with Python 3.14
([.github/workflows/tests.yml](.github/workflows/tests.yml)).
