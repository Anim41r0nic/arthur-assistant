"""Punto de entrada: `python -m arthur`."""

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from . import __version__
from .app import build_app
from .core.config import ConfigError, load_config, state_dir
from .core.logging_setup import setup_logging
from .interfaces.cli import CliConfirmer, run_chat
from .security.audit import AuditLog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arthur", description="Asistente personal ARTHUR")
    parser.add_argument("--config", type=Path, help="ruta a un config.toml")
    parser.add_argument("--debug", action="store_true", help="muestra todo el registro en la terminal")
    parser.add_argument("--estados", action="store_true", help="muestra los cambios de estado")
    parser.add_argument("--voz", action="store_true", help="hablar con ARTHUR: te escucha (Enter) y responde en voz alta")
    parser.add_argument("--orb", action="store_true", help="abre también la interfaz visual (el Orb)")
    parser.add_argument("--cerebro", choices=("ollama", "reglas"), help="cambia el cerebro solo para esta sesión")
    parser.add_argument("--conectar-google", action="store_true",
                        help="autoriza a ARTHUR a usar Google: leer calendario y Gmail, crear eventos (se abre el navegador)")
    parser.add_argument("--desconectar-google", action="store_true", help="borra el permiso de Google del llavero")
    parser.add_argument("--servicio", action="store_true",
                        help="modo servicio, sin terminal (lo usa systemd al arrancar el escritorio)")
    parser.add_argument("--abrir-orb", action="store_true", help="abre en el navegador el Orb del ARTHUR en marcha")
    parser.add_argument("--calibrar-activacion", action="store_true",
                        help="dices «Hey Arthur» varias veces y ARTHUR anota cómo lo entiende (sin guardar audio)")
    servidor = parser.add_argument_group("ARTHUR en el servidor (docs/servidor.md)")
    servidor.add_argument("--core", action="store_true", help="modo servidor (lo usa el contenedor Docker)")
    servidor.add_argument("--dispositivo-nuevo", metavar="NOMBRE", help="da de alta un aparato y muestra su llave (en el servidor)")
    servidor.add_argument("--dispositivos", action="store_true", help="lista los aparatos dados de alta (en el servidor)")
    servidor.add_argument("--revocar", metavar="NOMBRE", help="quita el acceso a un aparato (en el servidor)")
    servidor.add_argument("--emparejar", metavar="URL", help="conecta este aparato a un servidor de ARTHUR")
    servidor.add_argument("--servicio-cliente", action="store_true",
                          help="el portátil como aparato del servidor, sin terminal (lo usa systemd): «Hey Arthur» y atajo")
    servidor.add_argument("--cliente", action="store_true",
                          help="habla con el ARTHUR del servidor (por texto; con --voz, también hablando)")
    parser.add_argument("--web", nargs="?", const="", metavar="URL",
                        help="abre la web de ARTHUR en el navegador (la primera vez: arthur --web https://…)")
    parser.add_argument("--version", action="version", version=f"arthur {__version__}")
    args = parser.parse_args(argv)

    if args.web is not None:
        return web_command(args.web)

    if args.dispositivo_nuevo or args.dispositivos or args.revocar:
        return devices_command(args)
    if args.emparejar or args.cliente or args.servicio_cliente:
        return client_command(args)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2

    if args.cerebro:
        provider = {"ollama": "ollama", "reglas": "rules"}[args.cerebro]
        config = dataclasses.replace(config, brain=dataclasses.replace(config.brain, provider=provider))

    if args.voz:
        config = dataclasses.replace(config, voice=dataclasses.replace(config.voice, tts=True, stt=True))

    if args.conectar_google or args.desconectar_google:
        return google_command(connect=args.conectar_google)
    if args.core:
        return core_command(config, debug=args.debug)

    from .api.control import default_socket_path
    from .interfaces.service import run_client, run_service, service_running

    if args.calibrar_activacion:
        return calibrate_wake(config)
    if args.abrir_orb:
        return open_orb_command()
    if not args.servicio and service_running():
        # Ya hay un ARTHUR (el servicio): en vez de arrancar otro, hablamos con él.
        return run_client()
    if args.servicio:
        # Sin terminal: voz y atajo, Orb sin abrir el navegador, y todo al registro (journal).
        config = dataclasses.replace(
            config,
            voice=dataclasses.replace(config.voice, tts=True, stt=True),
            orb=dataclasses.replace(config.orb, open_browser=False),
        )

    log_path = setup_logging(config.logging, debug=args.debug, service=args.servicio)
    log = logging.getLogger("arthur")
    log.info("Arrancando ARTHUR %s (cerebro: %s, log: %s)", __version__, config.brain.provider, log_path)

    from .security.broker import RoutingConfirmer
    from .tools.programs import desktop_notify

    # Se confirma donde estás: en la terminal si lo escribiste; en el Orb si usaste el atajo.
    from .security.confirm import DenyAll

    terminal = DenyAll() if args.servicio else CliConfirmer(input, print)  # el servicio no tiene terminal
    confirmer = RoutingConfirmer(terminal, say=print, notify=desktop_notify)
    memory = None
    if config.memory.enabled:
        from .memory.store import MemoryStore

        memory = MemoryStore()
    notifications = None
    if config.notifications.enabled:
        from .integrations.notifications import NotificationWatcher

        notifications = NotificationWatcher(lambda note: None)  # build_app le asigna su función de aviso
    wake = None
    if args.servicio and config.wake.enabled:
        from .voice.wakeword import WakeSwitch

        wake = WakeSwitch()  # el detector se conecta dentro de run_service
    gestures = None
    if args.servicio and config.gestures.enabled:
        from .interfaces.gestos.watcher import GestureSwitch

        gestures = GestureSwitch()  # el vigilante (con la cámara APAGADA) se conecta dentro de run_service
    app = build_app(config, confirmer=confirmer, audit=AuditLog(state_dir() / "audit.jsonl"),
                    memory=memory, notifications=notifications, wake=wake, gestures=gestures)
    if notifications:
        try:
            notifications.start()
        except OSError as e:
            print(f"⚠ No puedo escuchar las notificaciones: {e}")
            notifications = None
    server = start_orb(app, confirmer, gestures) if (args.orb or args.servicio) else None
    try:
        if args.servicio:
            code = run_service(app, orb_url=lambda: server.url if server else None,
                               control_path=default_socket_path(), wake=wake, gestures=gestures,
                               broker=server.broker if server else None)
        else:
            code = run_chat(app, show_states=args.estados, control_path=default_socket_path())
    finally:
        if server:
            server.stop()
        if app.assistant.speaker:
            app.assistant.speaker.close()
        if memory:
            memory.close()
        if notifications:
            notifications.stop()
    log.info("ARTHUR detenido")
    return code


