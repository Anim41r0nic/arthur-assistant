# ARTHUR en Windows

| | Estado |
|---|---|
| **Servidor** en un PC con Windows 10/11 | Con Docker Desktop y `scripts/instalar-servidor.ps1` (esta guía). **Nuevo, en pruebas:** si algo falla, abre un *issue* |
| **Aparato** Windows (para hablarle desde ese PC) | Hoy, la web de chat (abajo). «Hey Arthur», el Orb y el control del escritorio: en preparación |

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

Hoy, con la **web de chat**: escribir, hablarle con el micrófono, mandarle fotos y recibir avisos.

1. Copia `scripts\windows\arthur.cmd` a `%LOCALAPPDATA%\Microsoft\WindowsApps` (ya está en el PATH).
2. La primera vez: `arthur configurar https://<tu-servidor>.<tu-red>.ts.net`. Después, `arthur` la abre.
3. En Edge o Chrome, el menú «Instalar ARTHUR» la deja como una app con su icono.

En preparación: un aparato Windows completo como el de Linux («Hey Arthur», el Orb a pantalla completa, voz, abrir
apps, volumen, música y ventanas).
