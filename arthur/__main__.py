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
    studio = parser.add_argument_group("Creator Studio (docs/studio.md)")
    studio.add_argument("--studio-clave-youtube", action="store_true",
                        help="guarda la clave de la YouTube Data API (te la pide sin enseñarla)")
    studio.add_argument("--studio-clave-gemini", action="store_true",
                        help="guarda la clave de Gemini (Google AI Studio, capa gratis) que escribe los guiones")
    studio.add_argument("--studio-probar-gemini", action="store_true",
                        help="comprueba la clave de Gemini: qué modelo gratuito usa y una respuesta de prueba")
    studio.add_argument("--studio-investigar", nargs="+", metavar="NICHO",
                        help="compara hasta 5 nichos de YouTube y escribe un informe (gasta cuota de la API)")
    studio.add_argument("--studio-preparar", metavar="PROYECTO",
                        help="lee el guion de un proyecto de vídeo y descarga sus imágenes comprobando la licencia")
    studio.add_argument("--studio-voz", metavar="PROYECTO",
                        help="genera la voz (Piper) y los subtítulos de un proyecto de vídeo")
    studio.add_argument("--studio-montar", metavar="PROYECTO",
                        help="monta el vídeo, la miniatura y la descripción de un proyecto (ffmpeg) y lo comprueba")
    studio.add_argument("--studio-producir", metavar="PROYECTO",
                        help="todo seguido: preparar imágenes, voz y montaje de un proyecto de vídeo")
    studio.add_argument("--studio-corto", metavar="PROYECTO",
                        help="saca el corto vertical (Shorts, Reels, TikTok) de un vídeo ya hecho y lo manda a revisar")
    studio.add_argument("--studio-revisar", metavar="PROYECTO",
                        help="manda el último montaje de un proyecto a la página de revisión")
    studio.add_argument("--studio-web", action="store_true",
                        help="arranca la página de revisión (ARTHUR_STUDIO_HOST / ARTHUR_STUDIO_PORT, por defecto 127.0.0.1:8773)")
    studio.add_argument("--studio-dispositivo-nuevo", metavar="NOMBRE",
                        help="da de alta un aparato para la página de revisión y muestra su llave UNA vez")
    studio.add_argument("--studio-cliente-youtube", metavar="FICHERO", type=Path,
                        help="instala el cliente OAuth (JSON de Google Cloud, «Aplicación de escritorio») para subir vídeos")
    studio.add_argument("--studio-conectar-youtube", choices=("es", "en"),
                        help="da permiso para subir vídeos al canal de ese idioma (se comprueba que es el canal correcto)")
    studio.add_argument("--studio-publicar", metavar="N", type=int,
                        help="sube a YouTube (en privado) el vídeo N, que tienes que haber aprobado en la página")
    studio.add_argument("--studio-piloto-ahora", action="store_true",
                        help="piloto automático UNA vez: Gemini escribe una historia nueva y se producen los vídeos "
                             "(es + en, largo + corto) para revisar. Nunca publica")
    studio.add_argument("--studio-piloto-diario", action="store_true",
                        help="piloto automático cada día (lo usa el servicio studio-piloto; hora en "
                             "ARTHUR_STUDIO_PILOTO_HORA, por defecto 6)")
    apuntes = parser.add_argument_group("Apuntes: fotos o PDF a texto y PDF (docs/apuntes.md)")
    apuntes.add_argument("--apuntes-web", action="store_true",
                         help="arranca la página de apuntes (ARTHUR_APUNTES_HOST / ARTHUR_APUNTES_PORT, "
                              "por defecto 127.0.0.1:8774)")
    apuntes.add_argument("--apuntes-dispositivo-nuevo", metavar="NOMBRE",
                         help="da de alta un aparato para la página de apuntes y muestra su llave UNA vez")
    apuntes.add_argument("--apuntes-clave-gemini", action="store_true",
                         help="guarda la clave de Gemini (aistudio.google.com/apikey) para leer los apuntes")
    apuntes.add_argument("--apuntes-importar", metavar="CARPETA", type=Path,
                         help="importa una carpeta entera (p. ej. la de la uni): subcarpetas = carpetas, PDF y fotos = "
                              "apuntes sin leer todavía; se puede repetir (lo ya importado se salta)")
    studio.add_argument("--studio-dias", type=int, default=90, metavar="N", help="mirar los vídeos de los últimos N días (90)")
    studio.add_argument("--studio-region", default="ES", metavar="XX", help="país de la búsqueda (ES)")
    studio.add_argument("--studio-idioma", default="es", metavar="xx", help="idioma de los vídeos (es)")
    parser.add_argument("--web", nargs="?", const="", metavar="URL",
                        help="abre la web de ARTHUR en el navegador (la primera vez: arthur --web https://…)")
    parser.add_argument("--version", action="version", version=f"arthur {__version__}")
    args = parser.parse_args(argv)

    if args.web is not None:
        return web_command(args.web)
    if args.studio_clave_gemini or args.studio_probar_gemini:
        return studio_gemini_command(probe=args.studio_probar_gemini)
    if args.studio_piloto_ahora or args.studio_piloto_diario:
        return studio_autopilot_command(daily=args.studio_piloto_diario)
    if args.apuntes_web or args.apuntes_dispositivo_nuevo or args.apuntes_clave_gemini or args.apuntes_importar:
        return notes_command(args)
    if args.studio_clave_youtube or args.studio_investigar or args.studio_preparar or args.studio_voz \
            or args.studio_montar or args.studio_producir or args.studio_revisar or args.studio_web \
            or args.studio_dispositivo_nuevo or args.studio_cliente_youtube or args.studio_conectar_youtube \
            or args.studio_publicar or args.studio_corto:
        return studio_command(args)

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


