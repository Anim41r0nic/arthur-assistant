"""Reenviar al cerebro desde el panel y desde Apuntes («Pregunta a tu cerebro», la nota diaria del portátil, la
pestaña Repaso de Apuntes y la línea del Repaso en el panel).

El cerebro no tiene puerto fuera de la red de Docker y el navegador nunca habla con él: la página de siempre recibe
la petición (con su cookie, X-Arthur y Origin, como sus otras rutas) y la pasa al cerebro con SU llave de aparato.

Lo que vuelve se rehace con solo los campos que la página pinta y con el tipo esperado: si el cerebro devolviera
algo raro (un campo de más, una cita sin título), no llega tal cual al navegador. La página pinta el texto con
textContent y solo enlaza direcciones obsidian://, http:// o https://.
"""

from __future__ import annotations

import http.client
import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import date

from ..tools.base import ToolError

log = logging.getLogger(__name__)

MAX_QUESTION = 500
MAX_CITATIONS = 8
UNAVAILABLE = "El cerebro no está disponible ahora"


class RelayError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def clean_answer(data: object) -> dict:
    """La respuesta del cerebro con solo lo que la página necesita."""
    data = data if isinstance(data, dict) else {}
    citations = []
    for c in data.get("citations") or []:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not isinstance(c.get("title"), str):
            continue
        citations.append({"id": _text(c["id"], 8), "title": _text(c["title"], 300),
                          "source": _text(c.get("source"), 40), "url": _text(c.get("url"), 2000)})
        if len(citations) >= MAX_CITATIONS:
            break
    return {"answer": _text(data.get("answer"), 20_000), "engine": _text(data.get("engine"), 20),
            "found": data.get("found") is True, "citations": citations}


def question(body: dict) -> str:
    """La pregunta del cuerpo, comprobada (RelayError 400 si no vale). Se comprueba aquí también para no gastar una
    llamada al cerebro con algo que va a rechazar."""
    q = body.get("q")
    if not isinstance(q, str) or not q.strip() or len(q) > MAX_QUESTION:
        raise RelayError(400, f"Escribe la pregunta (hasta {MAX_QUESTION} caracteres)")
    return " ".join(q.split())


def ask(link, body: dict) -> dict:
    """POST /api/ask en el cerebro. RelayError 400 (pregunta mala) o 503 (cerebro apagado o sin configurar)."""
    q = question(body)
    if link is None:
        raise RelayError(503, UNAVAILABLE)
    try:
        return clean_answer(link.call("/api/ask", {"q": q}))
    except ToolError as e:
        log.warning("Cerebro no disponible para preguntar (%s)", type(e).__name__)
        raise RelayError(503, UNAVAILABLE) from None


def diario(link, body: dict) -> dict:
    """POST /api/diario en el cerebro, con el material tal cual (lo comprueba el cerebro, diario.validate)."""
    if link is None:
        raise RelayError(503, UNAVAILABLE)
    try:
        data = link.call("/api/diario", body)
    except ToolError as e:
        log.warning("Cerebro no disponible para la nota diaria (%s)", type(e).__name__)
        raise RelayError(503, UNAVAILABLE) from None
    if not isinstance(data, dict) or not isinstance(data.get("markdown"), str):
        raise RelayError(503, UNAVAILABLE)
    return {"markdown": data["markdown"], "engine": _text(data.get("engine"), 20)}


# ── Repaso: la pestaña de Apuntes (todas las rutas) y la línea del panel (solo el resumen) ──────────────────────

REPASO_GET = ("hoy", "libre", "ajustes", "resumen")
REPASO_POST = {  # ruta → los únicos campos que pasan al cerebro
    "respuesta": ("question_id", "respuesta", "nota"),
    "mal": ("question_id", "motivo"),
    "ajustes": ("asignaturas", "aviso", "retencion"),
}
TIPOS = ("test", "vf", "formula", "corta")
MAX_FOLDER = 1100        # 10 carpetas de 100 letras y sus «/» (repaso/service.py folder_value)
MAX_LIST = 500           # preguntas o carpetas que se pasan de una vez (el cerebro ya manda 200 como mucho)
PASS_THROUGH = (400, 404, 409, 413)  # errores del cerebro que se enseñan tal cual (su motivo es para Marc)
LINE_TIMEOUT = 4         # la línea del panel: si el cerebro tarda, el panel sale sin ella
LINE_CACHE = 60          # segundos que vale la línea del panel: un cerebro lento no retrasa cada carga
SAFE_URL = re.compile(r"^https?://")
MAX_REPASO_DAYS = 8      # días del Repaso que se pasan a la revisión del panel (el cerebro manda 7)
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _texts(value: object, limit: int, count: int) -> list[str]:
    return [v[:limit] for v in value if isinstance(v, str)][:count] if isinstance(value, list) else []


