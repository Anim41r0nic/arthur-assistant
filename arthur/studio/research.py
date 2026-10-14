"""Investigador de Creator Studio (paso 2): compara nichos de YouTube con datos públicos y los puntúa.

Un trabajo «investigar» recibe hasta 5 nichos (nombre + lo que se buscaría en YouTube) y hace estos pasos,
cada uno guardado en studio.sqlite3 para que un corte no obligue a repetir (ni a gastar cuota otra vez):

  buscar      → los 50 vídeos con más visitas de cada nicho en los últimos N días, en DOS búsquedas:
                cortos (menos de 4 min) y largos (4 a 20 min), porque no se pueden comparar  (200 unidades por nicho)
  videos      → visitas, me gusta, comentarios y duración de esos vídeos               (1 unidad por cada 50)
  canales     → suscriptores de los canales que los publicaron                         (1 unidad por cada 50)
  referencias → lo normal de cada canal: la mediana de sus últimos vídeos              (~1 unidad por canal)
  informe     → cuentas, puntuación y el informe escrito

Honestidad (docs/studio.md): cada dato del informe dice si es
  observado  → lo publica YouTube tal cual (visitas, suscriptores; estos últimos YouTube los redondea),
  estimado   → una cuenta hecha con datos observados (visitas al día, «cuántas veces lo normal»),
  hipótesis  → una interpretación nuestra que puede fallar (la puntuación del nicho).
Lo que YouTube NO publica (ingresos, RPM, clics en la miniatura, retención, búsquedas) no se inventa.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from math import log10
from typing import Any

from .jobs import Step, StepContext, StepFailed
from .youtube import YouTubeClient, YouTubeRefused

KIND = "investigar"
MAX_NICHES = 5
CHANNELS_PER_NICHE = 30    # canales a los que se les mira «lo normal» (los de los vídeos con más visitas)
BASELINE_UPLOADS = 10      # últimos vídeos de cada canal para calcular su mediana
BASELINE_MIN_AGE = 7       # días: un vídeo más nuevo aún no ha tenido tiempo de sumar visitas
SHORT_SECONDS = 180        # los Shorts pueden durar hasta 3 minutos
OUTLIER_RATIO = 3.0        # «destaca» = al menos 3 veces lo normal de su canal
MIN_BASELINE = 1_000       # si lo normal de un canal son menos visitas, «veces lo normal» da cifras absurdas (×15.000)
# Un Short de 40 s y un vídeo de 15 min no compiten en lo mismo ni pagan lo mismo: cada uno con su búsqueda y su nota.
FORMATS = {"largos": ("medium", "largos (4-20 min)"), "cortos": ("short", "cortos (menos de 4 min)")}
SMALL_CHANNEL = 100_000    # suscriptores: por debajo, «canal pequeño»
NO_LANGUAGE = {"zxx", "und", "mul"}  # códigos ISO: sin contenido hablado, sin determinar, varios
REGION = re.compile(r"^[A-Z]{2}$")
LANGUAGE = re.compile(r"^[a-z]{2}$")

LEGEND = ("**observado**: lo publica YouTube tal cual · **estimado**: cuenta hecha con datos observados · "
          "**hipótesis**: interpretación nuestra, puede fallar")
UNKNOWN = ("YouTube no publica ingresos ni RPM, clics en la miniatura (CTR), retención ni volumen de búsquedas: "
           "este informe no los estima.")


def validate_params(params: dict[str, Any]) -> dict[str, Any]:
    """Comprueba y normaliza lo que se pide. Lanza ValueError con un mensaje para Marc."""
    niches = params.get("nichos")
    if not isinstance(niches, list) or not 1 <= len(niches) <= MAX_NICHES:
        raise ValueError(f"Hay que dar entre 1 y {MAX_NICHES} nichos")
    clean = []
    for niche in niches:
        if isinstance(niche, str):
            niche = {"nombre": niche, "busqueda": niche}
        name = str(niche.get("nombre", "")).strip()[:60] if isinstance(niche, dict) else ""
        query = str(niche.get("busqueda", name)).strip()[:100] if isinstance(niche, dict) else ""
        if not name or not query:
            raise ValueError("Cada nicho necesita un nombre y lo que se buscaría en YouTube")
        clean.append({"nombre": name, "busqueda": query})
    if len({n["busqueda"].lower() for n in clean}) != len(clean):
        raise ValueError("Hay nichos repetidos")
    days = params.get("dias", 90)
    if not isinstance(days, int) or isinstance(days, bool) or not 7 <= days <= 365:
        raise ValueError("Los días deben estar entre 7 y 365")
    region = str(params.get("region", "ES"))
    language = str(params.get("idioma", "es"))
    if not REGION.match(region) or not LANGUAGE.match(language):
        raise ValueError("Región (p. ej. ES) o idioma (p. ej. es) no válidos")
    return {"nichos": clean, "dias": days, "region": region, "idioma": language}


def estimated_cost(niche_count: int) -> int:
    """Unidades de cuota que puede gastar, como mucho, investigar `niche_count` nichos (sin contar la caché)."""
    per_niche = 100 * len(FORMATS) + 2 + 2 + CHANNELS_PER_NICHE + (CHANNELS_PER_NICHE * BASELINE_UPLOADS) // 50 + 1
    return per_niche * niche_count


# --- los pasos ---------------------------------------------------------------

def research_steps(client: Callable[[], YouTubeClient], now: Callable[[], datetime] = datetime.now) -> tuple[Step, ...]:
    """`client` se llama al ejecutar cada paso: la clave se lee entonces y nunca se guarda en el trabajo."""

    def guarded(run: Callable[[StepContext], dict[str, Any]]) -> Callable[[StepContext], dict[str, Any]]:
        def wrapper(ctx: StepContext) -> dict[str, Any]:
            try:
                return run(ctx)
            except (YouTubeRefused, ValueError) as error:
                # Clave mala, API sin activar, cuota agotada, nicho mal escrito: repetirlo daría lo mismo.
                raise StepFailed(str(error)) from None
        return wrapper

    def buscar(ctx: StepContext) -> dict[str, Any]:
        params = validate_params(ctx.job.params)
        yt = client()
        since = now() - timedelta(days=params["dias"])
        found = []
        for niche in params["nichos"]:
            formats = {name: yt.search_videos(niche["busqueda"], since, region=params["region"],
                                              language=params["idioma"], duration=duration)
                       for name, (duration, _) in FORMATS.items()}
            found.append({**niche, "formatos": formats})
        return {"nichos": found}

    def videos(ctx: StepContext) -> dict[str, Any]:
        ids = [i for niche in ctx.outputs["buscar"]["nichos"] for i in _all_ids(niche)]
        return {"videos": client().videos(ids)}

    def canales(ctx: StepContext) -> dict[str, Any]:
        ids = [v["channel_id"] for v in ctx.outputs["videos"]["videos"] if v.get("channel_id")]
        return {"canales": client().channels(ids)}

    def referencias(ctx: StepContext) -> dict[str, Any]:
        yt = client()
        by_id = {v["id"]: v for v in ctx.outputs["videos"]["videos"]}
        channels = {c["id"]: c for c in ctx.outputs["canales"]["canales"]}
        chosen: list[str] = []
        for niche in ctx.outputs["buscar"]["nichos"]:
            ranked = sorted((by_id[i] for i in _all_ids(niche) if i in by_id),
                            key=lambda v: v.get("views") or 0, reverse=True)
            picked: list[str] = []
            for video in ranked:
                cid = video.get("channel_id")
                if cid in channels and cid not in picked:
                    picked.append(cid)
                if len(picked) == CHANNELS_PER_NICHE:
                    break
            chosen += [c for c in picked if c not in chosen]
        uploads = {cid: yt.recent_uploads(channels[cid].get("uploads") or "", BASELINE_UPLOADS) for cid in chosen}
        stats = {v["id"]: v for v in yt.videos([i for ids in uploads.values() for i in ids])}
        old_enough = _aware(now()) - timedelta(days=BASELINE_MIN_AGE)
        medians = {}
        for cid, ids in uploads.items():
            views = []
            for vid in ids:
                video = stats.get(vid)
                published = _parse_time(video.get("published_at")) if video else None
                if video and video.get("views") is not None and published and published <= old_enough:
                    views.append(video["views"])
            if len(views) >= 3:  # con menos no hay «lo normal» que valga
                medians[cid] = {"mediana": int(statistics.median(views)), "muestra": len(views)}
        return {"medianas": medians}

    def informe(ctx: StepContext) -> dict[str, Any]:
        params = validate_params(ctx.job.params)
        result = analyze(ctx.outputs["buscar"]["nichos"], ctx.outputs["videos"]["videos"],
                         ctx.outputs["canales"]["canales"], ctx.outputs["referencias"]["medianas"], now(),
                         language=params["idioma"])
        result["parametros"] = params
        result["markdown"] = render_report(result)
        ctx.cost_eur = 0.0  # la API de YouTube es gratuita: se gasta cuota, no dinero
        return result

    return (Step("buscar", guarded(buscar)), Step("videos", guarded(videos)), Step("canales", guarded(canales)),
            Step("referencias", guarded(referencias)), Step("informe", guarded(informe)))


# --- las cuentas (sin red: se pueden probar con datos fijos) --------------------

def analyze(niches: list[dict[str, Any]], videos: list[dict[str, Any]], channels: list[dict[str, Any]],
            medians: dict[str, dict[str, int]], now: datetime, *, language: str = "es") -> dict[str, Any]:
    by_video = {v["id"]: v for v in videos}
    by_channel = {c["id"]: c for c in channels}
    out = []
    for niche in niches:
        entry = {"nombre": niche["nombre"], "busqueda": niche["busqueda"], "formatos": {}, "otros_idiomas": []}
        for name, search in niche["formatos"].items():
            rows = []
            for vid in search["ids"]:
                video = by_video.get(vid)
                if not video or video.get("views") is None:
                    continue
                row = _row(video, by_channel.get(video.get("channel_id"), {}),
                           medians.get(video.get("channel_id"), {}).get("mediana"), now)
                declared = (video.get("language") or "").lower()
                if declared in NO_LANGUAGE:
                    declared = ""  # «sin idioma» o «sin determinar»: como si no lo hubiera puesto
                if declared and declared.split("-")[0] != language:
                    # No se tira: un vídeo que funciona en otro idioma también es una pista (¿adaptarlo?).
                    # Pero no entra en la nota, que es «este nicho EN ESTE IDIOMA».
                    entry["otros_idiomas"].append({**row, "idioma": declared, "formato": name})
                    continue
                rows.append(row)
            entry["formatos"][name] = {"fecha": search.get("fetched_at"),
                                       "total_estimado_youtube": search.get("total_estimated"),
                                       **_score(rows), "videos": rows}
        out.append(entry)
    return {"nichos": out, "generado": _aware(now).replace(microsecond=0).isoformat()}


def _row(video: dict[str, Any], channel: dict[str, Any], baseline: int | None, now: datetime) -> dict[str, Any]:
    published = _parse_time(video.get("published_at"))
    age = max(1.0, (_aware(now) - published).total_seconds() / 86400) if published else None
    usable = baseline is not None and baseline >= MIN_BASELINE
    return {
        "id": video["id"], "titulo": video["title"], "canal": video.get("channel_title", ""),
        "visitas": video["views"],                          # observado
        "suscriptores": channel.get("subscribers"),         # observado (redondeado por YouTube)
        "short": video.get("seconds") is not None and video["seconds"] <= SHORT_SECONDS,
        "visitas_dia": round(video["views"] / age) if age else None,                 # estimado
        "veces_lo_normal": round(video["views"] / baseline, 1) if usable else None,  # estimado
        "lo_normal_del_canal": baseline,                                              # estimado (mediana)
        "publicado": video.get("published_at"),
    }


def _score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Puntuación 0-100 del nicho (hipótesis). La fórmula va escrita en el informe para que se pueda discutir.

      demanda  (40) → mediana de visitas al día de los vídeos más vistos; 10.000/día o más = 40 puntos
      hueco    (30) → parte de esos vídeos que son de canales pequeños (< 100.000 suscriptores)
      sorpresa (30) → parte de vídeos que hacen 3 veces o más lo normal de su canal; la mitad o más = 30
    """
    if len(rows) < 5:
        return {"puntuacion": {"valor": None, "tipo": "hipótesis",
                               "motivo": "Menos de 5 vídeos con datos: no basta para opinar"},
                "metricas": {}}
    per_day = [r["visitas_dia"] for r in rows if r["visitas_dia"] is not None]
    known_subs = [r for r in rows if r["suscriptores"] is not None]
    with_ratio = [r for r in rows if r["veces_lo_normal"] is not None]
    demand = statistics.median(per_day) if per_day else 0
    small = sum(1 for r in known_subs if r["suscriptores"] < SMALL_CHANNEL) / len(known_subs) if known_subs else None
    outliers = (sum(1 for r in with_ratio if r["veces_lo_normal"] >= OUTLIER_RATIO) / len(with_ratio)
                if with_ratio else None)
    points_demand = 40 * min(1.0, log10(demand + 1) / 4)
    points_gap = 30 * (small or 0)
    points_surprise = 30 * min(1.0, (outliers or 0) / 0.5)
    return {
        "puntuacion": {"valor": round(points_demand + points_gap + points_surprise), "tipo": "hipótesis",
                       "partes": {"demanda": round(points_demand), "hueco": round(points_gap),
                                  "sorpresa": round(points_surprise)}},
        "metricas": {
            "videos": {"valor": len(rows), "tipo": "observado"},
            "shorts": {"valor": sum(1 for r in rows if r["short"]), "tipo": "observado"},
            "mediana_visitas_dia": {"valor": round(demand), "tipo": "estimado"},
            "parte_canales_pequenos": {"valor": None if small is None else round(small, 2), "tipo": "estimado",
                                       "sobre": len(known_subs)},
            "parte_destacados": {"valor": None if outliers is None else round(outliers, 2), "tipo": "estimado",
                                 "sobre": len(with_ratio)},
        },
    }