def google_command(connect: bool) -> int:
    from .integrations.google import SCOPES, GoogleAuth, GoogleError
    from .tools.base import ToolError
    from .tools.programs import Programs

    auth = GoogleAuth()
    if not connect:
        auth.disconnect()
        print("Permiso de Google borrado del llavero. Para retirarlo también en Google: "
              "https://myaccount.google.com/permissions")
        return 0

    import os

    # Sin pantalla (el servidor, por SSH): no hay navegador aquí ni forma de que vuelva a este equipo.
    headless = not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def open_browser(url: str) -> None:
        if headless:
            print("\n1) Abre esta dirección en el navegador de tu ordenador o móvil y acepta:\n\n" + url + "\n")
            print("2) Al final, el navegador irá a una página de 127.0.0.1 que NO carga: es normal.\n"
                  "   Copia la dirección completa de la barra y pégala aquí (lleva un código de un solo uso).\n")
            return
        print("Abriendo Google en el navegador. Si no se abre, copia esta dirección:\n" + url)
        try:
            Programs().launch(["xdg-open", url])
        except ToolError:
            pass

    print("Permisos que se piden: leer tu calendario, CREAR eventos en él y leer Gmail "
          "(no puede enviar, borrar ni modificar correos).")
    try:
        auth.authorize(SCOPES, open_browser, paste=input if headless else None)
    except GoogleError as e:
        print(f"⚠ {e}")
        return 1
    print("✔ Conectado. Prueba en ARTHUR: «¿qué tengo hoy?», «apúntame dentista mañana a las 17:00» o «¿tengo correos nuevos?»")
    return 0