def _question(q: object) -> dict | None:
    """Una pregunta tal como la pinta la página: SIN respuesta, explicación ni trozo, venga lo que venga."""
    if not isinstance(q, dict) or _int(q.get("id")) is None or q.get("tipo") not in TIPOS \
            or not isinstance(q.get("enunciado"), str):
        return None
    opciones = _texts(q.get("opciones"), 1000, 4) if q["tipo"] == "test" else None
    return {"id": q["id"], "tipo": q["tipo"], "enunciado": q["enunciado"][:4000], "opciones": opciones,
            "asignatura": _text(q.get("asignatura"), 100), "carpeta": _texts(q.get("carpeta"), 100, 10),
            "nueva": q.get("nueva") is True}


def _questions(value: object) -> list[dict]:
    out = [_question(q) for q in value[:MAX_LIST]] if isinstance(value, list) else []
    return [q for q in out if q is not None]


def _fraction(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return float(value)


def _clean_repaso(name: str, data: object, folder: str | None) -> dict:
    d = data if isinstance(data, dict) else {}
    num = lambda key: _int(d.get(key)) or 0  # noqa: E731
    if name == "hoy":
        return {"dia": _text(d.get("dia"), 10), "preguntas": _questions(d.get("preguntas")), "total": num("total"),
                "hechas": num("hechas"), "hecha": d.get("hecha") is True, "racha": num("racha")}
    if name == "libre" and folder is None:
        raw = d.get("carpetas")
        # Solo una lista: un texto o un diccionario también se pueden recortar con [:] y romperían lo de abajo
        carpetas = [{"carpeta": c["carpeta"][:MAX_FOLDER], "preguntas": _int(c.get("preguntas")) or 0}
                    for c in (raw[:MAX_LIST] if isinstance(raw, list) else [])
                    if isinstance(c, dict) and isinstance(c.get("carpeta"), str)]
        return {"carpetas": carpetas}
    if name == "libre":
        return {"carpeta": _text(d.get("carpeta"), MAX_FOLDER), "preguntas": _questions(d.get("preguntas")),
                "total": num("total")}
    if name == "respuesta":
        respuesta = d.get("respuesta")
        out = {"question_id": num("question_id"), "falta_nota": d.get("falta_nota") is True,
               "respuesta": respuesta if isinstance(respuesta, bool) else _text(respuesta, 4000),
               "explicacion": _text(d.get("explicacion"), 4000), "enlace": _text(d.get("enlace"), 2000)}
        if not out["falta_nota"]:
            out.update(correcto=d.get("correcto") is True, nota=num("nota"), vuelve=_text(d.get("vuelve"), 40),
                       hecha=d.get("hecha") is True, racha=num("racha"),
                       otra_vez=d.get("otra_vez") is True)  # la fallada vuelve en esta sesión
        return out
    if name == "mal":
        return {"question_id": num("question_id"), "retirada": d.get("retirada") is True}
    if name == "ajustes":
        retencion = d.get("retencion")
        return {"asignaturas": _texts(d.get("asignaturas"), 100, 100), "aviso": _text(d.get("aviso"), 5),
                "retencion": float(retencion) if isinstance(retencion, (int, float)) and not isinstance(retencion, bool)
                else None, "disponibles": _texts(d.get("disponibles"), 100, 200)}
    por = d.get("por_asignatura") if isinstance(d.get("por_asignatura"), dict) else {}
    return {"racha": num("racha"), "pendientes": num("pendientes"), "hecha": d.get("hecha") is True,
            "aciertos_7d": _fraction(d.get("aciertos_7d")),
            "por_asignatura": {k[:100]: f for k, v in list(por.items())[:100]
                               if isinstance(k, str) and (f := _fraction(v)) is not None}}


def repaso(link, method: str, name: str, *, folder: str | None = None, body: dict | None = None) -> dict:
    """/api/repaso/<name> en el cerebro. RelayError 404 (ruta que no existe), 400 (petición mala, también la que
    rechace el cerebro, con su motivo), 404/409 (pregunta que no existe o ya retirada) o 503 (cerebro apagado)."""
    if method == "GET" and name in REPASO_GET:
        path, payload = f"/api/repaso/{name}", None
        if name == "libre" and folder is not None:
            if not folder.strip() or len(folder) > MAX_FOLDER:
                raise RelayError(400, "Carpeta no válida")
            path += "?carpeta=" + urllib.parse.quote(folder, safe="/")
        else:
            folder = None
    elif method == "POST" and name in REPASO_POST:
        path, folder = f"/api/repaso/{name}", None
        payload = {k: body[k] for k in REPASO_POST[name] if k in (body or {})}
        if name != "ajustes" and _int(payload.get("question_id")) is None:
            raise RelayError(400, "Falta el número de la pregunta")
    else:
        raise RelayError(404, "no existe")
    if link is None:
        raise RelayError(503, UNAVAILABLE)
    try:
        data = link.call(path, payload)
    except ToolError as e:
        status = getattr(e, "status", None)
        if status in PASS_THROUGH:
            raise RelayError(status, getattr(e, "reason", "") or "El Repaso no ha aceptado eso") from None
        log.warning("Cerebro no disponible para el Repaso (%s)", type(e).__name__)
        raise RelayError(503, UNAVAILABLE) from None
    return _clean_repaso(name, data, folder)


def repaso_line(link, apuntes_url: str) -> dict | None:
    """Lo que pinta el panel en «Hoy» («Repaso: racha 6 · 82 % esta semana», con enlace a Apuntes), o None si el
    cerebro no está o no contesta a tiempo: entonces el panel no enseña nada del Repaso."""
    if link is None:
        return None
    try:
        data = link.call("/api/repaso/resumen", timeout=LINE_TIMEOUT)
    except (ToolError, OSError, http.client.HTTPException) as e:
        # ServiceLink ya convierte los cortes de red en ToolError; aun así, la línea es un adorno del panel y nunca
        # debe tumbar /api/today ni el resumen de la mañana por un error de red que se escape.
        log.info("Panel: sin línea del Repaso (%s)", type(e).__name__)
        return None
    racha = _int(data.get("racha")) if isinstance(data, dict) else None
    pendientes = _int(data.get("pendientes")) if isinstance(data, dict) else None
    if racha is None or pendientes is None:
        return None
    fraction = _fraction(data.get("aciertos_7d"))
    url = (apuntes_url or "").strip()
    line = {"racha": racha, "aciertos_7d": round(100 * fraction) if fraction is not None else None,
            "pendientes": pendientes, "hecha": data.get("hecha") is True,
            "url": url.rstrip("/") + "/#repaso" if SAFE_URL.match(url) else ""}
    # Para la revisión del panel (0.67.0). Un cerebro antiguo no los manda: entonces la línea sale sin ellos.
    if isinstance(data.get("hoy"), dict):
        line["hoy"] = {k: _count(data["hoy"].get(k)) for k in ("preguntas", "aciertos")}
    if isinstance(data.get("dias"), dict):
        dias = {}
        for iso, info in data["dias"].items():
            if len(dias) >= MAX_REPASO_DAYS:
                break
            if _iso_day(iso) and isinstance(info, dict):
                dias[iso] = {"hecha": info.get("hecha") is True, "preguntas": _count(info.get("preguntas")),
                             "aciertos": _count(info.get("aciertos"))}
        line["dias"] = dias
    return line


def _iso_day(value: object) -> bool:
    if not isinstance(value, str) or not ISO_DAY.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:   # «2026-13-45» tiene la forma pero no es un día
        return False
    return True


def _count(value: object) -> int | None:
    """Un contador del Repaso: entero no negativo y razonable, o None si no vale."""
    n = _int(value)
    return n if n is not None and 0 <= n <= 10_000 else None


class RepasoLine:
    """repaso_line con caché de LINE_CACHE segundos (también de «no contesta»): /api/today se pide en cada carga del
    panel y cada llamada al cerebro puede esperar hasta LINE_TIMEOUT. Con el candado, si llegan dos cargas a la vez
    solo una pregunta al cerebro; la otra espera y se lleva la misma línea."""

    def __init__(self, link, apuntes_url: str, *, ttl: float = LINE_CACHE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.link, self.apuntes_url, self.ttl, self._clock = link, apuntes_url, ttl, clock
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._until: float | None = None

    def __call__(self) -> dict | None:
        with self._lock:
            now = self._clock()
            if self._until is None or now >= self._until:
                self._value = repaso_line(self.link, self.apuntes_url)
                self._until = self._clock() + self.ttl
            return dict(self._value) if self._value is not None else None