def studio_command(args) -> int:
    """Creator Studio desde la terminal (en el servidor: dentro del contenedor del core)."""
    import getpass

    from .studio import research, youtube

    if args.studio_clave_youtube:
        key = getpass.getpass("Pega la clave de la YouTube Data API (no se verá al escribir) y pulsa Enter: ")
        try:
            youtube.save_key(key)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        print(f"✓ Clave guardada en {youtube.key_file()} (solo la puede leer este usuario).")
        return 0

    from .studio.jobs import JobRunner
    from .studio.store import StudioStore

    if args.studio_preparar:
        return studio_prepare_command(args.studio_preparar)
    if args.studio_voz:
        return studio_voice_command(args.studio_voz)
    if args.studio_montar:
        return studio_montage_command(args.studio_montar)
    if args.studio_revisar:
        return studio_review_command(args.studio_revisar)
    if args.studio_corto:
        return studio_short_command(args.studio_corto)
    if args.studio_web:
        return studio_web_command()
    if args.studio_dispositivo_nuevo:
        return studio_device_command(args.studio_dispositivo_nuevo)
    if args.studio_cliente_youtube:
        from .studio.publish import PublishError, install_client
        try:
            print(f"✓ Cliente OAuth instalado en {install_client(args.studio_cliente_youtube)} (solo lo lee este usuario).")
        except PublishError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        return 0
    if args.studio_conectar_youtube:
        return studio_connect_command(args.studio_conectar_youtube)
    if args.studio_publicar:
        return studio_publish_command(args.studio_publicar)
    if args.studio_producir:
        # Cada paso reutiliza lo que ya esté hecho (imágenes, frases, escenas): repetirlo tras un fallo es barato.
        for step in (studio_prepare_command, studio_voice_command, studio_montage_command, studio_review_command,
                     studio_short_command):
            code = step(args.studio_producir)
            if code:
                return code
        return 0
    try:
        params = research.validate_params({"nichos": args.studio_investigar, "dias": args.studio_dias,
                                           "region": args.studio_region, "idioma": args.studio_idioma})
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    key = youtube.load_key()
    if not key:
        print("✗ Falta la clave de YouTube. Guárdala primero con: arthur --studio-clave-youtube", file=sys.stderr)
        return 1
    store = StudioStore()
    try:
        client = youtube.YouTubeClient(key, store)
        cost = research.estimated_cost(len(params["nichos"]))
        print(f"Cuota de YouTube: quedan {client.left_today()} unidades hoy; esto gasta como mucho {cost} "
              "(menos si se repite algo de las últimas 12 horas).")
        runner = JobRunner(store, {research.KIND: research.research_steps(lambda: client)})
        job = runner.submit(research.KIND, params)
        # Sin hilo: aquí se espera a que termine. Un fallo pasajero deja el trabajo en cola para más tarde.
        runner.run_once()
        job = store.get_job(job.id)
        if job.status != "done":
            print(f"✗ El trabajo #{job.id} ha quedado «{job.status}»: {job.error}", file=sys.stderr)
            return 1
        report = next(s.output for s in store.steps(job.id) if s.name == "informe")["markdown"]
        folder = youtube.studio_dir() / "informes"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f"nichos-{job.id}.md"
        path.write_text(report, encoding="utf-8")
        print(report)
        print(f"Informe guardado en {path}. Cuota gastada hoy: {client.used_today()} unidades.")
        return 0
    finally:
        store.close()