def calibrate_wake(config, attempts: int = 5) -> int:
    """Calibración: qué texto "oye" Vosk cuando TÚ dices la frase. Solo se guarda ese texto, nunca el audio."""
    import json

    from .voice.recorder import Recorder
    from .voice.wakeword import vosk_recognizer

    cfg = config.wake
    try:
        free = vosk_recognizer(cfg.model_path, None)
        limited = vosk_recognizer(cfg.model_path, [cfg.phrase, *cfg.variants])
    except ImportError:
        print("Falta Vosk: sudo pacman -S python-vosk")
        return 1
    except Exception as e:
        print(f"No puedo cargar el modelo de Vosk ({cfg.model_path}): {e}")
        return 1
    print(f"Calibración de «{cfg.phrase}». Di la frase {attempts} veces, con naturalidad.")
    results = []
    for n in range(1, attempts + 1):
        try:
            input(f"\n[{n}/{attempts}] Pulsa Enter y di «Hey Arthur»… ")
        except (EOFError, KeyboardInterrupt):
            break
        recorder = Recorder(max_seconds=8)
        recorder.start()
        recorder.wait_for_end(silence=0.8, no_speech=4, max_seconds=6)
        audio = recorder.stop()  # solo en memoria; se descarta tras analizarlo
        for rec in (free, limited):
            rec.Reset()
            rec.AcceptWaveform(audio)
        heard = json.loads(free.FinalResult())["text"]
        matched = json.loads(limited.FinalResult())["text"]
        detected = any(p in matched for p in (cfg.phrase, *cfg.variants))
        results.append({"oido": heard, "con_gramatica": matched, "detectado": detected})
        print(f"   el modelo ha oído: «{heard}» · {'✔ detectado' if detected else '✘ no detectado'}")
        del audio
    path = state_dir() / "calibracion.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps({"frase": cfg.phrase, "intentos": results}, ensure_ascii=False, indent=2))
    ok = sum(r["detectado"] for r in results)
    print(f"\nDetectado {ok} de {len(results)} veces. Resultado (solo texto) en {path}")
    return 0


def web_command(url: str) -> int:
    """Abre la web de chat del servidor (fase S5). Con URL, además la recuerda para las siguientes veces."""
    from .interfaces.core import RemoteError, load_web_url, save_web_url
    from .tools.base import ToolError
    from .tools.programs import Programs

    try:
        url = save_web_url(url) if url else load_web_url()
    except RemoteError as e:
        print(f"✘ {e}", file=sys.stderr)
        return 2
    if not url:
        print("Aún no sé dónde está tu web. La primera vez: arthur --web https://TU-SERVIDOR.ts.net", file=sys.stderr)
        return 1
    try:
        Programs().launch(["xdg-open", url])
    except ToolError as e:
        print(f"⚠ No he podido abrir el navegador: {e}")
        return 1
    print(f"Abriendo {url}")
    return 0


def open_orb_command() -> int:
    from .api.control import ControlUnavailable, send_command
    from .tools.base import ToolError
    from .tools.programs import Programs

    try:
        url = send_command("orb-url")
    except ControlUnavailable:
        print("ARTHUR no está en marcha. Arráncalo con: arthur --voz --orb (o como servicio)")
        return 1
    if not url.startswith("http://127.0.0.1:"):
        print(f"⚠ {url}")
        return 1
    if sys.platform == "win32":
        import webbrowser  # Windows: el navegador predeterminado (la dirección ya está comprobada arriba)

        return 0 if webbrowser.open(url) else 1
    try:
        Programs().launch(["xdg-open", url])
    except ToolError as e:
        print(f"⚠ No he podido abrir el navegador: {e}")
        return 1
    return 0