# --- el informe escrito --------------------------------------------------------

def render_report(result: dict[str, Any]) -> str:
    params = result.get("parametros", {})
    lines = [
        "# Informe de nichos de YouTube", "",
        f"Generado: {result['generado']} · últimos {params.get('dias', '?')} días · región {params.get('region', '?')} "
        f"· idioma {params.get('idioma', '?')}",
        "Fuente: YouTube Data API v3 (search.list, videos.list, channels.list, playlistItems.list).", "",
        LEGEND, "",
    ]
    for name, (_, label) in FORMATS.items():
        ranked = sorted(result["nichos"], key=lambda n: _value(n["formatos"][name]), reverse=True)
        lines += [f"## Comparación: vídeos {label}", "",
                  "| Nicho | Puntuación (hipótesis) | Visitas/día, mediana (estimado) | Canales pequeños (estimado) "
                  "| Destacados (estimado) | Vídeos (observado) |",
                  "|---|---|---|---|---|---|"]
        for niche in ranked:
            fmt = niche["formatos"][name]
            if fmt["puntuacion"]["valor"] is None:
                lines.append(f"| {_md(niche['nombre'])} | sin datos | | | | {len(fmt['videos'])} |")
                continue
            m = fmt["metricas"]
            lines.append(
                f"| {_md(niche['nombre'])} | **{fmt['puntuacion']['valor']}**/100 "
                f"| {_num(m['mediana_visitas_dia']['valor'])} | {_pct(m['parte_canales_pequenos']['valor'])} "
                f"| {_pct(m['parte_destacados']['valor'])} | {m['videos']['valor']} |")
        lines.append("")
    lines += ["Puntuación = demanda (hasta 40: 10.000 visitas/día de mediana o más) + hueco (hasta 30: parte de "
              "vídeos de canales con menos de 100.000 suscriptores) + sorpresa (hasta 30: parte de vídeos que hacen "
              "3 veces o más lo normal de su canal; la mitad o más da los 30). «Lo normal» es la mediana de los "
              f"últimos {BASELINE_UPLOADS} vídeos del canal; si son menos de {_num(MIN_BASELINE)} visitas, no se "
              "compara. Los cortos suelen tener muchas más visitas y pagar mucho menos que los largos: compara cada "
              "formato con el suyo. Es una guía para decidir qué mirar primero, no una predicción.",
              "", "Ojo: al ordenar por visitas, YouTube mete vídeos que solo llevan la palabra o la etiqueta y no son "
              "del tema. Mira los enlaces antes de decidir.", ""]
    for niche in result["nichos"]:
        lines += [f"## {_md(niche['nombre'])}", "", f"Búsqueda: «{_md(niche['busqueda'])}»", ""]
        for name, (_, label) in FORMATS.items():
            fmt = niche["formatos"][name]
            if fmt["puntuacion"]["valor"] is None:
                lines += [f"**Vídeos {label}:** {fmt['puntuacion']['motivo'].lower()}.", ""]
                continue
            parts = fmt["puntuacion"]["partes"]
            lines += [f"**Vídeos {label}:** {fmt['puntuacion']['valor']}/100 (hipótesis): demanda "
                      f"{parts['demanda']}, hueco {parts['hueco']}, sorpresa {parts['sorpresa']} · datos pedidos: "
                      f"{fmt['fecha']}", ""]
            top = sorted((v for v in fmt["videos"] if v["veces_lo_normal"] is not None),
                         key=lambda v: v["veces_lo_normal"], reverse=True)[:5]
            if top:
                lines += ["| Vídeo | Canal | Suscriptores (observado) | Visitas (observado) "
                          "| Veces lo normal (estimado) |", "|---|---|---|---|---|"]
                for v in top:
                    lines.append(f"| {_link(v)} | {_md(v['canal'])} | {_num(v['suscriptores'])} "
                                 f"| {_num(v['visitas'])} | ×{v['veces_lo_normal']} |")
                lines.append("")
        others = sorted(niche["otros_idiomas"], key=lambda v: v["visitas"], reverse=True)
        if others:
            lines += [f"**En otros idiomas** (no cuentan en la nota; {len(others)} vídeo{'s' if len(others) != 1 else ''}):", "",
                      "| Vídeo | Idioma | Canal | Visitas (observado) |", "|---|---|---|---|"]
            for v in others[:5]:
                lines.append(f"| {_link(v)} | {v['idioma']} | {_md(v['canal'])} | {_num(v['visitas'])} |")
            lines.append("")
    lines += ["## Lo que no sabemos", "", UNKNOWN,
              "Los vídeos que no declaran su idioma se cuentan como del idioma pedido.", ""]
    return "\n".join(lines)


def _value(fmt: dict[str, Any]) -> int:
    return -1 if fmt["puntuacion"]["valor"] is None else fmt["puntuacion"]["valor"]


def _link(video: dict[str, Any]) -> str:
    kind = " (Short)" if video["short"] else ""
    return f"[{_md(video['titulo'])}](https://youtu.be/{video['id']}){kind}"


def _all_ids(niche: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(i for search in niche["formatos"].values() for i in search["ids"]))


def _md(text: str) -> str:
    """Que un título con «|», corchetes o saltos de línea no rompa la tabla ni el enlace."""
    return str(text).replace("|", "/").replace("[", "(").replace("]", ")").replace("\n", " ").strip()


def _num(value: int | None) -> str:
    return "oculto" if value is None else f"{value:,}".replace(",", ".")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)} %"


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.astimezone()


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