def studio_gemini_command(probe: bool) -> int:
    import getpass

    from .studio import gemini
    from .studio.store import StudioStore

    if not probe:
        key = getpass.getpass("Pega la clave de Gemini (no se verá al escribir) y pulsa Enter: ")
        try:
            gemini.save_gemini_key(key)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        print(f"✓ Clave de Gemini guardada en {gemini.key_file()} (solo la puede leer este usuario).")
        probe = True
    key = gemini.load_gemini_key()
    if not key:
        print("✗ Falta la clave de Gemini: arthur --studio-clave-gemini", file=sys.stderr)
        return 1
    store = StudioStore()
    try:
        client = gemini.GeminiClient(key, store)
        print(f"Modelo gratuito elegido: {client.model}")
        answer = client.generate("Responde solo con una frase corta de Séneca sobre el tiempo, en español.",
                                 temperature=0.2)
        print(f"Respuesta de prueba: {answer[:300]}")
        print(f"✓ Gemini funciona. Peticiones hoy: {client.used_today()} de {client.daily_limit}.")
        return 0
    except gemini.GeminiError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    finally:
        store.close()


def studio_autopilot_command(daily: bool) -> int:
    import os
    import threading
    import time
    from datetime import datetime, timedelta

    from .studio import autopilot
    from .studio.jobs import JobRunner
    from .studio.store import StudioStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = StudioStore()
    try:
        # Más paciencia que la cola normal: Gemini gratis a veces dice «ocupado» un buen rato (visto el 2026-10-13).
        from .studio.notify import Notifier, send

        notifier = Notifier.from_env()
        runner = JobRunner(store, {autopilot.KIND: autopilot.autopilot_steps(store)}, max_attempts=6,
                           backoff=timedelta(minutes=2), max_backoff=timedelta(minutes=30),
                           on_change=lambda job: send(notifier, autopilot.job_notice(store, job) or ""))
        if daily:
            try:
                hour = int(os.environ.get("ARTHUR_STUDIO_PILOTO_HORA", autopilot.DEFAULT_HOUR))
            except ValueError:
                hour = -1
            if not 0 <= hour <= 23:
                print("✗ ARTHUR_STUDIO_PILOTO_HORA debe ser una hora de 0 a 23", file=sys.stderr)
                return 2
            stop = threading.Event()
            # La cola en su hilo (retoma lo que quedara a medias tras un reinicio); aquí, el reloj del día.
            runner.start()
            print(f"Piloto automático: un vídeo nuevo cada día a partir de las {hour}:00 (Ctrl+C para parar)", flush=True)
            try:
                autopilot.daily_loop(store, lambda: runner.submit(autopilot.KIND, {}), stop, hour=hour)
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                runner.stop(timeout=30)
            return 0
        job = runner.submit(autopilot.KIND, {})
        print(f"Piloto automático: trabajo #{job.id}. Tarda un buen rato (guiones, voces y cuatro montajes).", flush=True)
        # Sin hilo: aquí se espera. Un fallo pasajero (Gemini ocupado) lo deja en cola: se reintenta solo, igual
        # que en el servicio, hasta 3 veces.
        while True:
            runner.run_once()
            job = store.get_job(job.id)
            if job.status != "queued":
                break
            wait = max(0.0, (job.next_run_at - datetime.now()).total_seconds())
            print(f"… fallo pasajero ({job.error}); reintento en {wait:.0f} s", flush=True)
            time.sleep(wait)
        if job.status != "done":
            print(f"✗ El piloto ha quedado «{job.status}»: {job.error}", file=sys.stderr)
            return 1
        outputs = {s.name: s.output or {} for s in store.steps(job.id)}
        print(f"✓ «{outputs['guion']['titulo']}» listo para revisar en la página "
              f"({outputs['guion']['avisos']} avisos del revisor).")
        return 0
    finally:
        store.close()


