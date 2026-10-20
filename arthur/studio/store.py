"""Base de datos de Creator Studio (SQLite, biblioteca estándar).

Va en su propio fichero, NO en memory.sqlite3: lo de Studio (trabajos, ideas, vídeos) crece mucho más que
tus recuerdos, y así se puede copiar, vaciar o borrar sin tocar tu memoria.

Fichero: <datos de ARTHUR>/studio/studio.sqlite3 (carpeta 700, fichero 600). En el servidor, dentro del
volumen /data del core.

Las tablas cambiarán según avance Studio: `PRAGMA user_version` dice qué versión del esquema tiene el
fichero, y `_migrate` lo pone al día paso a paso sin perder lo que ya hay.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ..core.config import data_dir

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]
StepStatus = Literal["running", "done", "failed"]
FINISHED: tuple[JobStatus, ...] = ("done", "failed", "cancelled")

MAX_PARAMS = 64 * 1024   # bytes de JSON por trabajo: lo que se pide, no los resultados grandes (esos van a ficheros)
MAX_OUTPUT = 256 * 1024  # bytes de JSON que guarda cada paso
MAX_CACHE = 1024 * 1024  # bytes de una respuesta de API guardada (una búsqueda de 50 vídeos ocupa ~60 KB)
MAX_ERROR = 2000         # caracteres del último error (lo justo para entenderlo, sin volcar trazas enteras)
KIND_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")

# Cada elemento sube el esquema una versión. Nunca se edita uno ya publicado: se añade otro detrás.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE jobs (
        id               INTEGER PRIMARY KEY,
        kind             TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
        params           TEXT NOT NULL,
        attempts         INTEGER NOT NULL DEFAULT 0,
        error            TEXT,
        next_run_at      TEXT NOT NULL,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    );
    CREATE INDEX jobs_pending ON jobs (status, next_run_at);
    CREATE TABLE job_steps (
        job_id      INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        seconds     REAL,
        cost_eur    REAL,
        output      TEXT,
        error       TEXT,
        started_at  TEXT,
        finished_at TEXT,
        PRIMARY KEY (job_id, name)
    );
    """,
    # 2: investigador (paso 2). Cuota gastada de cada API por día y respuestas guardadas para no gastarla dos veces.
    """
    CREATE TABLE api_usage (
        day   TEXT NOT NULL,
        api   TEXT NOT NULL,
        units INTEGER NOT NULL,
        PRIMARY KEY (day, api)
    );
    CREATE TABLE api_cache (
        key        TEXT PRIMARY KEY,
        body       TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    """,
    # 3: producción (paso 3). Cada imagen, música o clip de un vídeo, con su licencia: sin licencia válida, no se monta.
    """
    CREATE TABLE assets (
        id          INTEGER PRIMARY KEY,
        project     TEXT NOT NULL,
        name        TEXT NOT NULL,
        kind        TEXT NOT NULL,
        source      TEXT NOT NULL,
        source_url  TEXT NOT NULL,
        title       TEXT NOT NULL,
        license     TEXT NOT NULL,
        license_url TEXT,
        author      TEXT,
        attribution INTEGER NOT NULL,
        sha256      TEXT,
        status      TEXT NOT NULL CHECK (status IN ('ok', 'blocked')),
        reason      TEXT,
        fetched_at  TEXT NOT NULL,
        UNIQUE (project, name)
    );
    """,
    # 4: revisión (paso 4). Cada vídeo listo espera la decisión de Marc. La aprobación queda atada a las huellas
    # del vídeo, la miniatura y los textos: si algo cambia después, ya no vale y hay que volver a aprobar.
    """
    CREATE TABLE reviews (
        id           INTEGER PRIMARY KEY,
        project      TEXT NOT NULL,
        language     TEXT NOT NULL,
        channel      TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN
                         ('pending', 'approved', 'changes', 'discarded', 'replaced', 'published', 'failed')),
        title        TEXT NOT NULL,
        video_sha256 TEXT NOT NULL,
        thumb_sha256 TEXT NOT NULL,
        meta_sha256  TEXT NOT NULL,
        feedback     TEXT,
        decided_by   TEXT,
        decided_at   TEXT,
        youtube_id   TEXT,
        error        TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX reviews_by_project ON reviews (project, status);
    """,
    # 5: cortos verticales. Cada proyecto puede tener en revisión su vídeo largo y su corto, por separado.
    """
    ALTER TABLE reviews ADD COLUMN kind TEXT NOT NULL DEFAULT 'largo';
    """,
    # 6: perfil del canal (channels.py), separado del idioma: Frutinovelas también es en español. Las revisiones
    # que ya había se asignan por el @ que se aprobó (no por idioma), así cada una sigue yendo al mismo canal.
    # En la versión pública los @ dependen de la configuración, así que la migración no los escribe: una revisión
    # sin perfil se resuelve al leerla por su @ y, si no, por idioma (channels.for_review), al mismo canal que antes.
    """
    ALTER TABLE reviews ADD COLUMN profile TEXT NOT NULL DEFAULT '';
    """,
    # 7: Shorts importados (importer.py, Frutinovelas). `origin` = de dónde se copió el vídeo; vacío en todo lo que
    # monta Studio. Un vídeo importado nunca se aprueba solo: solo con «Aprobar» de Marc.
    """
    ALTER TABLE reviews ADD COLUMN origin TEXT NOT NULL DEFAULT '';
    """,
)


