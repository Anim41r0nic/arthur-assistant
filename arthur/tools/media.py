"""media: controlar la música o el vídeo que suena (Spotify, YouTube en Firefox…)."""

import urllib.parse

from ..integrations.media import ADJUSTMENTS, COMMANDS, MediaPlayer
from .base import Param, Risk, Tool, ToolDenied, ToolError

ACTIONS = (*COMMANDS, *ADJUSTMENTS, "status")
DONE = {"play": "Le doy al play", "pause": "Pausado", "toggle": "Hecho", "next": "Siguiente canción",
        "previous": "Canción anterior", "volume_up": "Volumen de la música subido",
        "volume_down": "Volumen de la música bajado", "forward": "Adelantado 10 segundos",
        "back": "Atrasado 10 segundos"}

# Direcciones FIJAS: del modelo solo viene el texto a buscar, siempre codificado (sin poder colar «:» ni «/»).
SEARCH_URLS = {
    "spotify": "spotify:search:{q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
}
MAX_QUERY = 100


def build_media_tools(player: MediaPlayer) -> list[Tool]:
    def check(args: dict) -> None:
        if args["action"] not in ACTIONS:
            raise ToolDenied(f"Acción desconocida: {args['action']!r} (usa {', '.join(ACTIONS)})")

    def media(args: dict) -> str:
        action = args["action"]
        if player.status() is None:
            raise ToolError("No hay ningún reproductor abierto (abre Spotify o un vídeo primero)")
        if action == "status":
            return player.now_playing()
        if action in ADJUSTMENTS:  # no depende de si ARTHUR habla: se aplica ya
            player.adjust(action)
            return DONE[action]
        # Durante una conversación se aplica cuando ARTHUR termine de hablar; fuera de ella (gestos), ya.
        if not (player.ducker and player.ducker.request(action)):
            player.command(action)
        return DONE[action]

    def check_search(args: dict) -> None:
        if args["service"] not in SEARCH_URLS:
            raise ToolDenied(f"Servicio desconocido: {args['service']!r} (usa {', '.join(SEARCH_URLS)})")
        query = args["query"].strip()
        if not query or not query.isprintable():
            raise ToolDenied("Falta qué buscar (sin saltos de línea ni caracteres raros)")

    def search_music(args: dict) -> str:
        service, query = args["service"], args["query"].strip()
        url = SEARCH_URLS[service].format(q=urllib.parse.quote(query, safe=""))
        try:
            player.programs.launch(["xdg-open", url])
        except ToolError as e:
            hint = " (¿está instalado spotify-launcher?)" if service == "spotify" else ""
            raise ToolError(f"No he podido abrir la búsqueda en {service}{hint}: {e}") from e
        return (f"He abierto la búsqueda de «{query}» en {service.capitalize()}. "
                "Elige tú qué suena: ARTHUR no le da al play por su cuenta.")

    return [
        Tool(
            name="media",
            description="Música/vídeo que suena (Spotify, YouTube…): play, pause, toggle, next, previous, "
                        "volume_up/volume_down (volumen de la música), forward/back (±10 s) "
                        "o status (qué suena).",
            risk=Risk.LOW,
            params=(Param("action", str, "", max_length=10),),
            check=check,
            run=media,
        ),
        Tool(
            name="search_music",
            description="Abre la búsqueda de una canción/artista/lista en Spotify (service=spotify) o YouTube "
                        "(service=youtube). No reproduce: eso lo eliges tú.",
            risk=Risk.LOW,
            params=(
                Param("query", str, "", max_length=MAX_QUERY),
                Param("service", str, "", required=False, default="spotify", max_length=10),
            ),
            check=check_search,
            run=search_music,
        ),
    ]