def start_orb(app, confirmer, gestures=None):
    """Arranca el servidor del Orb y abre el navegador. Si falla, ARTHUR sigue sin Orb.
    `gestures`: GestureSwitch para el botón de la cámara (None = sin botón)."""
    from .api.server import OrbServer
    from .security.broker import ConfirmationBroker
    from .tools.base import ToolError
    from .tools.programs import Programs

    try:
        broker = ConfirmationBroker(app.bus)
        server = OrbServer(
            app.bus,
            port=app.config.orb.port,
            assistant_name=app.config.assistant.name,
            get_state=lambda: app.assistant.state,
            broker=broker,
            gestures=gestures,
        )
    except OSError as e:
        print(f"⚠ No puedo arrancar el Orb en el puerto {app.config.orb.port}: {e.strerror}. Sigo sin él.")
        return None
    broker.has_clients = lambda: server.client_count() > 0
    confirmer.broker = broker  # las peticiones por atajo ya se pueden confirmar en el Orb
    server.start()
    print(f"Orb: {server.url}")
    if app.config.orb.open_browser:
        try:
            Programs().launch(["xdg-open", server.url])
        except ToolError as e:
            print(f"⚠ No he podido abrir el navegador ({e}). Abre la dirección de arriba a mano.")
    return server




def devices_command(args) -> int:
    """Alta, lista y baja de aparatos. Se ejecuta en el servidor (docker compose exec core arthur …)."""
    from .core.config import data_dir
    from .security.devices import DeviceError, DeviceRegistry

    registry = DeviceRegistry(data_dir() / "devices.json")
    try:
        if args.dispositivo_nuevo:
            token = registry.add(args.dispositivo_nuevo)
            print(f"Aparato «{args.dispositivo_nuevo}» dado de alta. Su llave (solo se muestra AHORA, guárdala):")
            print(f"\n  {token}\n")
            print("En ese aparato: arthur --emparejar http://SERVIDOR:8770  y pega la llave cuando la pida.")
        elif args.revocar:
            ok = registry.revoke(args.revocar)
            print(f"Acceso de «{args.revocar}» retirado." if ok else f"No hay ningún aparato llamado «{args.revocar}».")
            return 0 if ok else 1
        else:
            names = registry.names()
            print("\n".join(f"  {name}  (desde {created})" for name, created in names) or "  Ningún aparato dado de alta.")
    except DeviceError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return 0


