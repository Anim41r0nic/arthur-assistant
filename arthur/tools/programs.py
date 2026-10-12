"""Ejecución de los programas del escritorio que usan las herramientas.

A diferencia de run_command, estos programas son fijos (los elige el código de
ARTHUR, nunca el modelo) y no van en el sandbox: necesitan acceso a tu sesión
gráfica para abrir apps, notificar o usar el portapapeles. Por eso sus argumentos
se validan antes en cada herramienta y nunca pasan por una shell.

Los tests sustituyen esta clase por una falsa: así nunca tocan tu escritorio.
"""

import shutil
import subprocess
import threading

from .base import ToolError


class Programs:
    def _resolve(self, argv: list[str]) -> list[str]:
        path = shutil.which(argv[0])
        if path is None:
            raise ToolError(f"El programa '{argv[0]}' no está instalado")
        return [path, *argv[1:]]

    def run(self, argv: list[str], *, input: str | None = None, timeout: int = 10) -> str:
        """Ejecuta y espera. Devuelve la salida; si falla, lanza ToolError con el motivo."""
        try:
            proc = subprocess.run(
                self._resolve(argv),
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"'{argv[0]}' no ha terminado en {timeout} s") from e
        if proc.returncode != 0:
            raise ToolError(f"'{argv[0]}' ha fallado: {proc.stderr.strip() or f'código {proc.returncode}'}")
        return proc.stdout

    def launch(self, argv: list[str], *, grace: float = 2.0) -> None:
        """Lanza una app que se queda abierta (p. ej. `uwsm app`, que SE CONVIERTE en la app).

        No se puede esperar a que termine. Esperamos `grace` segundos: si en ese tiempo
        sale con error, es que no arrancó; si sigue viva, la app está abierta.
        """
        if argv[0] != "uwsm" and shutil.which("uwsm"):
            # Cada programa en su propio "scope" de systemd, como con tus atajos. Importante cuando
            # ARTHUR es un servicio: si no, Firefox (p. ej.) viviría dentro de ARTHUR y se cerraría con él.
            argv = ["uwsm", "app", "--", *argv]
        proc = subprocess.Popen(
            self._resolve(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # independiente de ARTHUR: si ARTHUR se cierra, la app sigue
        )
        try:
            code = proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            # Sigue en marcha: un hilo recogerá su código de salida cuando la cierres
            # (si nadie lo recoge, queda un proceso "zombi" hasta que ARTHUR termina).
            threading.Thread(target=proc.wait, daemon=True).start()
            return
        if code != 0:
            raise ToolError(f"'{argv[0]}' no ha podido abrir la aplicación (código {code})")

    def feed(self, argv: list[str], data: str, *, timeout: int = 5) -> None:
        """Pasa `data` por la entrada estándar sin capturar la salida (wl-copy se queda en segundo plano)."""
        try:
            proc = subprocess.run(
                self._resolve(argv),
                input=data,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"'{argv[0]}' no ha terminado en {timeout} s") from e
        if proc.returncode != 0:
            raise ToolError(f"'{argv[0]}' ha fallado (código {proc.returncode})")


def desktop_notify(text: str, title: str = "ARTHUR", expire_ms: int = 2500) -> None:
    """Notificación de escritorio breve. Si falla (p. ej. sin sesión gráfica), no pasa nada."""
    try:
        Programs().run(["notify-send", "--app-name=ARTHUR", f"--expire-time={expire_ms}", "--", title, text])
    except ToolError:
        pass
