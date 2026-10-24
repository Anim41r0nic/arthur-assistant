"""Almacén del Repaso: <datos>/cerebro/repaso.sqlite3

  questions  las preguntas sacadas de los apuntes (las retiradas se quedan, con su motivo, para no repetirlas)
  cards      la tarjeta FSRS de cada pregunta: cuándo vuelve a salir y cómo de firme está el recuerdo
  answers    cada respuesta de Marc (lo que leerá el agente de revisión de la pieza 4)
  days       por día: si la sesión diaria está hecha y si ya se mandó el aviso
  sessions   qué preguntas forman la sesión de cada día (así recargar la página no la cambia)
  failed_keys los trozos que no dieron ninguna pregunta válida, por su clave (para dejar de pedirlos)
  nights     por noche: cuándo fue el último intento de preparar preguntas, cuántas salieron y si hay que
             reintentar (así reiniciar el cerebro a las 03:00 no vuelve a generar ni se salta el tope de 40)

Cada pregunta guarda el número de su trozo (`chunk_id`, para el enlace «Ver en el apunte») y su clave estable
(`chunk_key`, ver `index.chunk_key`). El número cambia cada vez que se reindexa el apunte; la clave solo si cambia
el texto del trozo. `follow_chunks` usa la clave para volver a enlazar las preguntas con su trozo y retirar las que
se han quedado sin él.

Sesión diaria: una pregunta fallada vuelve al final de la misma sesión (como en Duolingo). La sesión está hecha
cuando cada pregunta está acertada, retirada o fallada `MAX_FALLOS` veces ese día (para no atascarse en una).
Quién cuenta para la sesión lo decide `answer` aquí dentro, con el candado y en una transacción: así un doble
envío o dos peticiones a la vez no apuntan la misma respuesta dos veces.

Fechas: `due` y `last_review` se guardan en UTC con ancho fijo, para que ordenar el texto sea ordenar el
tiempo aunque en medio cambie la hora (+02:00 / +01:00). Lo que se enseña (`created_at`, `answered_at`) va en
hora de Madrid con su zona, y el «día» de una respuesta es el de Madrid: a las 00:30 ya es mañana para Marc
aunque en UTC aún sea hoy.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal
from zoneinfo import ZoneInfo

from .fsrs import STATES, Card, new_card, review

SCHEMA_VERSION = 2  # 1: preguntas, tarjetas, respuestas, días y sesiones · 2: clave estable del trozo
MEMORIA = Path(":memory:")
MADRID = ZoneInfo("Europe/Madrid")
TIPOS = ("test", "vf", "formula", "corta")
MODOS = ("diaria", "libre")
MAX_TEXT = 2000    # enunciado, respuesta o explicación: una pregunta de repaso es corta
MAX_MOTIVO = 300
MAX_FALLOS = 3     # fallada tantas veces el mismo día, la pregunta deja de volver hasta el próximo repaso
# Dos envíos de la misma pregunta tan seguidos son el mismo clic repetido (doble clic, reintento del navegador),
# no un segundo intento: volver a verla al final de la sesión y contestarla lleva más tiempo que esto
DOBLE_ENVIO = timedelta(seconds=3)


class RepasoError(Exception):
    pass


def _madrid_now() -> datetime:
    return datetime.now(MADRID)


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


class RepasoStore:
    def __init__(self, path: Path, *, now: Callable[[], datetime] = _madrid_now, zone: ZoneInfo = MADRID) -> None:
        """`path` es el fichero SQLite; `Path(":memory:")` (o `RepasoStore.in_memory()`) no toca el disco."""
        self._now = now
        self.zone = zone
        memoria = path == MEMORIA
        if not memoria:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Una conexión compartida entre la web y el reloj nocturno; el candado ordena los accesos
        self._lock = threading.Lock()
        self._db = sqlite3.connect(":memory:" if memoria else str(path), check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if not memoria:
            self._db.execute("PRAGMA journal_mode=WAL")
        if self._db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id INTEGER NOT NULL,
                    asignatura TEXT NOT NULL,
                    carpeta TEXT NOT NULL,          -- JSON: ["Cálculo", "Tema 2"]
                    tipo TEXT NOT NULL,
                    enunciado TEXT NOT NULL,
                    opciones TEXT,                  -- JSON o NULL si no es de tipo test
                    respuesta TEXT NOT NULL,        -- JSON: en vf es true/false
                    explicacion TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    retired_at TEXT,
                    motivo TEXT);
                CREATE TABLE IF NOT EXISTS cards (
                    question_id INTEGER PRIMARY KEY REFERENCES questions(id),
                    estabilidad REAL NOT NULL,
                    dificultad REAL NOT NULL,
                    due TEXT NOT NULL,
                    repeticiones INTEGER NOT NULL,
                    fallos INTEGER NOT NULL,
                    last_review TEXT,
                    estado TEXT NOT NULL,
                    paso INTEGER);
                CREATE INDEX IF NOT EXISTS cards_due ON cards (due);
                CREATE TABLE IF NOT EXISTS answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id INTEGER NOT NULL REFERENCES questions(id),
                    answered_at TEXT NOT NULL,      -- hora de Madrid: 2026-10-17T20:05:00+02:00
                    answered_utc TEXT NOT NULL,     -- la misma en UTC, para comparar momentos
                    dia TEXT NOT NULL,              -- día de Madrid: 2026-10-17 (en «diaria», el de su sesión)
                    nota INTEGER NOT NULL,
                    acierto INTEGER NOT NULL,
                    modo TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS answers_utc ON answers (answered_utc);
                CREATE INDEX IF NOT EXISTS answers_dia ON answers (dia, modo);
                CREATE TABLE IF NOT EXISTS days (
                    fecha TEXT PRIMARY KEY,
                    hecha INTEGER NOT NULL DEFAULT 0,
                    avisada INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    fecha TEXT NOT NULL,
                    pos INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    PRIMARY KEY (fecha, question_id));
                CREATE TABLE IF NOT EXISTS failed_chunks (
                    chunk_id INTEGER PRIMARY KEY,
                    intentos INTEGER NOT NULL,
                    ultimo_utc TEXT NOT NULL);
                PRAGMA user_version = 1;
            """)
        if self._db.execute("PRAGMA user_version").fetchone()[0] < 2:
            # Las preguntas de la versión 1 se quedan con la clave vacía: `follow_chunks` se la pone en la siguiente
            # pasada si su trozo sigue existiendo (los números de trozo nunca se reutilizan) o las retira si no.
            # `failed_chunks` (por número) se deja de usar: como mucho, un trozo que fallaba se reintenta otra vez.
            self._db.executescript("""
                ALTER TABLE questions ADD COLUMN chunk_key TEXT;
                CREATE INDEX IF NOT EXISTS questions_key ON questions (chunk_key);
                CREATE TABLE IF NOT EXISTS failed_keys (
                    chunk_key TEXT PRIMARY KEY,
                    intentos INTEGER NOT NULL,
                    ultimo_utc TEXT NOT NULL);
                PRAGMA user_version = 2;
            """)
        # Fuera del bloque de versión: así también aparece en un almacén que ya existía, sin migración
        self._db.execute("""CREATE TABLE IF NOT EXISTS nights (
                                fecha TEXT PRIMARY KEY,
                                intento TEXT NOT NULL,          -- hora de Madrid del último intento
                                creadas INTEGER NOT NULL,       -- preguntas de esa noche, sumando los intentos
                                parado TEXT,
                                reintentar INTEGER NOT NULL)""")

    @classmethod
    def in_memory(cls, *, now: Callable[[], datetime] = _madrid_now) -> "RepasoStore":
        return cls(MEMORIA, now=now)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.zone)

    def _stamp(self, moment: datetime | None = None) -> str:
        return self._local(moment or self._now()).replace(microsecond=0).isoformat()

    def local_day(self, moment: datetime) -> date:
        return self._local(moment).date()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    @contextmanager
    def _tx(self):
        """Candado + transacción: o se guarda todo o nada (una pregunta sin tarjeta, o una tarjeta movida sin su
        respuesta apuntada, dejarían el Repaso descuadrado). IMMEDIATE coge ya el permiso de escritura."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # --- preguntas ---
    def add_question(self, q: dict) -> int:
        """`q` = {"chunk_id", "chunk_key"? (la clave estable del trozo), "asignatura", "carpeta"? (por defecto
        [asignatura]), "tipo", "enunciado", "opciones"? (lista), "respuesta", "explicacion"?}. La comprobación de fondo
        (una sola correcta, LaTeX…) la hace quien las genera; aquí solo se evita guardar algo que rompa la página."""
        chunk_id = q.get("chunk_id")
        if not isinstance(chunk_id, int) or isinstance(chunk_id, bool):
            raise RepasoError("La pregunta necesita el id del trozo del que sale")
        chunk_key = q.get("chunk_key")
        if chunk_key is not None and (not isinstance(chunk_key, str) or not chunk_key):
            raise RepasoError("La clave del trozo tiene que ser un texto")
        asignatura = " ".join(str(q.get("asignatura") or "").split())
        if not asignatura:
            raise RepasoError("La pregunta necesita asignatura")
        carpeta = q.get("carpeta") or [asignatura]
        if not isinstance(carpeta, list) or not all(isinstance(c, str) and c for c in carpeta):
            raise RepasoError("La carpeta tiene que ser una lista de nombres")
        tipo = q.get("tipo")
        if tipo not in TIPOS:
            raise RepasoError(f"Tipo de pregunta desconocido: {tipo}")
        enunciado = str(q.get("enunciado") or "").strip()
        explicacion = str(q.get("explicacion") or "").strip()
        respuesta = q.get("respuesta")
        if not enunciado or respuesta is None or respuesta == "":
            raise RepasoError("La pregunta necesita enunciado y respuesta")
        if max(len(enunciado), len(explicacion), len(json.dumps(respuesta, ensure_ascii=False))) > MAX_TEXT:
            raise RepasoError("La pregunta es demasiado larga")
        opciones = q.get("opciones")
        if opciones is not None and (not isinstance(opciones, list) or not all(isinstance(o, str) for o in opciones)):
            raise RepasoError("Las opciones tienen que ser una lista de textos")
        now = self._now()
        card = new_card(now)
        with self._tx():
            cur = self._db.execute(
                "INSERT INTO questions (chunk_id, chunk_key, asignatura, carpeta, tipo, enunciado, opciones, "
                "respuesta, explicacion, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (chunk_id, chunk_key, asignatura, json.dumps(carpeta, ensure_ascii=False), tipo, enunciado,
                 None if opciones is None else json.dumps(opciones, ensure_ascii=False),
                 json.dumps(respuesta, ensure_ascii=False), explicacion, self._stamp(now)))
            qid = cur.lastrowid
            self._save_card(qid, card, insert=True)
        return qid

    def retire(self, qid: int, reason: str) -> None:
        """Retira la pregunta para siempre. Si era la última que faltaba de la sesión (la de hoy, o la de ayer
        si se está acabando pasada la medianoche), el día queda hecho: retirarla no debe romper la racha."""
        motivo = " ".join(str(reason).split())[:MAX_MOTIVO]
        now = self._now()
        today = self.local_day(now)
        with self._tx():
            cur = self._db.execute("UPDATE questions SET retired_at = ?, motivo = ? WHERE id = ?",
                                   (self._stamp(now), motivo, qid))
            if cur.rowcount == 0:
                raise KeyError(qid)
            for d in (today, today - timedelta(days=1)):
                self._maybe_complete(d.isoformat())

    def question(self, qid: int) -> dict:
        rows = self._q(_SELECT + " WHERE q.id = ?", (qid,))
        if not rows:
            raise KeyError(qid)
        return self._question(rows[0])

    def questions(self, ids: Iterable[int]) -> list[dict]:
        """Las preguntas en el orden de `ids` (las que no existen se saltan)."""
        ids = list(ids)
        if not ids:
            return []
        rows = self._q(_SELECT + f" WHERE q.id IN ({','.join('?' * len(ids))})", tuple(ids))
        by_id = {r["id"]: self._question(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def active_questions(self) -> list[dict]:
        return [self._question(r) for r in self._q(_SELECT + " WHERE q.retired_at IS NULL ORDER BY q.id")]

    def chunks_with_questions(self) -> set[int]:
        # Las retiradas cuentan: si no, cada noche se volvería a preguntar por el mismo trozo
        return {r[0] for r in self._q("SELECT DISTINCT chunk_id FROM questions")}

    def keys_with_questions(self) -> set[str]:
        # Como `chunks_with_questions`, pero por la clave estable: es lo que sobrevive a reindexar el apunte
        return {r[0] for r in self._q("SELECT DISTINCT chunk_key FROM questions WHERE chunk_key IS NOT NULL")}

    def note_failed_key(self, key: str) -> int:
        """Apunta que un trozo (por su clave) no dio ninguna pregunta válida y devuelve cuántas noches lleva así."""
        self._q("""INSERT INTO failed_keys (chunk_key, intentos, ultimo_utc) VALUES (?, 1, ?)
                   ON CONFLICT (chunk_key) DO UPDATE SET intentos = intentos + 1, ultimo_utc = excluded.ultimo_utc""",
                (key, self._stamp()))
        return self._q("SELECT intentos FROM failed_keys WHERE chunk_key = ?", (key,))[0][0]

    def failed_keys(self, max_tries: int) -> set[str]:
        # Un trozo que ya falló `max_tries` noches no se vuelve a pedir: gastaría presupuesto cada noche para nada
        return {r[0] for r in self._q("SELECT chunk_key FROM failed_keys WHERE intentos >= ?", (max_tries,))}

    def follow_chunks(self, current: dict[str, tuple[int, str]], reason: str) -> dict:
        """Pone las preguntas al día con los trozos que hay ahora en el índice: `current` = {clave: (id, asignatura)}.

        - Si el trozo de una pregunta sigue (misma clave), se le pone su número de ahora (el enlace «Ver en el
          apunte» vuelve a funcionar) y su asignatura de ahora (el apunte se pudo mover de carpeta).
        - Si ya no está (se editó ese trozo, se borró el apunte), la pregunta se retira con `reason`. Se queda
          guardada, como todas las retiradas; si el texto vuelve (Marc deshace el cambio), vuelve con él.
        - Las de antes de la clave (versión 1) la toman de su número, si ese trozo sigue existiendo.

        Con `current` vacío no se hace nada: un índice vacío es uno recién creado o sin leer todavía, no que Marc lo
        haya borrado todo (lo mismo que hace el índice con un escaneo sin notas). Devuelve los recuentos."""
        result = {"reenlazadas": 0, "retiradas": 0, "recuperadas": 0}
        if not current:
            return result
        motivo = " ".join(str(reason).split())[:MAX_MOTIVO]
        by_id = {cid: key for key, (cid, _) in current.items()}
        stamp = self._stamp()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute("SELECT id, chunk_id, chunk_key, asignatura, carpeta, retired_at, motivo "
                                        "FROM questions").fetchall()
                for r in rows:
                    key = r["chunk_key"] or by_id.get(r["chunk_id"])
                    hit = current.get(key) if key else None
                    if hit is None:
                        if r["retired_at"] is None:
                            self._db.execute("UPDATE questions SET retired_at = ?, motivo = ? WHERE id = ?",
                                             (stamp, motivo, r["id"]))
                            result["retiradas"] += 1
                        continue
                    chunk_id, subject = hit[0], " ".join(hit[1].split()) or r["asignatura"]
                    carpeta = r["carpeta"]
                    if json.loads(carpeta) == [r["asignatura"]]:  # la carpeta por defecto sigue a la asignatura
                        carpeta = json.dumps([subject], ensure_ascii=False)
                    if (chunk_id, key, subject, carpeta) != (r["chunk_id"], r["chunk_key"], r["asignatura"],
                                                             r["carpeta"]):
                        self._db.execute("UPDATE questions SET chunk_id = ?, chunk_key = ?, asignatura = ?, "
                                         "carpeta = ? WHERE id = ?", (chunk_id, key, subject, carpeta, r["id"]))
                        result["reenlazadas"] += 1
                    if r["retired_at"] is not None and r["motivo"] == motivo:
                        self._db.execute("UPDATE questions SET retired_at = NULL, motivo = NULL WHERE id = ?",
                                         (r["id"],))
                        result["recuperadas"] += 1
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return result

    def failed_keys_on(self, d: date) -> set[str]:
        """Trozos (por clave) que ya fallaron ese día de Madrid: un reintento esa misma noche no los repite."""
        # `ultimo_utc` guarda la hora de Madrid con su zona (`_stamp`): sus 10 primeros caracteres son el día
        return {r[0] for r in self._q("SELECT chunk_key FROM failed_keys WHERE substr(ultimo_utc, 1, 10) = ?",
                                      (d.isoformat(),))}

    def created_on(self, d: date) -> int:
        """Preguntas preparadas ese día (de Madrid), retiradas incluidas: lo que ya cuenta para el tope."""
        return self._q("SELECT COUNT(*) FROM questions WHERE substr(created_at, 1, 10) = ?", (d.isoformat(),))[0][0]

    def _question(self, r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "chunk_id": r["chunk_id"], "chunk_key": r["chunk_key"], "asignatura": r["asignatura"],
            "carpeta": json.loads(r["carpeta"]), "tipo": r["tipo"], "enunciado": r["enunciado"],
            "opciones": None if r["opciones"] is None else json.loads(r["opciones"]),
            "respuesta": json.loads(r["respuesta"]), "explicacion": r["explicacion"],
            "created_at": r["created_at"], "retired_at": r["retired_at"], "motivo": r["motivo"],
            "due": self._local(datetime.fromisoformat(r["due"])), "nueva": r["repeticiones"] == 0,
        }

    # --- tarjetas y respuestas ---
    def card(self, qid: int) -> Card:
        rows = self._q("SELECT * FROM cards WHERE question_id = ?", (qid,))
        if not rows:
            raise KeyError(qid)
        return self._card(rows[0])

    def _card(self, r: sqlite3.Row) -> Card:
        last = r["last_review"]
        return Card(stability=r["estabilidad"], difficulty=r["dificultad"],
                    due=self._local(datetime.fromisoformat(r["due"])), reps=r["repeticiones"], lapses=r["fallos"],
                    last_review=None if last is None else self._local(datetime.fromisoformat(last)),
                    state=r["estado"], step=r["paso"])

    def _save_card(self, qid: int, card: Card, *, insert: bool = False) -> None:
        """Con el candado ya cogido."""
        if card.state not in STATES:
            raise RepasoError(f"Estado de tarjeta desconocido: {card.state}")
        values = (card.stability, card.difficulty, _utc(card.due), card.reps, card.lapses,
                  None if card.last_review is None else _utc(card.last_review), card.state, card.step)
        if insert:
            self._db.execute("INSERT INTO cards (estabilidad, dificultad, due, repeticiones, fallos, last_review, "
                             "estado, paso, question_id) VALUES (?,?,?,?,?,?,?,?,?)", (*values, qid))
        else:
            self._db.execute("UPDATE cards SET estabilidad = ?, dificultad = ?, due = ?, repeticiones = ?, "
                             "fallos = ?, last_review = ?, estado = ?, paso = ? WHERE question_id = ?",
                             (*values, qid))

    def answer(self, qid: int, rating: int, correct: bool, mode: Literal["diaria", "libre"], now: datetime, *,
               retention: float = 0.9) -> Card:
        """Como `record_answer`, devolviendo solo la tarjeta."""
        return self.record_answer(qid, rating, correct, mode, now, retention=retention)["card"]

    def record_answer(self, qid: int, rating: int, correct: bool, mode: Literal["diaria", "libre"], now: datetime,
                      *, retention: float = 0.9) -> dict:
        """Apunta la respuesta y mueve la tarjeta; `retention` es la de los ajustes del Repaso.

        `mode="diaria"` pide que cuente para la sesión diaria, pero quien decide es esto, dentro del candado:
        - si la pregunta está pendiente en la sesión de hoy (o en la de ayer, si se empezó antes de medianoche y
          aún le quedan), cuenta para esa sesión; si con ella ya no queda ninguna, ese día queda hecho;
        - si ya estaba resuelta hoy, o llega otra vez en menos de `DOBLE_ENVIO`, es un envío repetido: no se
          apunta ni se mueve la tarjeta, y se devuelve la que hay (`repetida`);
        - si no es de ninguna sesión abierta, es repaso libre: mueve la tarjeta igual (repasar es repasar) pero
          no completa nada.
        Devuelve {"card", "mode", "day" (el de la sesión o el de Madrid), "repetida", "otra_vez"}; `otra_vez`
        dice si vuelve a salir en esta misma sesión porque se ha fallado."""
        if rating not in (1, 2, 3, 4):
            raise RepasoError("La nota tiene que ser 1, 2, 3 o 4")
        if mode not in MODOS:
            raise RepasoError(f"Modo desconocido: {mode}")
        with self._tx():
            row = self._db.execute("SELECT retired_at FROM questions WHERE id = ?", (qid,)).fetchone()
            if row is None:
                raise KeyError(qid)
            if row["retired_at"] is not None:
                raise RepasoError("Esa pregunta está retirada")
            modo, day, repetida = "libre", self.local_day(now).isoformat(), False
            if mode == "diaria":
                slot = self._daily_slot(qid, now)
                if slot is not None:
                    modo, (day, repetida) = "diaria", slot
            if not repetida:
                card = review(self._card_row(qid), rating, now, retention)
                self._save_card(qid, card)
                self._db.execute(
                    "INSERT INTO answers (question_id, answered_at, answered_utc, dia, nota, acierto, modo) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (qid, self._stamp(now), _utc(now), day, rating, int(bool(correct)), modo))
                if modo == "diaria":
                    self._maybe_complete(day)
            otra_vez = modo == "diaria" and qid in self._pending(day)
            return {"card": self._card_row(qid), "mode": modo, "day": day, "repetida": repetida,
                    "otra_vez": otra_vez}

    def _card_row(self, qid: int) -> Card:
        """Con el candado ya cogido."""
        return self._card(self._db.execute("SELECT * FROM cards WHERE question_id = ?", (qid,)).fetchone())

    def _daily_slot(self, qid: int, now: datetime) -> tuple[str, bool] | None:
        """Con el candado ya cogido. ¿De qué sesión es esta respuesta? (día, ¿repetida?), o None si de ninguna.
        Primero la de hoy; después la de ayer, para la sesión empezada a las 23:55 y acabada a las 00:05."""
        today = self.local_day(now)
        for d in (today, today - timedelta(days=1)):
            day = d.isoformat()
            if self._db.execute("SELECT 1 FROM sessions WHERE fecha = ? AND question_id = ?",
                                (day, qid)).fetchone() is None:
                continue
            last = self._db.execute("SELECT MAX(answered_utc) FROM answers WHERE question_id = ? AND dia = ? "
                                    "AND modo = 'diaria'", (qid, day)).fetchone()[0]
            if last is not None and now - datetime.fromisoformat(last) < DOBLE_ENVIO:
                return day, True
            if qid in self._pending(day):
                return day, False
            if d == today:
                return day, True   # ya resuelta hoy: otro envío no la cuenta dos veces
        return None

    def _pending(self, day: str) -> list[int]:
        """Con el candado ya cogido. Las de la sesión de `day` que faltan: ni acertadas, ni retiradas, ni
        falladas `MAX_FALLOS` veces. Primero las aún sin intentar, en su orden; detrás las falladas, la que se
        falló antes primero (así la que acabas de fallar no sale justo a continuación si hay otras)."""
        rows = self._db.execute(
            "SELECT s.question_id, COALESCE(MAX(a.acierto), 0) AS ok, COUNT(a.id) AS intentos, "
            "MAX(a.answered_utc) AS ultimo FROM sessions s "
            "JOIN questions q ON q.id = s.question_id AND q.retired_at IS NULL "
            "LEFT JOIN answers a ON a.question_id = s.question_id AND a.dia = s.fecha AND a.modo = 'diaria' "
            "WHERE s.fecha = ? GROUP BY s.question_id, s.pos "
            "HAVING ok = 0 AND intentos < ? ORDER BY ultimo IS NOT NULL, ultimo, s.pos", (day, MAX_FALLOS))
        return [r[0] for r in rows]

    def _maybe_complete(self, day: str) -> None:
        """Con el candado ya cogido. La única comprobación de «sesión completa» (la llaman `answer` y `retire`):
        si ese día hay sesión y ya no le falta ninguna, el día queda hecho (es lo que cuenta para la racha)."""
        if self._db.execute("SELECT 1 FROM sessions WHERE fecha = ? LIMIT 1", (day,)).fetchone() is None:
            return
        if not self._pending(day):
            self._mark_day(day, done=True, notified=None)

    def session_pending(self, d: date) -> list[int]:
        """Las que faltan de la sesión de `d`, en el orden en que tienen que salir (ver `_pending`)."""
        with self._lock:
            return self._pending(d.isoformat())

    def answers_since(self, since: datetime) -> list[dict]:
        rows = self._q(
            "SELECT a.*, q.asignatura FROM answers a JOIN questions q ON q.id = a.question_id "
            "WHERE a.answered_utc >= ? ORDER BY a.answered_utc, a.id", (_utc(since),))
        return [{"id": r["id"], "question_id": r["question_id"], "answered_at": r["answered_at"], "day": r["dia"],
                 "rating": r["nota"], "correct": bool(r["acierto"]), "mode": r["modo"],
                 "asignatura": r["asignatura"]} for r in rows]

    # --- para elegir la sesión (session.py) ---
    def due_ids(self, subjects: set[str], now: datetime, limit: int) -> list[int]:
        """Ya contestadas alguna vez y que tocan, las más atrasadas primero."""
        if not subjects or limit <= 0:
            return []
        marks = ",".join("?" * len(subjects))
        rows = self._q(
            "SELECT q.id FROM questions q JOIN cards c ON c.question_id = q.id "
            f"WHERE q.retired_at IS NULL AND c.repeticiones > 0 AND c.due <= ? AND q.asignatura IN ({marks}) "
            "ORDER BY c.due, q.id LIMIT ?", (_utc(now), *sorted(subjects), limit))
        return [r[0] for r in rows]

    def new_ids(self, subjects: set[str], limit: int) -> list[int]:
        """Nunca contestadas, en el orden en que se prepararon."""
        if not subjects or limit <= 0:
            return []
        marks = ",".join("?" * len(subjects))
        rows = self._q(
            "SELECT q.id FROM questions q JOIN cards c ON c.question_id = q.id "
            f"WHERE q.retired_at IS NULL AND c.repeticiones = 0 AND q.asignatura IN ({marks}) "
            "ORDER BY q.id LIMIT ?", (*sorted(subjects), limit))
        return [r[0] for r in rows]

    def session(self, d: date) -> list[int] | None:
        rows = self._q("SELECT question_id FROM sessions WHERE fecha = ? ORDER BY pos", (d.isoformat(),))
        return [r[0] for r in rows] if rows else None

    def save_session(self, d: date, ids: list[int]) -> None:
        with self._lock:
            self._db.executemany("INSERT OR IGNORE INTO sessions (fecha, pos, question_id) VALUES (?,?,?)",
                                 [(d.isoformat(), pos, qid) for pos, qid in enumerate(ids)])

    # --- días ---
    def day(self, d: date) -> dict:
        rows = self._q("SELECT * FROM days WHERE fecha = ?", (d.isoformat(),))
        done, notified = (bool(rows[0]["hecha"]), bool(rows[0]["avisada"])) if rows else (False, False)
        return {"day": d.isoformat(), "done": done, "notified": notified}

    def done_days(self, until: date) -> set[date]:
        return {date.fromisoformat(r[0])
                for r in self._q("SELECT fecha FROM days WHERE hecha = 1 AND fecha <= ?", (until.isoformat(),))}

    def mark_day(self, d: date, *, done: bool | None = None, notified: bool | None = None) -> None:
        with self._lock:
            self._mark_day(d.isoformat(), done=done, notified=notified)

    # --- noches de preguntas (scheduler.RepasoClock) ---
    def night(self, d: date) -> dict | None:
        rows = self._q("SELECT * FROM nights WHERE fecha = ?", (d.isoformat(),))
        if not rows:
            return None
        r = rows[0]
        return {"dia": r["fecha"], "intento": datetime.fromisoformat(r["intento"]), "creadas": r["creadas"],
                "parado": r["parado"], "reintentar": bool(r["reintentar"])}

    def save_night(self, d: date, *, attempt: datetime, created: int, stopped: str | None, retry: bool) -> None:
        motivo = None if stopped is None else " ".join(str(stopped).split())[:MAX_MOTIVO]
        self._q("""INSERT INTO nights (fecha, intento, creadas, parado, reintentar) VALUES (?,?,?,?,?)
                   ON CONFLICT (fecha) DO UPDATE SET intento = excluded.intento, creadas = excluded.creadas,
                   parado = excluded.parado, reintentar = excluded.reintentar""",
                (d.isoformat(), self._stamp(attempt), int(created), motivo, int(bool(retry))))

    def _mark_day(self, day: str, *, done: bool | None, notified: bool | None) -> None:
        """Con el candado ya cogido. `None` deja ese dato como estaba."""
        self._db.execute("INSERT OR IGNORE INTO days (fecha) VALUES (?)", (day,))
        if done is not None:
            self._db.execute("UPDATE days SET hecha = ? WHERE fecha = ?", (int(done), day))
        if notified is not None:
            self._db.execute("UPDATE days SET avisada = ? WHERE fecha = ?", (int(notified), day))


_SELECT = "SELECT q.*, c.due, c.repeticiones FROM questions q JOIN cards c ON c.question_id = q.id"
