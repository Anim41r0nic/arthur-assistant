"""El índice del cerebro: <datos>/cerebro/indice.sqlite3.

Guarda cada nota, apunte o PDF (`docs`), sus trozos (`chunks`), un índice de palabras (`chunks_fts`, SQLite FTS5) y
los `[[enlaces]]` de Obsidian (`links`). Cada trozo lleva además su embedding (1 024 números en float16, 2 KB):
vacío = pendiente; mientras tanto ese trozo solo se encuentra por palabras.

Lo que importa:

- Solo se reindexa lo que cambió: cada documento guarda el SHA-256 de su texto. Cada pasada (cada 15 min) lee todas
  las notas, pero si el hash es el mismo no se toca nada, y los embeddings (que cuestan Neurons) se conservan.
- Renombrar o mover una nota en Obsidian = una ruta nueva con el mismo texto. Si la ruta vieja ha desaparecido y su
  hash es el mismo, se le cambia la ruta en vez de crear otra nota: ni duplicados, ni restos, ni embeddings repetidos.
- Cada trozo tiene además una CLAVE estable (`chunk_key`: fuente + texto normalizado). Su número (`id`) cambia
  cada vez que se rehace el documento; la clave no, mientras su texto siga igual. El Repaso liga sus preguntas a la
  clave para no volver a pedirlas (ni perderlas) cuando Marc retoca otra parte del apunte. Quien quiera enterarse de
  los cambios se apunta con `on_change`.
- Lo que ya no existe se borra (trozos, palabras y enlaces). Un escaneo que no da NINGUNA nota no borra nada: eso es
  una bóveda sin montar (Syncthing caído), no que Marc lo haya borrado todo.
- El índice de palabras quita las tildes (`remove_diacritics 2`): «limite sucesion» encuentra «Límite de una sucesión».
- Un único escritor (el reindexador) y una conexión compartida protegida con un candado; WAL para que leer mientras se
  escribe no se bloquee.

numpy es opcional: en la imagen del servidor está (búsquedas por significado en milisegundos); sin él (la batería de
tests del portátil) se calcula el coseno en Python puro, más lento pero con el mismo resultado.
"""

from __future__ import annotations

import hashlib
import logging
import math
import operator
import sqlite3
import struct
import threading
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..notes.engines import EngineError
from .chunks import SEPARADOR, Chunk, Doc, split
from .embed import BATCH as _EMBED_BATCH
from .embed import DIM

try:  # solo dentro de arthur/cerebro/ (Global Constraints): el núcleo sigue con biblioteca estándar
    import numpy as np
except ImportError:  # pragma: no cover - depende de lo instalado
    np = None

log = logging.getLogger(__name__)

