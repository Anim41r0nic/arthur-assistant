"""Almacén del panel: <datos>/panel/panel.sqlite3

  tasks      las tareas propias de Marc (`arthur`) y las que vienen de Studio (`studio`)
  briefings  el resumen de la mañana de cada día (uno por día; rehacerlo lo sustituye)

Esquema 2 (0.67.0, la revisión nocturna y semanal, `revision.py`):
  plan_fotos    la foto del plan de cada día (lo que tocaba hoy), tomada una sola vez a primera hora
  bloques       lo que Marc marcó en cada bloque de estudio del calendario (✓ hecho, ½ medio, ✗ no)
  dias          por día: pausa («hoy descanso»), lo último que se supo del Repaso, cuentas y puntos al cerrar
  revisiones    la revisión de la noche o de la semana: números, texto, motor y si llegó el aviso
  logros        las insignias conseguidas y su fecha (no se quitan nunca)
  preferencias  ajustes que se cambian desde la página (ocultar el juego)

Las casillas de Obsidian y los eventos del calendario NO se guardan aquí: se leen al momento y solo se
muestran, así nunca hay dos copias que se desfasen.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..core.config import data_dir

SCHEMA_VERSION = 2
MARCAS = ("hecho", "medio", "no")
MAX_TITLE = 200
SOURCES = ("arthur", "studio")
MEMORIA = Path(":memory:")


def panel_dir() -> Path:
    return data_dir() / "panel"


def _madrid_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid"))


class PanelError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    source: str
    due: str | None          # día ISO (2026-10-16) o None si no tiene fecha
    created_at: str          # momento ISO con zona (2026-10-16T08:30:00+02:00)
    done_at: str | None
    studio_ref: str | None   # id del trabajo de Studio: evita crear la misma tarea cada vez que se sincroniza


class PanelStore:
    def __init__(self, root: Path | None = None, *, now: Callable[[], datetime] = _madrid_now) -> None:
        """`root=Path(":memory:")` (o `PanelStore.in_memory()` en tests) no toca el disco."""
        self._now = now
        self.root = root or panel_dir()
        memoria = self.root == MEMORIA
        if memoria:
            target = ":memory:"
        else:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = str(self.root / "panel.sqlite3")
        # Una conexión compartida entre el hilo de la web y el del planificador; el candado las ordena.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if not memoria:
            self._db.execute("PRAGMA journal_mode=WAL")
        if self._db.execute("PRAGMA user_version").fetchone()[0] < 1:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    due TEXT,
                    created_at TEXT NOT NULL,
                    done_at TEXT,
                    studio_ref TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_studio_ref ON tasks (studio_ref) WHERE studio_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS briefings (
                    date TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    delivered INTEGER NOT NULL);
                PRAGMA user_version = 1;
            """)
        if self._db.execute("PRAGMA user_version").fetchone()[0] < 2:
            # Solo tablas nuevas: las tareas y los resúmenes de antes no se tocan.
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS plan_fotos (
                    fecha TEXT PRIMARY KEY,
                    tomada_en TEXT NOT NULL,
                    datos TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bloques (
                    fecha TEXT NOT NULL,
                    clave TEXT NOT NULL,
                    titulo TEXT NOT NULL,
                    inicio TEXT NOT NULL,
                    fin TEXT NOT NULL,
                    marca TEXT NOT NULL,
                    marcado_en TEXT NOT NULL,
                    PRIMARY KEY (fecha, clave));
                CREATE TABLE IF NOT EXISTS dias (
                    fecha TEXT PRIMARY KEY,
                    pausa INTEGER NOT NULL DEFAULT 0,
                    repaso TEXT,
                    datos TEXT,
                    puntos INTEGER NOT NULL DEFAULT 0,
                    cerrado INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS revisiones (
                    fecha TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    creada_en TEXT NOT NULL,
                    datos TEXT NOT NULL,
                    texto TEXT NOT NULL,
                    motor TEXT NOT NULL,
                    avisada INTEGER NOT NULL,
                    leida_en TEXT,
                    PRIMARY KEY (fecha, tipo));
                CREATE TABLE IF NOT EXISTS logros (
                    id TEXT PRIMARY KEY,
                    fecha TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preferencias (
                    clave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL);
                PRAGMA user_version = 2;
            """)

    @classmethod
    def in_memory(cls, *, now: Callable[[], datetime] = _madrid_now) -> "PanelStore":
        return cls(MEMORIA, now=now)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _stamp(self) -> str:
        return self._now().replace(microsecond=0).isoformat()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- tareas ---
    def add_task(self, title: str, due: date | None, source: str = "arthur", studio_ref: str | None = None) -> Task:
        title = " ".join(str(title).split())
        if not title or len(title) > MAX_TITLE:
            raise PanelError(f"El título debe tener entre 1 y {MAX_TITLE} caracteres")
        if source not in SOURCES:
            raise PanelError(f"Fuente de tarea desconocida: {source}")
        with self._lock:
            if studio_ref is not None:
                rows = self._db.execute("SELECT * FROM tasks WHERE studio_ref = ?", (studio_ref,)).fetchall()
                if rows:
                    return _task(rows[0])
            cur = self._db.execute(
                "INSERT INTO tasks (title, source, due, created_at, studio_ref) VALUES (?,?,?,?,?)",
                (title, source, due.isoformat() if due else None, self._stamp(), studio_ref))
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _task(row)

    def toggle(self, task_id: int) -> Task:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            self._db.execute("UPDATE tasks SET done_at = ? WHERE id = ?",
                             (None if row["done_at"] else self._stamp(), task_id))
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row)

    def tasks_for(self, day: date) -> list[Task]:
        """Sin hacer: las de ese día, las sin fecha y las atrasadas (siguen saliendo hasta que se hagan).
        Hechas: solo las que se hicieron ese día, para que la lista de ayer no se arrastre para siempre."""
        d = day.isoformat()
        pendientes = self._q(
            "SELECT * FROM tasks WHERE done_at IS NULL AND (due IS NULL OR due <= ?) "
            "ORDER BY due IS NULL, due, id", (d,))
        hechas = self._q("SELECT * FROM tasks WHERE done_at IS NOT NULL AND substr(done_at, 1, 10) = ? "
                         "ORDER BY done_at, id", (d,))
        return [_task(r) for r in (*pendientes, *hechas)]

    # --- resumen de la mañana ---
    def briefing_sent(self, day: date) -> bool:
        return bool(self._q("SELECT 1 FROM briefings WHERE date = ? AND delivered = 1", (day.isoformat(),)))

    def save_briefing(self, day: date, text: str, engine: str, delivered: bool) -> None:
        self._q("INSERT OR REPLACE INTO briefings (date, sent_at, text, engine, delivered) VALUES (?,?,?,?,?)",
                (day.isoformat(), self._stamp(), text, engine, int(delivered)))

    def last_briefing(self, day: date) -> dict | None:
        rows = self._q("SELECT * FROM briefings WHERE date = ?", (day.isoformat(),))
        if not rows:
            return None
        r = rows[0]
        return {"date": r["date"], "sent_at": r["sent_at"], "text": r["text"], "engine": r["engine"],
                "delivered": bool(r["delivered"])}


    # --- revisión: tareas para la foto y las cuentas ---
    def task(self, task_id: int) -> Task | None:
        rows = self._q("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return _task(rows[0]) if rows else None

    def plan_candidates(self, day: date) -> list[Task]:
        """Tareas con fecha de ese día, incluidas las ya hechas hoy si la foto llega tarde.

        Una atrasada sigue visible en el panel, pero solo afecta al porcentaje el día que vencía.
        """
        d = day.isoformat()
        return [_task(r) for r in self._q(
            "SELECT * FROM tasks WHERE due = ? "
            "AND (done_at IS NULL OR substr(done_at, 1, 10) >= ?) ORDER BY due, id", (d, d))]

    def done_on(self, day: date) -> list[Task]:
        return [_task(r) for r in self._q(
            "SELECT * FROM tasks WHERE done_at IS NOT NULL AND substr(done_at, 1, 10) = ? ORDER BY done_at, id",
            (day.isoformat(),))]

    # --- revisión: foto del plan ---
    def save_photo(self, day: date, data: dict) -> dict:
        """Guarda la foto si ese día aún no tiene; si ya tenía, devuelve la de antes sin tocarla."""
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO plan_fotos (fecha, tomada_en, datos) VALUES (?,?,?)",
                             (day.isoformat(), self._stamp(), json.dumps(data, ensure_ascii=False)))
        return self.photo(day)

    def photo(self, day: date) -> dict | None:
        rows = self._q("SELECT * FROM plan_fotos WHERE fecha = ?", (day.isoformat(),))
        if not rows:
            return None
        return {**json.loads(rows[0]["datos"]), "tomada_en": rows[0]["tomada_en"]}

    # --- revisión: bloques de estudio ---
    def set_mark(self, day: date, key: str, title: str, start: str, end: str, mark: str | None) -> None:
        """Marca un bloque (hecho, medio, no) o le quita la marca (None): vuelve a «sin marcar»."""
        if mark is not None and mark not in MARCAS:
            raise PanelError(f"Marca desconocida: {mark}")
        with self._lock:
            if mark is None:
                self._db.execute("DELETE FROM bloques WHERE fecha = ? AND clave = ?", (day.isoformat(), key))
            else:
                self._db.execute(
                    "INSERT OR REPLACE INTO bloques (fecha, clave, titulo, inicio, fin, marca, marcado_en) "
                    "VALUES (?,?,?,?,?,?,?)", (day.isoformat(), key, title, start, end, mark, self._stamp()))

    def marks(self, day: date) -> dict[str, dict]:
        return {r["clave"]: {"titulo": r["titulo"], "inicio": r["inicio"], "fin": r["fin"], "marca": r["marca"]}
                for r in self._q("SELECT * FROM bloques WHERE fecha = ?", (day.isoformat(),))}

    # --- revisión: días ---
    def _ensure_day(self, day: date) -> None:
        self._db.execute("INSERT OR IGNORE INTO dias (fecha) VALUES (?)", (day.isoformat(),))

    def touch_day(self, day: date) -> None:
        """Crea la fila del día si no existe: así el día cuenta como «con datos» aunque no se haga nada."""
        with self._lock:
            self._ensure_day(day)

    def day(self, day: date) -> dict | None:
        rows = self._q("SELECT * FROM dias WHERE fecha = ?", (day.isoformat(),))
        return _day(rows[0]) if rows else None

    def days(self) -> dict[date, dict]:
        return {date.fromisoformat(r["fecha"]): _day(r) for r in self._q("SELECT * FROM dias ORDER BY fecha")}

    def set_pause(self, day: date, on: bool) -> None:
        with self._lock:
            self._ensure_day(day)
            self._db.execute("UPDATE dias SET pausa = ? WHERE fecha = ?", (int(bool(on)), day.isoformat()))

    def set_repaso(self, day: date, info: dict) -> None:
        with self._lock:
            self._ensure_day(day)
            self._db.execute("UPDATE dias SET repaso = ? WHERE fecha = ?", (json.dumps(info), day.isoformat()))

    def save_day(self, day: date, data: dict, points: int, *, close: bool) -> None:
        """Guarda las cuentas de un día. Si el día ya estaba cerrado, los puntos solo pueden subir (nada se resta
        nunca); mientras está abierto (hoy) se guardan tal cual, porque desmarcar un error no es una penalización."""
        with self._lock:
            self._ensure_day(day)
            row = self._db.execute("SELECT puntos, cerrado FROM dias WHERE fecha = ?", (day.isoformat(),)).fetchone()
            if row["cerrado"]:
                points = max(points, row["puntos"])
            self._db.execute("UPDATE dias SET datos = ?, puntos = ?, cerrado = ? WHERE fecha = ?",
                             (json.dumps(data, ensure_ascii=False), int(points), int(close or row["cerrado"]),
                              day.isoformat()))

    # --- revisión: revisiones de la noche y de la semana ---
    def save_revision(self, day: date, kind: str, data: dict, text: str, engine: str, delivered: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO revisiones (fecha, tipo, creada_en, datos, texto, motor, avisada, leida_en) "
                "VALUES (?,?,?,?,?,?,?, (SELECT leida_en FROM revisiones WHERE fecha = ? AND tipo = ?))",
                (day.isoformat(), kind, self._stamp(), json.dumps(data, ensure_ascii=False), text, engine,
                 int(delivered), day.isoformat(), kind))

    def revision(self, day: date, kind: str) -> dict | None:
        rows = self._q("SELECT * FROM revisiones WHERE fecha = ? AND tipo = ?", (day.isoformat(), kind))
        if not rows:
            return None
        r = rows[0]
        return {"fecha": r["fecha"], "tipo": r["tipo"], "creada_en": r["creada_en"], "datos": json.loads(r["datos"]),
                "texto": r["texto"], "motor": r["motor"], "avisada": bool(r["avisada"]), "leida_en": r["leida_en"]}

    def mark_week_read(self, day: date) -> bool:
        """Marca leído el resumen semanal de ese domingo (para la insignia «Mirar la semana»)."""
        with self._lock:
            cur = self._db.execute("UPDATE revisiones SET leida_en = COALESCE(leida_en, ?) "
                                   "WHERE fecha = ? AND tipo = 'semana'", (self._stamp(), day.isoformat()))
            return cur.rowcount > 0

    def first_week_read(self) -> date | None:
        rows = self._q("SELECT MIN(leida_en) FROM revisiones WHERE tipo = 'semana' AND leida_en IS NOT NULL")
        return date.fromisoformat(rows[0][0][:10]) if rows and rows[0][0] else None

    # --- revisión: insignias y preferencias ---
    def add_badges(self, earned: dict[str, date]) -> None:
        """Las nuevas se guardan con su fecha; las que ya estaban no cambian (ni se quitan)."""
        with self._lock:
            self._db.executemany("INSERT OR IGNORE INTO logros (id, fecha) VALUES (?, ?)",
                                 [(k, d.isoformat()) for k, d in earned.items()])

    def badges(self) -> dict[str, date]:
        return {r["id"]: date.fromisoformat(r["fecha"]) for r in self._q("SELECT * FROM logros")}

    def pref(self, key: str, default: str) -> str:
        rows = self._q("SELECT valor FROM preferencias WHERE clave = ?", (key,))
        return rows[0][0] if rows else default

    def set_pref(self, key: str, value: str) -> None:
        self._q("INSERT OR REPLACE INTO preferencias (clave, valor) VALUES (?, ?)", (key, value))


def _day(row: sqlite3.Row) -> dict:
    return {"fecha": row["fecha"], "pausa": bool(row["pausa"]),
            "repaso": json.loads(row["repaso"]) if row["repaso"] else None,
            "datos": json.loads(row["datos"]) if row["datos"] else None,
            "puntos": row["puntos"], "cerrado": bool(row["cerrado"])}


def _task(row: sqlite3.Row) -> Task:
    return Task(id=row["id"], title=row["title"], source=row["source"], due=row["due"],
                created_at=row["created_at"], done_at=row["done_at"], studio_ref=row["studio_ref"])
