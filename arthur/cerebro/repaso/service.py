"""El Repaso tal como lo usan la API (/api/repaso/*) y el reloj del aviso: une almacén, sesión y ajustes.

Dos reglas que se cumplen aquí y no en la página:
- Lo que sale hacia el navegador NO lleva la respuesta ni la explicación hasta que se contesta (`public`): la
  página de Apuntes la ve Marc, pero una pestaña o una extensión curiosa no deben poder chivarle la solución.
- La nota de test, verdadero/falso y fórmula la pone el servidor comparando la respuesta (acierto «Bien» = 3,
  fallo «Otra vez» = 1); la que mande el navegador no cuenta. Solo en «corta» la elige Marc, después de ver la
  respuesta, como en Anki.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..settings import (CerebroError, RepasoSettings, load_settings, notice_value, retention_value, save_repaso,
                        subjects_value)
from . import session
from .generate import _VERDAD, _norm
from .store import MAX_MOTIVO, RepasoError, RepasoStore

MADRID = ZoneInfo("Europe/Madrid")
GOOD, AGAIN = 3, 1
MAX_ANSWER = 500       # lo que escribe Marc en una respuesta
MAX_FOLDER_DEPTH = 10
MAX_FREE = 200         # «sin límite» en la sesión libre, pero sin mandar miles de golpe


class RepasoInputError(ValueError):
    """Lo que manda la página no vale (→ 400). El mensaje se puede enseñar."""


def _madrid_now() -> datetime:
    return datetime.now(MADRID)


def public(q: dict) -> dict:
    """La pregunta sin nada que delate la respuesta (ni el trozo del que sale)."""
    return {"id": q["id"], "tipo": q["tipo"], "enunciado": q["enunciado"], "opciones": q["opciones"],
            "asignatura": q["asignatura"], "carpeta": q["carpeta"], "nueva": q["nueva"]}


def _latex(s: str) -> str:
    # Para comparar fórmulas: fuera espacios y los «$» que la rodean («n x^{n-1}» = «$nx^{n - 1}$»)
    return re.sub(r"\s+|\$", "", s)


def is_correct(q: dict, answer) -> bool:
    """¿Acierta? Solo para test, vf y formula. Lanza RepasoInputError si la respuesta no es del tipo que toca."""
    tipo = q["tipo"]
    if tipo == "vf":
        if isinstance(answer, str) and _norm(answer) in _VERDAD:
            answer = _VERDAD[_norm(answer)]
        if not isinstance(answer, bool):
            raise RepasoInputError("En verdadero o falso la respuesta es true o false")
        return answer is q["respuesta"]
    if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER:
        raise RepasoInputError(f"Escribe la respuesta (hasta {MAX_ANSWER} caracteres)")
    if tipo == "test":
        return _norm(answer) == _norm(q["respuesta"])
    return _latex(answer) == _latex(q["respuesta"])


def folder_value(text: str | None) -> list[str]:
    """«Cálculo/Tema 2» → ["Cálculo", "Tema 2"]. Sin trozos vacíos ni rutas absurdas."""
    if not isinstance(text, str):
        raise RepasoInputError("Falta la carpeta")
    parts = [" ".join(p.split()) for p in text.split("/")]
    if not parts or len(parts) > MAX_FOLDER_DEPTH or not all(parts) or any(len(p) > 100 for p in parts):
        raise RepasoInputError("Carpeta no válida")
    return parts


class Repaso:
    def __init__(self, store: RepasoStore, settings_path: Path, *, now: Callable[[], datetime] = _madrid_now,
                 link_fn: Callable[[int], str] | None = None,
                 subjects_fn: Callable[[], list[str]] | None = None) -> None:
        self.store = store
        self.settings_path = settings_path
        self.now = now
        self.link_fn = link_fn or (lambda chunk_id: "")      # «Ver en el apunte»
        self.subjects_fn = subjects_fn or (lambda: [])       # asignaturas que hay en Apuntes (para los ajustes)
        self._lock = threading.Lock()
        self._settings = load_settings(settings_path).repaso

    # ── Ajustes ─────────────────────────────────────────────────────────────────────────────────────────────────

    @property
    def settings(self) -> RepasoSettings:
        with self._lock:
            return self._settings

    def settings_view(self) -> dict:
        s = self.settings
        try:
            disponibles = list(self.subjects_fn())
        except Exception:  # noqa: BLE001 — el índice ocupado no debe impedir ver los ajustes
            disponibles = []
        return {"asignaturas": list(s.subjects), "aviso": s.notice_text, "retencion": s.retention,
                "disponibles": disponibles}

    def save_settings(self, body: dict) -> dict:
        """Cambia lo que venga (asignaturas, aviso, retencion); lo que no venga se queda como estaba."""
        unknown = set(body) - {"asignaturas", "aviso", "retencion"}
        if unknown:
            raise RepasoInputError("Ajuste desconocido: " + ", ".join(sorted(unknown)))
        with self._lock:
            cur = self._settings
            try:
                subjects = subjects_value(body["asignaturas"]) if "asignaturas" in body else cur.subjects
                notice = notice_value(body["aviso"]) if "aviso" in body else cur.notice
                retention = retention_value(body["retencion"]) if "retencion" in body else cur.retention
            except CerebroError as e:
                raise RepasoInputError(str(e)) from None
            self._settings = save_repaso(self.settings_path, subjects=subjects, notice=notice, retention=retention)
        return self.settings_view()

    # ── Sesión ──────────────────────────────────────────────────────────────────────────────────────────────────

    def today(self) -> dict:
        now = self.now()
        day = self.store.local_day(now)
        preguntas = session.daily(self.store, set(self.settings.subjects), now)
        ids = self.store.session(day) or []
        # «Hechas» = resueltas (acertadas, retiradas o falladas 3 veces); una fallada que vuelve aún no lo está
        return {"dia": day.isoformat(), "preguntas": [public(q) for q in preguntas], "total": len(ids),
                "hechas": len(ids) - len(self.store.session_pending(day)) if ids else 0,
                "hecha": self.store.day(day)["done"], "racha": session.streak(self.store, day)}

    def pending(self, now: datetime | None = None) -> int:
        """Cuántas quedan hoy, SIN fijar la sesión (el panel y el aviso preguntan antes de que Marc la abra)."""
        now = now or self.now()
        day = self.store.local_day(now)
        ids = self.store.session(day)
        if ids is not None:
            return len(self.store.session_pending(day))   # las falladas que tienen que volver también cuentan
        subjects = set(self.settings.subjects)
        due = self.store.due_ids(subjects, now, 10)
        return len(due) + len(self.store.new_ids(subjects, min(5, 10 - len(due))))

    def free(self, folder: str | None) -> dict:
        if folder is None:
            # Sin carpeta: qué asignaturas y temas hay, para elegir
            counts: dict[str, int] = {}
            for q in self.store.active_questions():
                for depth in range(1, len(q["carpeta"]) + 1):
                    key = "/".join(q["carpeta"][:depth])
                    counts[key] = counts.get(key, 0) + 1
            return {"carpetas": [{"carpeta": k, "preguntas": n} for k, n in sorted(counts.items())]}
        preguntas = session.free(self.store, folder_value(folder), self.now())
        return {"carpeta": folder, "preguntas": [public(q) for q in preguntas[:MAX_FREE]], "total": len(preguntas)}

    def answer(self, body: dict) -> dict:
        qid = body.get("question_id")
        if not isinstance(qid, int) or isinstance(qid, bool):
            raise RepasoInputError("Falta el número de la pregunta")
        q = self.store.question(qid)  # KeyError → 404
        if q["retired_at"] is not None:
            raise RepasoError("Esa pregunta está retirada")
        solution = {"respuesta": q["respuesta"], "explicacion": q["explicacion"], "enlace": self.link_fn(q["chunk_id"])}
        if q["tipo"] == "corta":
            nota = body.get("nota")
            if nota is None:
                # Primer paso: enseñar la respuesta; Marc se pone la nota después
                return {"question_id": qid, "falta_nota": True, **solution}
            if not isinstance(nota, int) or isinstance(nota, bool) or nota not in (1, 2, 3, 4):
                raise RepasoInputError("La nota tiene que ser 1, 2, 3 o 4")
            correct = nota > AGAIN  # «Difícil» también es acordarse
        else:
            if "respuesta" not in body:
                raise RepasoInputError("Falta la respuesta")
            correct = is_correct(q, body["respuesta"])
            nota = GOOD if correct else AGAIN
        now = self.now()
        # Si cuenta para la sesión diaria (y para cuál: la de ayer si se empezó antes de medianoche) o es repaso
        # libre lo decide el almacén con el candado cogido: decidirlo aquí fuera dejaba que un doble envío
        # entrara dos veces. La página no dice en qué modo está: una pendiente de la sesión cuenta para ella.
        r = self.store.record_answer(qid, nota, correct, "diaria", now, retention=self.settings.retention)
        dia = date.fromisoformat(r["day"])
        return {"question_id": qid, "correcto": correct, "nota": nota, "falta_nota": False, **solution,
                "vuelve": r["card"].due.isoformat(), "otra_vez": r["otra_vez"],
                "hecha": self.store.day(dia)["done"], "racha": session.streak(self.store, self.store.local_day(now))}

    def report(self, body: dict) -> dict:
        qid, motivo = body.get("question_id"), body.get("motivo", "")
        if not isinstance(qid, int) or isinstance(qid, bool):
            raise RepasoInputError("Falta el número de la pregunta")
        if not isinstance(motivo, str) or len(motivo) > MAX_MOTIVO:
            raise RepasoInputError(f"El motivo, hasta {MAX_MOTIVO} caracteres")
        self.store.retire(qid, motivo)  # KeyError → 404
        return {"question_id": qid, "retirada": True}

    def summary(self) -> dict:
        now = self.now()
        day = self.store.local_day(now)
        week = session.first_daily(self.store.answers_since(now - timedelta(days=7)))
        # (answers_since mira 7×24 h hacia atrás: el primer día de `dias` puede quedar corto por la mañana)
        # Para la revisión del panel (0.67.0): preguntas y aciertos de la sesión diaria de hoy y de los últimos
        # 7 días, con el primer intento de cada pregunta (los mismos que cuentan para el %).
        dias = {}
        for i in range(7):
            d = day - timedelta(days=i)
            answers = [a for a in week if a["day"] == d.isoformat()]
            dias[d.isoformat()] = {"hecha": self.store.day(d)["done"], "preguntas": len(answers),
                                   "aciertos": sum(a["correct"] for a in answers)}
        return {"racha": session.streak(self.store, day), "pendientes": self.pending(now),
                "hecha": self.store.day(day)["done"],
                "aciertos_7d": sum(a["correct"] for a in week) / len(week) if week else None,
                "por_asignatura": session.accuracy(self.store, now),
                "hoy": {k: dias[day.isoformat()][k] for k in ("preguntas", "aciertos")}, "dias": dias}