class StudioStoreError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    status: JobStatus
    params: dict[str, Any]
    attempts: int
    error: str | None
    next_run_at: datetime
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class JobStep:
    job_id: int
    name: str
    status: StepStatus
    attempts: int
    seconds: float | None
    cost_eur: float | None  # None = no se sabe (no es lo mismo que 0 €)
    output: dict[str, Any] | None
    error: str | None


def studio_dir() -> Path:
    return data_dir() / "studio"


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _to_json(value: Any, limit: int, what: str) -> str:
    if not isinstance(value, dict):
        raise StudioStoreError(f"{what} debe ser un diccionario")
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise StudioStoreError(f"{what} no se puede guardar como JSON: {error}") from None
    if len(text.encode()) > limit:
        raise StudioStoreError(f"{what} es demasiado grande (máximo {limit // 1024} KB)")
    return text


class StudioStore:
    def __init__(self, path: Path | None = None) -> None:
        """`path=Path(":memory:")` (o `StudioStore.in_memory()` en tests) no toca el disco."""
        self.path = path or studio_dir() / "studio.sqlite3"
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.path.exists():
                # Crear el fichero con permisos 600 ANTES de que SQLite escriba nada en él.
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        # Un solo hilo usa la conexión cada vez (candado), aunque la llamen hilos distintos
        # (la cola de trabajos, la web, las herramientas).
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._migrate()

    @classmethod
    def in_memory(cls) -> "StudioStore":
        return cls(Path(":memory:"))

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                # Un fichero de una versión MÁS NUEVA de ARTHUR: no lo tocamos, podríamos estropearlo.
                raise StudioStoreError(
                    f"studio.sqlite3 es de una versión más nueva de ARTHUR (esquema {version}); actualiza ARTHUR"
                )
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                # Cada migración entera o nada: si se corta a medias, el fichero queda como estaba.
                with self._db:
                    for statement in filter(str.strip, script.split(";")):
                        self._db.execute(statement)
                    self._db.execute(f"PRAGMA user_version = {number}")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._db.execute("PRAGMA user_version").fetchone()[0]

    # --- trabajos ----------------------------------------------------------

    def create_job(self, kind: str, params: dict[str, Any], *, now: datetime | None = None) -> Job:
        if not kind or len(kind) > 40 or not set(kind) <= KIND_CHARS:
            raise StudioStoreError(f"Tipo de trabajo no válido: {kind!r}")
        text = _to_json(params, MAX_PARAMS, "Los parámetros")
        moment = _iso(now or _now())
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO jobs (kind, status, params, next_run_at, created_at, updated_at) "
                "VALUES (?, 'queued', ?, ?, ?, ?)",
                (kind, text, moment, moment, moment),
            )
        return self.get_job(cursor.lastrowid)

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def list_jobs(self, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        """Los más recientes primero."""
        limit = max(1, min(limit, 500))
        with self._lock:
            if status:
                rows = self._db.execute(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = self._db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_job(r) for r in rows]

    def claim_next(self, now: datetime | None = None) -> Job | None:
        """Coge el trabajo en cola más antiguo que ya toque y lo marca «running», todo de una vez.

        Si dos hilos lo pidieran a la vez, solo uno se lo lleva (UPDATE … WHERE status='queued').
        """
        moment = _iso(now or _now())
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND next_run_at <= ? ORDER BY next_run_at, id LIMIT 1",
                (moment,),
            ).fetchone()
            if not row:
                return None
            changed = self._db.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
                (moment, row[0]),
            ).rowcount
        return self.get_job(row[0]) if changed else None

    def retry_later(self, job_id: int, error: str, when: datetime, *, now: datetime | None = None) -> None:
        """Vuelve a la cola para otro intento a partir de `when` (el paso que falló se repite)."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET status = 'queued', attempts = attempts + 1, error = ?, next_run_at = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (error[:MAX_ERROR], _iso(when), _iso(now or _now()), job_id),
            )

    def finish_job(self, job_id: int, status: JobStatus, error: str | None = None, *, now: datetime | None = None) -> None:
        if status not in FINISHED:
            raise StudioStoreError(f"Estado final no válido: {status}")
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error[:MAX_ERROR] if error else None, _iso(now or _now()), job_id),
            )

    def request_cancel(self, job_id: int, *, now: datetime | None = None) -> bool:
        """En cola: se cancela ya. En marcha: se avisa y la cola lo para al acabar el paso actual.

        Devuelve False si no existe o ya había terminado.
        """
        moment = _iso(now or _now())
        with self._lock, self._db:
            queued = self._db.execute(
                "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (moment, job_id),
            ).rowcount
            running = self._db.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status = 'running'",
                (moment, job_id),
            ).rowcount
        return bool(queued or running)

    def cancel_requested(self, job_id: int) -> bool:
        with self._lock:
            row = self._db.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row and row[0])

    def recover_interrupted(self, *, now: datetime | None = None) -> list[int]:
        """Al arrancar: lo que estaba «running» se quedó a medias (corte de luz, reinicio, contenedor parado).

        Vuelve a la cola y retomará desde el primer paso sin terminar; los pasos «done» no se repiten.
        Un paso que estaba en marcha queda como no hecho. Los que pidieron cancelación se cancelan.
        """
        moment = _iso(now or _now())
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute("SELECT id FROM jobs WHERE status = 'running'")]
            self._db.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE status = 'running' AND cancel_requested = 1",
                (moment,),
            )
            self._db.execute(
                "UPDATE jobs SET status = 'queued', next_run_at = ?, updated_at = ?, "
                "error = 'Interrumpido: se retoma desde el último paso terminado' WHERE status = 'running'",
                (moment, moment),
            )
            self._db.execute("DELETE FROM job_steps WHERE status = 'running'")
        return ids

    # --- pasos de un trabajo -----------------------------------------------

    def start_step(self, job_id: int, name: str, *, now: datetime | None = None) -> None:
        moment = _iso(now or _now())
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO job_steps (job_id, name, status, attempts, started_at) VALUES (?, ?, 'running', 1, ?) "
                "ON CONFLICT (job_id, name) DO UPDATE SET status = 'running', attempts = attempts + 1, "
                "error = NULL, started_at = excluded.started_at, finished_at = NULL",
                (job_id, name, moment),
            )

    def finish_step(self, job_id: int, name: str, output: dict[str, Any], seconds: float,
                    cost_eur: float | None = None, *, now: datetime | None = None) -> None:
        text = _to_json(output, MAX_OUTPUT, f"La salida del paso «{name}»")
        with self._lock, self._db:
            self._db.execute(
                "UPDATE job_steps SET status = 'done', output = ?, seconds = ?, cost_eur = ?, finished_at = ? "
                "WHERE job_id = ? AND name = ?",
                (text, round(seconds, 3), cost_eur, _iso(now or _now()), job_id, name),
            )

    def fail_step(self, job_id: int, name: str, error: str, seconds: float, *, now: datetime | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE job_steps SET status = 'failed', error = ?, seconds = ?, finished_at = ? "
                "WHERE job_id = ? AND name = ?",
                (error[:MAX_ERROR], round(seconds, 3), _iso(now or _now()), job_id, name),
            )

    def steps(self, job_id: int) -> list[JobStep]:
        with self._lock:
            rows = self._db.execute(
                "SELECT job_id, name, status, attempts, seconds, cost_eur, output, error FROM job_steps "
                "WHERE job_id = ? ORDER BY started_at, rowid",
                (job_id,),
            ).fetchall()
        return [JobStep(r[0], r[1], r[2], r[3], r[4], r[5], json.loads(r[6]) if r[6] else None, r[7]) for r in rows]


    # --- cuota y caché de las API (investigador) ---------------------------

    def add_usage(self, day: str, api: str, units: int) -> int:
        """Suma `units` a lo gastado hoy en `api` y devuelve el total del día."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO api_usage (day, api, units) VALUES (?, ?, ?) "
                "ON CONFLICT (day, api) DO UPDATE SET units = units + excluded.units",
                (day, api, units),
            )
            return self._db.execute("SELECT units FROM api_usage WHERE day = ? AND api = ?", (day, api)).fetchone()[0]

    def usage(self, day: str, api: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT units FROM api_usage WHERE day = ? AND api = ?", (day, api)).fetchone()
        return row[0] if row else 0

    def cache_get(self, key: str, newer_than: datetime) -> tuple[dict[str, Any], datetime] | None:
        """La respuesta guardada y cuándo se pidió, si es posterior a `newer_than`."""
        with self._lock:
            row = self._db.execute("SELECT body, fetched_at FROM api_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row[1])
        return (json.loads(row[0]), fetched) if fetched > newer_than else None

    def cache_put(self, key: str, body: dict[str, Any], *, now: datetime | None = None) -> None:
        text = _to_json(body, MAX_CACHE, "La respuesta")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO api_cache (key, body, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET body = excluded.body, fetched_at = excluded.fetched_at",
                (key, text, _iso(now or _now())),
            )

    def cache_prune(self, older_than: datetime) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM api_cache WHERE fetched_at < ?", (_iso(older_than),)).rowcount


    # --- recursos de un vídeo (imágenes…) con su licencia --------------------

    def save_asset(self, asset: "Asset", *, now: datetime | None = None) -> None:
        if asset.status not in ("ok", "blocked"):
            raise StudioStoreError(f"Estado de recurso no válido: {asset.status}")
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO assets ({_ASSET_COLUMNS}, fetched_at) VALUES ({', '.join('?' * 13)}, ?) "
                "ON CONFLICT (project, name) DO UPDATE SET kind = excluded.kind, source = excluded.source, "
                "source_url = excluded.source_url, title = excluded.title, license = excluded.license, "
                "license_url = excluded.license_url, author = excluded.author, attribution = excluded.attribution, "
                "sha256 = excluded.sha256, status = excluded.status, reason = excluded.reason, "
                "fetched_at = excluded.fetched_at",
                (asset.project, asset.name, asset.kind, asset.source, asset.source_url, asset.title, asset.license,
                 asset.license_url, asset.author, int(asset.attribution), asset.sha256, asset.status, asset.reason,
                 _iso(now or _now())),
            )

    def assets(self, project: str) -> list["Asset"]:
        with self._lock:
            rows = self._db.execute(f"SELECT {_ASSET_COLUMNS} FROM assets WHERE project = ? ORDER BY id",
                                    (project,)).fetchall()
        return [Asset(*r[:9], bool(r[9]), *r[10:]) for r in rows]


@dataclass(frozen=True)
class Asset:
    project: str
    name: str            # nombre del fichero dentro de la carpeta del proyecto
    kind: str            # «imagen» (más adelante: música, clip)
    source: str          # «commons»…
    source_url: str      # página del recurso, para comprobarlo
    title: str
    license: str         # código: «pd», «cc0», «cc-by-4.0»…
    license_url: str | None
    author: str | None
    attribution: bool    # la licencia obliga a citar al autor (va a la descripción del vídeo)
    sha256: str | None   # huella del fichero descargado
    status: str          # «ok» o «blocked» (bloquea el montaje)
    reason: str | None


_ASSET_COLUMNS = ("project, name, kind, source, source_url, title, license, license_url, author, attribution, "
                  "sha256, status, reason")
_JOB_COLUMNS = "id, kind, status, params, attempts, error, next_run_at, cancel_requested, created_at, updated_at"


def _job(row) -> Job:
    return Job(
        id=row[0], kind=row[1], status=row[2], params=json.loads(row[3]), attempts=row[4], error=row[5],
        next_run_at=datetime.fromisoformat(row[6]), cancel_requested=bool(row[7]),
        created_at=datetime.fromisoformat(row[8]), updated_at=datetime.fromisoformat(row[9]),
    )