def client_command(args) -> int:
    import getpass

    from .interfaces.core import RemoteCore, RemoteError, load_client, pair, run_remote_client

    if args.emparejar:
        return pair(args.emparejar, ask_secret=getpass.getpass, say=print)
    try:
        url, token = load_client()
    except RemoteError as e:
        print(f"✘ {e}", file=sys.stderr)
        return 2
    # Las «manos» de este aparato (fase S3): lo que el servidor podrá pedirle, con las reglas de su config.
    from .interfaces.core import LocalExecutor, capability_specs
    from .security.policy import Policy
    from .tools import build_registry

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    # En la terminal solo avisos (el resto, a arthur.log); como servicio, todo al registro (journal).
    setup_logging(config.logging, debug=args.debug, service=args.servicio_cliente)
    windows = sys.platform == "win32"  # el aparato Windows (docs/windows.md): sus propias manos, sin cámara ni avisos aún
    gestures = None
    if args.servicio_cliente and config.gestures.enabled and not windows:
        from .interfaces.gestos.watcher import GestureSwitch

        gestures = GestureSwitch()  # la cámara y su burbuja viven en ESTE aparato (fase S7)
    notifications = None
    if args.servicio_cliente and config.notifications.enabled and not windows:
        from .integrations.notifications import NotificationWatcher

        notifications = NotificationWatcher(lambda note: None)  # run_client_service le pone su función de aviso
        try:
            notifications.start()
        except OSError as e:
            logging.getLogger("arthur").warning("No puedo escuchar las notificaciones: %s", e)
            notifications = None
    if windows:
        registry = build_registry(config.security, profile="windows")
    else:
        registry = build_registry(config.security, profile="desktop", gestures=gestures, notifications=notifications)
    executor = LocalExecutor(registry, Policy(config.security), AuditLog(state_dir() / "audit.jsonl"))
    core = RemoteCore(url, token)
    specs = capability_specs(registry, config.security.trusted_apps)
    if args.servicio_cliente:
        from .api.control import default_socket_path
        from .interfaces.voz_cliente import run_client_service

        logging.getLogger("arthur").info("Arrancando ARTHUR %s como aparato del servidor", __version__)
        try:
            return run_client_service(core, config, control_path=default_socket_path(), executor=executor,
                                      specs=specs, gestures=gestures, notifications=notifications)
        finally:
            if notifications:
                notifications.stop()
    voice = None
    if args.voz:  # el micrófono y el altavoz, aquí; oír y hablar, en el servidor (fase S4)
        from .interfaces.voz_cliente import RemoteVoice, make_player

        voice = RemoteVoice(core, player=make_player(volume=config.voice.volume),
                            max_seconds=config.voice.max_record_seconds)
    return run_remote_client(core, executor=executor, voice=voice, specs=specs)


def core_command(config, *, debug: bool = False) -> int:
    """El ARTHUR del servidor: sin escritorio, con la API de aparatos y, si hay arthur-voz, con voz."""
    import os

    from .core.config import data_dir
    from .interfaces.core import run_core
    from .security.broker import RoutingConfirmer
    from .security.confirm import DenyAll
    from .security.devices import DeviceRegistry

    setup_logging(config.logging, debug=debug, service=True)
    log = logging.getLogger("arthur")
    log.info("Arrancando ARTHUR %s en modo servidor (cerebro: %s)", __version__, config.brain.provider)
    confirmer = RoutingConfirmer(DenyAll(), say=lambda text: log.info("%s", text.strip()))
    memory = None
    if config.memory.enabled:
        from .memory.store import MemoryStore

        memory = MemoryStore()
    app = build_app(config, confirmer=confirmer, audit=AuditLog(state_dir() / "audit.jsonl"),
                    memory=memory, profile="core")
    from .integrations.voz import VoiceLink

    try:
        voice = VoiceLink.from_env(os.environ, speed=config.voice.speed)
    except ValueError as e:
        log.error("La voz no se activa: %s", e)
        voice = None
    log.info("Voz del servidor: %s", f"arthur-voz en {voice.url}" if voice else "desactivada (sin ARTHUR_VOZ_URL)")
    from .integrations.push import PushLink, PushNotifier, PushSubscriptions

    devices = DeviceRegistry(data_dir() / "devices.json")
    push = None
    try:
        link = PushLink.from_env(os.environ)
        if link is not None:
            push = PushNotifier(link, PushSubscriptions(data_dir() / "push.json"), devices)
    except ValueError as e:
        log.error("Los avisos al móvil no se activan: %s", e)
    log.info("Avisos al móvil: %s", f"arthur-push en {push.link.url}" if push else "desactivados (sin ARTHUR_PUSH_URL)")
    host = os.environ.get("ARTHUR_CORE_HOST", "127.0.0.1")
    port = int(os.environ.get("ARTHUR_CORE_PORT", "8770"))
    try:
        return run_core(app, devices=devices, host=host, port=port, confirmer=confirmer, voice=voice, push=push)
    finally:
        if memory:
            memory.close()
        log.info("ARTHUR (servidor) detenido")


if __name__ == "__main__":
    sys.exit(main())