def studio_prepare_command(name: str) -> int:
    from .studio.assets import AssetError, CommonsClient
    from .studio.project import ScriptError, full_description, prepare, project_dir
    from .studio.store import StudioStore

    try:
        folder = project_dir(name)
    except ScriptError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    store = StudioStore()
    try:
        result = prepare(name, store, CommonsClient(store))
    except (ScriptError, AssetError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    script = result.script
    print(f"Guion: «{script.titulo}» · {len(script.escenas)} escenas · {script.words} palabras "
          f"(unos {script.minutes:.1f} minutos de voz) · {len(script.fuentes)} fuentes")
    for asset in result.assets:
        mark = "✓" if asset.status == "ok" else "✗"
        print(f"  {mark} {asset.title} · {asset.license} · {asset.reason or ''}")
    if not result.ready:
        print(f"✗ {len(result.blocked)} imágenes bloqueadas: cámbialas en {folder / 'guion.toml'} y repite.",
              file=sys.stderr)
        return 1
    print("\nDescripción para YouTube:\n" + full_description(script, result.assets))
    print(f"\n✓ Todo listo en {folder}")
    return 0


def studio_voice_command(name: str) -> int:
    import time

    from .studio.project import ScriptError, load_script, project_dir
    from .studio.voice import VoiceError, engine_for, narrate, write_srt

    try:
        folder = project_dir(name)
        script = load_script(folder / "guion.toml")
        started = time.monotonic()
        engine = engine_for(script.voz, script.idioma)
        print(f"Voz: {engine.id.split('|')[0]}", flush=True)
        narration = narrate(script, folder, engine)
        srt = write_srt(narration, folder / "salida" / "subtitulos.srt")
    except (ScriptError, VoiceError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    minutes, seconds = divmod(round(narration.duration), 60)
    print(f"✓ Voz: {narration.path} ({minutes} min {seconds} s, {len(narration.chunks)} frases, "
          f"hecha en {time.monotonic() - started:.0f} s)")
    print(f"✓ Subtítulos: {srt}")
    return 0


def studio_montage_command(name: str) -> int:
    import time

    from .studio.montage import MontageError, produce
    from .studio.project import ScriptError, load_script, project_dir
    from .studio.store import StudioStore

    store = StudioStore()
    try:
        folder = project_dir(name)
        script = load_script(folder / "guion.toml")
        started = time.monotonic()
        report = produce(script, store.assets(name), folder, progress=lambda step: print(f"  · {step}", flush=True))
    except (ScriptError, MontageError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"Hecho en {(time.monotonic() - started) / 60:.1f} min.")
    print(f"  Vídeo:      {report['video']} ({report['duracion']:.0f} s, {report['ancho']}x{report['alto']}, "
          f"{report['lufs']} LUFS)")
    print(f"  Miniatura:  {report['miniatura']}")
    print(f"  Subtítulos: {folder / 'salida' / 'subtitulos.srt'}")
    if not report["ok"]:
        print("✗ El control de calidad ha encontrado problemas:\n  " + "\n  ".join(report["problemas"]),
              file=sys.stderr)
        return 1
    print("✓ Control de calidad superado.")
    return 0


def studio_review_command(name: str) -> int:
    from .studio.project import ScriptError
    from .studio.review import ReviewBook, ReviewError
    from .studio.store import StudioStore

    store = StudioStore()
    try:
        review = ReviewBook(store).submit(name)
    except (ReviewError, ScriptError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"✓ Vídeo #{review.id} «{review.title}» esperando tu revisión (canal {review.channel}).")
    return 0


def studio_short_command(name: str) -> int:
    import time

    from .studio.montage import MontageError
    from .studio.project import ScriptError, load_script, project_dir
    from .studio.review import ReviewBook, ReviewError
    from .studio.shorts import produce_short
    from .studio.store import StudioStore

    store = StudioStore()
    try:
        folder = project_dir(name)
        script = load_script(folder / "guion.toml")
        started = time.monotonic()
        report = produce_short(script, store.assets(name), folder, progress=lambda step: print(f"  · {step}", flush=True))
        print(f"Corto hecho en {(time.monotonic() - started) / 60:.1f} min: {report['video']} "
              f"({report['duracion']:.0f} s, {report['ancho']}x{report['alto']}, {report['lufs']} LUFS, "
              f"escenas {report['escenas']})")
        if not report["ok"]:
            print("✗ El control de calidad ha encontrado problemas:\n  " + "\n  ".join(report["problemas"]),
                  file=sys.stderr)
            return 1
        review = ReviewBook(store).submit(name, "corto")
    except (ScriptError, MontageError, ReviewError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"✓ Corto #{review.id} «{review.title}» esperando tu revisión (canal {review.channel}).")
    return 0


def notes_command(args) -> int:
    """Apuntes desde la terminal (en el servidor: dentro de su contenedor)."""
    import getpass
    import os

    from .notes import engines
    from .notes.store import notes_dir

    key_file = notes_dir() / "gemini-key"
    if args.apuntes_clave_gemini:
        key = getpass.getpass("Pega la clave de Gemini (no se verá al escribir) y pulsa Enter: ")
        try:
            engines.save_key(key, key_file)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        print(f"✓ Clave guardada en {key_file} (solo la puede leer este usuario).")
        return 0

    from .security.devices import DeviceError, DeviceRegistry
    from .notes.web import NotesWeb, devices_file

    if args.apuntes_dispositivo_nuevo:
        try:
            key = DeviceRegistry(devices_file()).add(args.apuntes_dispositivo_nuevo)
        except DeviceError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        print(f"Llave de «{args.apuntes_dispositivo_nuevo}» para la página de apuntes "
              f"(se enseña SOLO esta vez; pégala al entrar):\n\n{key}\n")
        return 0

    from .notes import render
    from .notes.store import NotesStore
    from .notes.worker import Worker

    if args.apuntes_importar:
        from .notes.importer import import_tree

        folder = args.apuntes_importar.expanduser()
        if not folder.is_dir():
            print(f"✗ No existe la carpeta {folder}", file=sys.stderr)
            return 1
        store = NotesStore()
        try:
            report = import_tree(store, folder)
        finally:
            store.close()
        print(f"✓ {len(report.imported)} apuntes nuevos importados; {report.already} ya estaban.")
        if report.unsupported:
            print(f"  {len(report.unsupported)} ficheros no admitidos (solo PDF, JPEG y PNG), se han dejado: "
                  + ", ".join(sorted({Path(r).suffix.lower() or "sin extensión" for r in report.unsupported})))
        for rel in report.too_big:
            print(f"  Demasiado grande, no importado: {rel}")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for program in ("pandoc", "typst", "ocrmypdf"):
        if not render.available(program):
            print(f"Aviso: falta «{program}»: " + ("no habrá PDF escaneado" if program == "ocrmypdf"
                                                   else "no se podrá hacer el PDF limpio"), file=sys.stderr)
    store = NotesStore()
    worker = Worker(store, {"gemini": lambda: engines.GeminiEngine(engines.load_key(key_file)),
                            "local": engines.LocalEngine},
                    auto_engine=lambda: "gemini" if engines.load_key(key_file) else "local")
    web = NotesWeb(store, DeviceRegistry(devices_file()), worker,
                   os.environ.get("ARTHUR_APUNTES_HOST", "127.0.0.1"), int(os.environ.get("ARTHUR_APUNTES_PORT", "8774")),
                   gemini_ready=lambda: engines.load_key(key_file) is not None,
                   tutor_engine=lambda: engines.GeminiEngine(engines.load_key(key_file)))
    worker.start()
    print(f"Página de apuntes en {web.url} (Ctrl+C para parar)", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.shutdown()
        store.close()
    return 0


def studio_device_command(name: str) -> int:
    from .security.devices import DeviceError, DeviceRegistry
    from .studio.web import devices_file

    try:
        key = DeviceRegistry(devices_file()).add(name)
    except DeviceError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"Llave de «{name}» para la página de revisión (se enseña SOLO esta vez; pégala al entrar):\n\n{key}\n")
    return 0


def studio_web_command() -> int:
    import os

    from .security.devices import DeviceRegistry
    from .studio.review import ReviewBook
    from .studio.store import StudioStore
    from .studio.web import StudioWeb, devices_file

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("ARTHUR_STUDIO_HOST", "127.0.0.1")
    port = int(os.environ.get("ARTHUR_STUDIO_PORT", "8773"))
    from .studio.publish import publish
    from .studio.web import Publisher

    store = StudioStore()
    book = ReviewBook(store)
    from .studio.notify import Notifier, send

    notifier = Notifier.from_env()
    publisher = Publisher(book, lambda b, review_id: publish(b, review_id, notify=lambda text: send(notifier, text)))
    web = StudioWeb(book, DeviceRegistry(devices_file()), host, port, publisher)
    publisher.start()
    import threading

    from .studio import autopilot

    # Publicar solo lo del piloto sin dudas, a su hora (docs/studio.md). «no» lo apaga.
    hour = os.environ.get("ARTHUR_STUDIO_PUBLICAR_HORA", str(autopilot.PUBLISH_HOUR)).strip().lower()
    stop = threading.Event()
    if hour != "no":
        if not hour.isdigit() or not 0 <= int(hour) <= 23:
            print("✗ ARTHUR_STUDIO_PUBLICAR_HORA debe ser una hora de 0 a 23 (o «no»)", file=sys.stderr)
            return 2
        threading.Thread(target=autopilot.auto_publish_loop, args=(book, publisher.enqueue, stop),
                         kwargs={"hour": int(hour)}, name="studio-autopublicar", daemon=True).start()
        print(f"Lo del piloto sin dudas se publica solo a partir de las {int(hour)}:00", flush=True)
    print(f"Página de revisión en {web.url} (Ctrl+C para parar)", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        web.shutdown()
        store.close()
    return 0


def studio_connect_command(language: str) -> int:
    import json
    import os

    from .integrations.google import GoogleError
    from .studio.publish import SCOPES, PublishError, YouTubeUploader, auth_for, channel_file
    from .studio.review import CHANNELS

    expected = CHANNELS[language]
    headless = not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def open_browser(url: str) -> None:
        print(f"\n1) Abre esta dirección en tu navegador, entra con la cuenta del canal y elige {expected}:\n\n{url}\n")
        if headless:
            print("2) Al final irá a una página de 127.0.0.1 que NO carga: es normal. Copia su dirección completa.\n")

    print(f"Permisos que se piden para {expected}: SUBIR vídeos y VER los datos del canal "
          "(no puede borrar ni cambiar vídeos).")
    auth = auth_for(language)
    try:
        auth.authorize(SCOPES, open_browser, paste=input if headless else None)
        channel = YouTubeUploader(language, auth=auth).check_channel()
    except (GoogleError, PublishError) as e:
        auth.disconnect()  # un permiso a medias o del canal equivocado no se guarda
        print(f"✗ {e}", file=sys.stderr)
        return 1
    path = channel_file(language)
    path.write_text(json.dumps({"id": channel["id"], "handle": channel["handle"], "title": channel["title"]},
                               ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    print(f"✓ Conectado al canal {channel['handle']} («{channel['title']}»). Para retirar el permiso: "
          "https://myaccount.google.com/permissions")
    return 0


def studio_publish_command(review_id: int) -> int:
    from .studio.publish import PublishError, publish
    from .studio.review import ReviewBook
    from .studio.store import StudioStore

    store = StudioStore()
    try:
        video_id, warning = publish(ReviewBook(store), review_id, progress=lambda step: print(f"  · {step}", flush=True))
    except PublishError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"✓ Subido en PRIVADO: https://studio.youtube.com/video/{video_id}/edit")
    if warning:
        print(f"⚠ {warning}")
    return 0


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
    from .tools.services import ServiceLink

    services = {}
    for name, prefix, secret in (("Studio", "ARTHUR_STUDIO", "studio_core"),
                                 ("Apuntes", "ARTHUR_APUNTES", "apuntes_core")):
        try:
            services[name] = ServiceLink.from_env(os.environ, name, prefix, secret)
        except ValueError as e:
            log.error("%s no se conecta al chat: %s", name, e)
            services[name] = None
        log.info("%s en el chat: %s", name, services[name].url if services[name] else f"no (sin {prefix}_URL)")
    app = build_app(config, confirmer=confirmer, audit=AuditLog(state_dir() / "audit.jsonl"),
                    memory=memory, profile="core", studio=services["Studio"], apuntes=services["Apuntes"])
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
