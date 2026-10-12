# ARTHUR en Windows

| | Estado |
|---|---|
| **Servidor** en un PC con Windows 10/11 | Con Docker Desktop y `scripts/instalar-servidor.ps1` (esta guía). **Nuevo, en pruebas:** si algo falla, abre un *issue* |
| **Aparato** Windows (para hablarle desde ese PC) | La web de chat y, **nuevo, en pruebas (fase A)**, el aparato completo: voz con atajo, Orb, confirmaciones, apps, volumen y música (abajo). «Hey Arthur» y ventanas: fase B |

## Servidor en Windows 10/11

ARTHUR funciona igual que en Linux: los cuatro contenedores (`core`, `ollama`, `voz`, `push`) son de Linux, y
**Docker Desktop** los ejecuta en una pequeña máquina Linux integrada en Windows (WSL2).

### Lo que necesitas

- Windows 10 (22H2) o 11, con la virtualización activada en la BIOS (casi todos los PC modernos la traen).
- **16 GB de RAM** recomendados. Con 8 GB funciona, pero muy justo: Windows ya usa 3-4 GB.
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (gratis para uso personal) y
  [Git para Windows](https://git-scm.com/download/win), o bien descarga el ZIP del repositorio.
- [Tailscale](https://tailscale.com/download) en este PC y en tus aparatos (recomendado).

### Instalar

En PowerShell (no hace falta administrador):

```powershell
git clone https://github.com/<tu-usuario>/arthur-assistant.git
cd arthur-assistant
powershell -ExecutionPolicy Bypass -File scripts\instalar-servidor.ps1
```

Te pregunta lo mismo que el de Linux, con la opción recomendada en verde: el modelo de IA, si dejarlo siempre
cargado, la gráfica y la zona horaria. Además comprueba cuánta memoria tiene Docker:

- Docker Desktop solo le da a su máquina Linux **la mitad de la RAM** del PC. Si es menos de 7 GB, el instalador
  te ofrece subirla (escribe `memory=…GB` en `%USERPROFILE%\.wslconfig`). Después hay que cerrar Docker Desktop,
  ejecutar `wsl --shutdown`, volver a abrirlo y repetir el instalador.

### Diferencias con Linux

- **Gráfica:** Docker Desktop solo deja usar una **NVIDIA** (el instalador la ofrece si la encuentra). Las
  gráficas integradas Intel o AMD no se pueden usar desde Docker en Windows: la IA va con el procesador.
- **Red:** en vez de publicar ARTHUR en la IP de Tailscale, en Windows se usa `tailscale serve`, que además da
  HTTPS (el micrófono de la web y los avisos lo necesitan):

  ```powershell
  tailscale serve --bg 8770
  ```

  La web queda en `https://<tu-pc>.<tu-red>.ts.net`, solo para tus aparatos de Tailscale.
- **Seguir en marcha:** Docker Desktop arranca cuando **inicias sesión**, no al encender el PC. Para usarlo como
  servidor 24/7:
  - Docker Desktop → *Settings → General → Start Docker Desktop when you sign in*.
  - *Configuración → Sistema → Inicio/apagado y suspensión*: que no se suspenda nunca (enchufado).
  - Que tu usuario inicie sesión solo al encender (o déjalo con la sesión abierta y la pantalla bloqueada).
- **Las llaves** (`secrets\`) quedan con permisos solo para tu usuario (Windows no tiene los permisos 600 de
  Linux; el instalador quita la herencia de permisos de esa carpeta).

Todo lo demás (aparatos, Google, mantenimiento, seguridad) es igual: [servidor.md](servidor.md). Las órdenes son las
mismas en PowerShell, por ejemplo `docker compose exec core python -m arthur --dispositivo-nuevo movil`.

## Usar ARTHUR desde un PC con Windows (aparato)

Hay dos formas. Las dos necesitan un servidor de ARTHUR ya en marcha (en Linux o en Windows).

### La web de chat (sin instalar nada)

Escribir, hablarle con el micrófono, mandarle fotos y recibir avisos.

1. Copia `scripts\windows\arthur.cmd` a `%LOCALAPPDATA%\Microsoft\WindowsApps` (ya está en el PATH).
2. La primera vez: `arthur configurar https://<tu-servidor>.<tu-red>.ts.net`. Después, `arthur` la abre.
3. En Edge o Chrome, el menú «Instalar ARTHUR» la deja como una app con su icono.

### El aparato completo (fase A, en pruebas)

Como `arthur --servicio-cliente` en Linux: este PC pone el micrófono y el altavoz, y le presta sus «manos» al
servidor. Arranca solo al iniciar sesión.

| Qué | Cómo lo hace en Windows (solo con lo que trae Python, sin pip) |
|---|---|
| Hablarle | Atajo **Ctrl+Alt+A**: púlsalo, habla, y vuelve a pulsarlo. El micro se graba con winmm (waveIn) |
| Oír la respuesta | winsound, desde la memoria (la voz nunca se guarda en disco). «Calla» la corta |
| El Orb | `python -m arthur --abrir-orb` (desde la carpeta de ARTHUR) lo abre en el navegador; solo en este PC (127.0.0.1) |
| Confirmar («¿Abro Spotify?») | En el Orb si está abierto; si no, una notificación de Windows con **Sí / No**. No contestar es NO |
| Abrir apps | Las del **menú Inicio** (sus accesos directos). Nunca una ruta ni una orden: solo un nombre de la lista |
| Volumen y música | Teclas multimedia (lo mismo que pulsarlas): play/pausa, siguiente, anterior, subir/bajar, silenciar |

Lo que **no** hace (aún): «Hey Arthur», el Orb a pantalla completa, mover ventanas, los avisos de WhatsApp y los
gestos con la cámara (fase B). Tampoco ejecuta órdenes ni instala programas: eso nunca se hace a distancia.

**Lo que necesitas:** [Python 3.14 o más nuevo](https://www.python.org/downloads/windows/) (al instalarlo, marca
«Add python.exe to PATH») y este repositorio (con Git o el ZIP). Nada más.

**Instalar** (en PowerShell, desde la carpeta de ARTHUR; no hace falta administrador):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\instalar-aparato.ps1
```

Te pregunta, con la opción recomendada en verde:

1. **Conectar con tu servidor:** antes, en el servidor, `docker compose exec core python -m arthur --dispositivo-nuevo pc-windows`
   te da la llave de este PC. El instalador pide la dirección y la llave (no se ve al escribir) y la guarda en
   `%APPDATA%\arthur\cliente.toml`.
2. **Arrancar al iniciar sesión:** una tarea programada «ARTHUR (aparato)» de tu usuario, sin permisos de
   administrador, que se reinicia si se cae. Si Windows no deja crearla, usa un acceso directo en la carpeta Inicio.
3. **El atajo Ctrl+Alt+A:** un acceso directo «ARTHUR - hablar» en el menú Inicio.

Para quitarlo: el mismo instalador con `-Quitar` (deja el emparejamiento; bórralo a mano si ya no quieres este PC).

**Dónde queda cada cosa:**

- Configuración (opcional, como en Linux): `%APPDATA%\arthur\config.toml` (ejemplo en `config/config.example.toml`).
  Para que abrir una app no pida confirmación, ponla en `trusted_apps` con su nombre del menú Inicio en minúsculas y
  con guiones (`google-chrome`, `spotify`…; `list_applications` te los dice).
- El registro: `%LOCALAPPDATA%\arthur\arthur.log`. Lo que dices solo se apunta con el nivel DEBUG.
- El buzón del atajo: `%LOCALAPPDATA%\arthur\control.key`. Windows no tiene los sockets privados de Linux: ARTHUR
  escucha en 127.0.0.1 (nunca en la red) con una llave nueva en cada arranque, y sin esa llave no atiende a nadie.
  Esa carpeta solo la puede leer tu usuario.

**Si algo no va:**

- *No me oye:* Configuración → Privacidad y seguridad → Micrófono → «Permitir que las aplicaciones de escritorio
  accedan al micrófono». Y que el micro bueno sea el **predeterminado** de Windows.
- *No salen las notificaciones de Sí/No:* que no esté activado «No molestar» (o Asistente de concentración). Si no
  se pueden mostrar, ARTHUR lo cuenta como NO: abre el Orb y confirma allí.
- *El volumen no es exacto:* Windows no deja leerlo sin COM; ARTHUR lo baja a 0 y lo sube a pasos del 2 %.