BATCH = _EMBED_BATCH  # textos por llamada a embed (se puede bajar en los tests)
_PACK = struct.Struct(f"<{DIM}e")  # float16 en little-endian: 2 048 bytes por vector
_BLOCK = 4096  # filas por bloque al multiplicar con numpy: pasar a float32 de golpe 10 000 × 1 024 serían 40 MB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    title TEXT NOT NULL,
    subject TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    hash TEXT NOT NULL,
    UNIQUE (source, ref)
);
CREATE INDEX IF NOT EXISTS docs_hash ON docs (source, hash);
-- AUTOINCREMENT: un id de trozo borrado nunca se reutiliza. Así un embedding calculado para un trozo que cambió
-- mientras tanto no puede acabar pegado al trozo nuevo que heredase su número.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES docs (id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    heading TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_pending ON chunks (id) WHERE embedding IS NULL;
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (heading, text, tokenize = 'unicode61 remove_diacritics 2');
-- Las palabras del índice (sin tildes), leídas en vivo del propio FTS5: no ocupa nada ni hay que mantenerla.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab (chunks_fts, row);
CREATE TABLE IF NOT EXISTS links (
    doc_id INTEGER NOT NULL REFERENCES docs (id) ON DELETE CASCADE,
    target TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_doc ON links (doc_id);
"""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def chunk_key(source: str, text: str) -> str:
    """La identidad estable de un trozo: no depende de su número ni del documento en que esté, solo de la fuente y de
    su texto (sin contar espacios ni saltos de línea de más). Así sobrevive a reindexar, renombrar o mover el
    apunte; si cambia una sola palabra, es otro trozo. Dos trozos idénticos en dos apuntes comparten clave: son
    la misma materia y no hace falta preguntarla dos veces."""
    norm = " ".join(unicodedata.normalize("NFC", text).split())
    return text_hash(f"{source}\x00{norm}")


def source_matches(source: str, sources: Sequence[str] | None) -> bool:
    """¿Entra `source` en el filtro? «obsidian» vale para las dos bóvedas (`obsidian:cerebro`, `obsidian:…`)."""
    return sources is None or any(source == s or source.startswith(s + ":") for s in sources)


def _sources_sql(sources: Sequence[str] | None, column: str = "d.source") -> tuple[str, list[str]]:
    if sources is None:
        return "", []
    if not sources:
        return " AND 0", []
    parts, args = [], []
    for s in sources:
        # Comparación exacta o «prefijo:» con substr: con LIKE, un «%» o «_» en el filtro haría de comodín.
        parts.append(f"({column} = ? OR substr({column}, 1, length(?) + 1) = ? || ':')")
        args += [s, s, s]
    return " AND (" + " OR ".join(parts) + ")", args


@dataclass
class _Plan:
    """Lo que hay que escribir en una pasada de `sync` (se calcula fuera del candado: trocear lleva su tiempo)."""

    new: list[tuple[Doc, str, list[Chunk]]]
    changed: list[tuple[int, Doc, str, list[Chunk]]]
    renamed: list[tuple[int, Doc]]
    meta: list[tuple[int, Doc]]
    dead: list[int]


class Index:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._cache: tuple[list[int], list[str], object] | None = None  # (ids, fuentes, matriz) de los vectores
        self._embed_error: str | None = None
        self._listeners: list[Callable[[str], object]] = []

    def on_change(self, fn: Callable[[str], object]) -> None:
        """`fn(fuente)` se llama tras cada `sync` que cambie algo de esa fuente (fuera del candado, en el hilo del
        reindexador). Un oyente que falla se apunta en el registro y no para el reindexado."""
        self._listeners.append(fn)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args: Sequence = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    # ── Escribir ────────────────────────────────────────────────────────────────────────────────────────────────

    def sync(self, docs: Iterable[Doc], *, source: str, alive_refs: set[str] | None = None) -> dict:
        """Pone el índice de una fuente al día con lo que acaba de leer.

        `docs` son los documentos leídos. `alive_refs` son TODAS las refs que siguen existiendo en la fuente:
        Apuntes manda solo lo cambiado desde la última vez, así que lo vivo viene aparte (`apuntes.ids`). Si es None,
        `docs` es un escaneo completo (Obsidian, PDF) y lo que no esté en él se borra. Devuelve cuántos documentos
        son nuevos, cuántos cambiaron (o se renombraron) y cuántos se han borrado."""
        incoming: dict[str, Doc] = {}
        for d in docs:
            if d.source != source:
                raise ValueError(f"Documento de {d.source!r} en la pasada de {source!r}")
            incoming[d.ref] = d  # una ref repetida: vale la última
        existing = {r["ref"]: r for r in self._q(
            "SELECT id, ref, title, subject, changed_at, hash FROM docs WHERE source = ?", (source,))}
        plan = self._plan(source, incoming, existing, alive_refs)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._apply(source, plan)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            if plan.new or plan.changed or plan.dead:
                self._cache = None
        if plan.new or plan.changed or plan.renamed or plan.meta or plan.dead:
            for fn in list(self._listeners):
                try:
                    fn(source)
                except Exception:  # noqa: BLE001 — el índice ya está al día; lo de fuera no debe tumbarlo
                    log.exception("Cerebro: un aviso de cambios del índice ha fallado")
        return {"nuevos": len(plan.new), "cambiados": len(plan.changed) + len(plan.renamed),
                "borrados": len(plan.dead)}

    def _plan(self, source: str, incoming: dict[str, Doc], existing: dict[str, sqlite3.Row],
              alive_refs: set[str] | None) -> _Plan:
        if alive_refs is None and not incoming and existing:
            log.warning("Cerebro: %s no ha dado ninguna nota; no se borra nada (¿carpeta sin montar?)", source)
            alive = set(existing)
        else:
            alive = set(incoming) | (alive_refs or set())
        # Los que van a desaparecer, por hash: candidatos a «es la misma nota con otro nombre».
        gone: dict[str, list[sqlite3.Row]] = {}
        for ref, row in existing.items():
            if ref not in alive:
                gone.setdefault(row["hash"], []).append(row)
        plan = _Plan([], [], [], [], [])
        for ref, d in incoming.items():
            h = text_hash(d.text)
            row = existing.get(ref)
            if row is not None:
                if row["hash"] != h:
                    plan.changed.append((row["id"], d, h, split(d)))
                elif (row["title"], row["subject"], row["changed_at"]) != (d.title, d.subject, d.changed_at):
                    plan.meta.append((row["id"], d))
            elif gone.get(h):
                plan.renamed.append((gone[h].pop()["id"], d))
            else:
                plan.new.append((d, h, split(d)))
        plan.dead = [row["id"] for rows in gone.values() for row in rows]
        return plan

    def _apply(self, source: str, plan: _Plan) -> None:
        db = self._db
        for doc_id in plan.dead:
            self._drop_chunks(doc_id)
            db.execute("DELETE FROM docs WHERE id = ?", (doc_id,))  # los enlaces se van en cascada
        for doc_id, d in plan.renamed:
            db.execute("UPDATE docs SET ref = ?, title = ?, subject = ?, changed_at = ? WHERE id = ?",
                       (d.ref, d.title, d.subject, d.changed_at, doc_id))
        for doc_id, d in plan.meta:
            db.execute("UPDATE docs SET title = ?, subject = ?, changed_at = ? WHERE id = ?",
                       (d.title, d.subject, d.changed_at, doc_id))
        for doc_id, d, h, chunks in plan.changed:
            self._drop_chunks(doc_id)
            db.execute("DELETE FROM links WHERE doc_id = ?", (doc_id,))
            db.execute("UPDATE docs SET title = ?, subject = ?, changed_at = ?, hash = ? WHERE id = ?",
                       (d.title, d.subject, d.changed_at, h, doc_id))
            self._add_chunks(doc_id, d, chunks)
        for d, h, chunks in plan.new:
            doc_id = db.execute(
                "INSERT INTO docs (source, ref, title, subject, changed_at, hash) VALUES (?, ?, ?, ?, ?, ?)",
                (source, d.ref, d.title, d.subject, d.changed_at, h)).lastrowid
            self._add_chunks(doc_id, d, chunks)

    def _drop_chunks(self, doc_id: int) -> None:
        # FTS5 no entiende de claves foráneas: sus filas se quitan a mano, antes que los trozos.
        self._db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
        self._db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    def _add_chunks(self, doc_id: int, d: Doc, chunks: list[Chunk]) -> None:
        for c in chunks:
            cid = self._db.execute("INSERT INTO chunks (doc_id, n, heading, text) VALUES (?, ?, ?, ?)",
                                   (doc_id, c.n, c.heading, c.text)).lastrowid
            self._db.execute("INSERT INTO chunks_fts (rowid, heading, text) VALUES (?, ?, ?)",
                             (cid, c.heading, c.text))
        self._db.executemany("INSERT INTO links (doc_id, target) VALUES (?, ?)", [(doc_id, t) for t in d.links])

    def embed_pending(self, embedder, limit: int = 500) -> int:
        """Calcula los embeddings que faltan, de `BATCH` en `BATCH`, hasta `limit` trozos. Devuelve cuántos guardó.

        Se para en cuanto el embedder dice que no (presupuesto gastado, Workers AI caído): lo que falte se hace en la
        siguiente pasada. La llamada a la red va FUERA del candado, para no dejar las búsquedas esperando."""
        stored = tried = last_id = 0
        self._embed_error = None
        while tried < limit:
            rows = self._q(
                "SELECT c.id, c.heading, c.text, d.title FROM chunks c JOIN docs d ON d.id = c.doc_id "
                "WHERE c.embedding IS NULL AND c.id > ? ORDER BY c.id LIMIT ?",
                (last_id, min(BATCH, limit - tried)))
            if not rows:
                break
            tried += len(rows)
            last_id = rows[-1]["id"]
            try:
                vectors = embedder.embed([_embed_text(r["title"], r["heading"], r["text"]) for r in rows])
            except EngineError as e:
                self._embed_error = str(e)
                log.info("Cerebro: embeddings parados (%s); siguen en la próxima pasada", type(e).__name__)
                break
            if len(vectors) != len(rows):
                self._embed_error = "El embedder ha devuelto un número de vectores distinto"
                break
            with self._lock:
                for r, v in zip(rows, vectors):
                    # «AND embedding IS NULL»: si el trozo se rehízo mientras tanto, su id ya no existe y no se toca.
                    cur = self._db.execute("UPDATE chunks SET embedding = ? WHERE id = ? AND embedding IS NULL",
                                           (_PACK.pack(*v), r["id"]))
                    stored += cur.rowcount
                self._cache = None
        return stored

    # ── Leer ────────────────────────────────────────────────────────────────────────────────────────────────────

    def chunks_fts(self, query: str, limit: int, sources: tuple[str, ...] | None) -> list[tuple[int, float]]:
        """Trozos que casan con una consulta FTS5 (`search.fts_query` la construye segura), del mejor al peor.

        La puntuación es BM25 cambiada de signo (más alto = mejor). Una consulta mal formada da lista vacía en vez de
        tumbar la pregunta."""
        where, args = _sources_sql(sources)
        try:
            rows = self._q(
                "SELECT f.rowid AS id, bm25(chunks_fts) AS rank FROM chunks_fts f "
                "JOIN chunks c ON c.id = f.rowid JOIN docs d ON d.id = c.doc_id "
                f"WHERE chunks_fts MATCH ?{where} ORDER BY rank LIMIT ?", [query, *args, int(limit)])
        except sqlite3.OperationalError as e:
            log.debug("Cerebro: consulta de palabras no válida (%s)", e)
            return []
        return [(r["id"], -r["rank"]) for r in rows]

    def count_fts(self, query: str) -> int:
        """En cuántos trozos casa una consulta FTS5 (trozos, no apariciones). 0 si la consulta no vale."""
        try:
            rows = self._q("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (query,))
        except sqlite3.OperationalError as e:
            log.debug("Cerebro: consulta de palabras no válida (%s)", e)
            return 0
        return int(rows[0][0])

    def chunk_count(self) -> int:
        return int(self._q("SELECT count(*) FROM chunks")[0][0])

    def vocabulary(self, min_len: int) -> list[str]:
        """Las palabras del índice (sin tildes, en minúsculas) de `min_len` letras o más."""
        return [r[0] for r in self._q("SELECT term FROM chunks_vocab WHERE length(term) >= ?", (int(min_len),))]

    def doc_names(self) -> list[str]:
        """«título ruta» de cada documento: palabras que nombran la nota pero no están en el índice de palabras
        (que guarda encabezado y texto), como «Tema1» o la carpeta «MTP 1»."""
        return [f"{r['title']} {r['ref']}" for r in self._q("SELECT title, ref FROM docs")]

    def chunk(self, chunk_id: int) -> dict | None:
        rows = self._q(
            "SELECT c.id, c.doc_id, c.n, c.heading, c.text, d.title, d.source, d.ref, d.subject, d.changed_at, "
            "c.embedding IS NOT NULL AS embedded FROM chunks c JOIN docs d ON d.id = c.doc_id WHERE c.id = ?",
            (chunk_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["embedded"] = bool(row["embedded"])  # ¿tiene ya su embedding? (si no, solo se encuentra por palabras)
        row["key"] = chunk_key(row["source"], row["text"])
        return row

    def chunk_ids(self, source: str, subjects: Iterable[str]) -> list[int]:
        """Los trozos de una fuente (exacta) cuyas asignaturas están en `subjects`: los de los documentos cambiados
        más recientemente primero y, dentro de cada uno, en su orden. Es lo que recorre el Repaso por la noche."""
        subjects = sorted(set(subjects))
        if not subjects:
            return []
        marks = ",".join("?" * len(subjects))
        rows = self._q(
            "SELECT c.id FROM chunks c JOIN docs d ON d.id = c.doc_id "
            f"WHERE d.source = ? AND d.subject IN ({marks}) ORDER BY d.changed_at DESC, d.id DESC, c.n",
            [source, *subjects])
        return [r["id"] for r in rows]

    def chunk_keys(self, source: str, subjects: Iterable[str] | None = None) -> list[tuple[int, str]]:
        """Como `chunk_ids` pero con la clave de cada trozo: [(id, clave)], en el mismo orden. Sin `subjects`, los de
        todas las asignaturas (lo que necesita el Repaso para saber qué preguntas siguen teniendo su trozo)."""
        return [(r["id"], chunk_key(source, r["text"])) for r in self._chunk_rows(source, subjects)]

    def chunk_map(self, source: str) -> dict[str, tuple[int, str]]:
        """{clave: (id, asignatura)} de todos los trozos de una fuente. Si dos trozos tienen la misma clave (el mismo
        texto en dos apuntes) vale el del documento cambiado más recientemente."""
        out: dict[str, tuple[int, str]] = {}
        for r in self._chunk_rows(source, None):
            out.setdefault(chunk_key(source, r["text"]), (r["id"], r["subject"]))
        return out

    def _chunk_rows(self, source: str, subjects: Iterable[str] | None) -> list[sqlite3.Row]:
        where, args = "", []
        if subjects is not None:
            subjects = sorted(set(subjects))
            if not subjects:
                return []
            where, args = f" AND d.subject IN ({','.join('?' * len(subjects))})", subjects
        return self._q(
            "SELECT c.id, c.text, d.subject FROM chunks c JOIN docs d ON d.id = c.doc_id "
            f"WHERE d.source = ?{where} ORDER BY d.changed_at DESC, d.id DESC, c.n", [source, *args])

    def subjects(self, source: str) -> list[str]:
        """Las asignaturas que hay en una fuente (para elegir las activas del Repaso en la página)."""
        return [r["subject"] for r in self._q(
            "SELECT DISTINCT subject FROM docs WHERE source = ? AND subject != '' ORDER BY subject", (source,))]

    def vectors(self) -> tuple[list[int], object]:
        """Los ids de los trozos con embedding y su matriz (una fila por trozo, normalizada a longitud 1).

        Con numpy es un array float16 (n × 1 024: ~2 KB por trozo en memoria); sin numpy, una lista de tuplas.
        Se guarda en memoria hasta que cambie algo en el índice."""
        ids, _, matrix = self._vectors()
        return ids, matrix

    def nearest(self, vector: Sequence[float], n: int, sources: tuple[str, ...] | None) -> list[tuple[int, float]]:
        """Los `n` trozos más parecidos a `vector` por coseno, del más al menos parecido: [(id, coseno)]."""
        norm = math.sqrt(sum(x * x for x in vector))
        if not norm or n <= 0:
            return []
        ids, srcs, matrix = self._vectors()
        keep = [i for i, s in enumerate(srcs) if source_matches(s, sources)]
        if not keep:
            return []
        if np is not None and not isinstance(matrix, list):
            q = np.asarray(vector, dtype=np.float32) / norm
            sub = matrix if len(keep) == len(ids) else matrix[keep]
            scores = np.concatenate([sub[k:k + _BLOCK].astype(np.float32) @ q for k in range(0, len(sub), _BLOCK)])
            order = np.argsort(-scores, kind="stable")[:n]
            return [(ids[keep[i]], float(scores[i])) for i in order]
        q = [x / norm for x in vector]
        scored = [(ids[i], sum(map(operator.mul, matrix[i], q))) for i in keep]
        scored.sort(key=lambda t: -t[1])
        return scored[:n]

    def _vectors(self) -> tuple[list[int], list[str], object]:
        cache = self._cache
        if cache is not None:
            return cache
        rows = self._q("SELECT c.id, d.source, c.embedding FROM chunks c JOIN docs d ON d.id = c.doc_id "
                       "WHERE c.embedding IS NOT NULL ORDER BY c.id")
        ids = [r["id"] for r in rows]
        srcs = [r["source"] for r in rows]
        if np is not None:
            raw = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype="<f2").reshape(len(rows), DIM)
            m32 = raw.astype(np.float32)
            lengths = np.linalg.norm(m32, axis=1, keepdims=True)
            lengths[lengths == 0] = 1.0
            matrix: object = (m32 / lengths).astype(np.float16)
        else:
            matrix = [_normalized(_PACK.unpack(r["embedding"])) for r in rows]
        cache = (ids, srcs, matrix)
        with self._lock:
            self._cache = cache
        return cache

    def stats(self) -> dict:
        rows = self._q(
            "SELECT d.source, COUNT(DISTINCT d.id) AS docs, COUNT(c.id) AS chunks, "
            "SUM(CASE WHEN c.id IS NOT NULL AND c.embedding IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM docs d LEFT JOIN chunks c ON c.doc_id = d.id GROUP BY d.source ORDER BY d.source")
        fuentes = {r["source"]: {"documentos": r["docs"], "trozos": r["chunks"], "pendientes": r["pending"] or 0}
                   for r in rows}
        return {"documentos": sum(f["documentos"] for f in fuentes.values()),
                "trozos": sum(f["trozos"] for f in fuentes.values()),
                "pendientes": sum(f["pendientes"] for f in fuentes.values()),
                "fuentes": fuentes, "error_embeddings": self._embed_error}


def _embed_text(title: str, heading: str, text: str) -> str:
    # El título y el encabezado van delante: «Cálculo › Límites» dice de qué va un trozo que solo tiene fórmulas.
    head = SEPARADOR.join(p for p in (title, heading) if p)
    return f"{head}\n{text}" if head else text


def _normalized(v: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(x * x for x in v))
    return tuple(x / norm for x in v) if norm else tuple(v)
