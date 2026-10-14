"""Revisión de vídeos (paso 4): nada se publica sin que Marc lo vea y pulse «Aprobar».

Regla de diseño (docs/studio.md): el modelo NUNCA publica. La aprobación la da una persona desde la página de
revisión y queda atada a tres huellas (sha256):

  vídeo       salida/video.mp4
  miniatura   salida/miniatura.jpg
  textos      título + descripción completa + etiquetas + idioma + canal

Antes de publicar (paso 5) se vuelven a calcular: si cualquier cosa cambió después de aprobar (otro montaje, una
coma del título), la aprobación ya no vale. Y al aprobar, la página manda la huella del vídeo que Marc VIO: si
mientras tanto se rehízo, tampoco vale (no se aprueba a ciegas un vídeo distinto del que se ha visto).

Estados:  pending ──► approved ──► published (o failed, con el motivo)
             │──► changes (con lo que hay que cambiar) ──► un montaje nuevo crea otra revisión
             │──► discarded
             └──► replaced (llegó un montaje más nuevo del mismo proyecto antes de decidir)
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .project import Script, full_description, load_script, project_dir
from .store import Asset, StudioStore

log = logging.getLogger(__name__)

# Los canales de Marc (creados y verificados el 2026-10-12). El idioma del guion decide el canal: así un vídeo
# en español nunca puede acabar en el canal en inglés.
CHANNELS = {"es": "@ArthuroEstoicismo", "en": "@ArthuroStoicEN"}
# Lo escribe el piloto automático (paso 6) en cada proyecto suyo: las dudas del revisor. Si existe, el proyecto es
# del piloto y se publica en PÚBLICO (decisión de Marc, 2026-10-13); los hechos a mano siguen subiendo en privado.
CHECK_FILE = "comprobacion.json"
CONFIRM_WORD = "PUBLICAR"   # lo que hay que escribir para aprobar un vídeo con dudas
MAX_FEEDBACK = 2000


class ReviewError(Exception):
    """El mensaje se le enseña a Marc tal cual."""


@dataclass(frozen=True)
class Fingerprint:
    video: str
    thumb: str
    meta: str


@dataclass(frozen=True)
class Review:
    id: int
    project: str
    language: str
    channel: str
    status: str
    title: str
    video_sha256: str
    thumb_sha256: str
    meta_sha256: str
    feedback: str | None
    decided_by: str | None
    decided_at: str | None
    youtube_id: str | None
    error: str | None
    created_at: str
    kind: str = "largo"   # «largo» (horizontal) o «corto» (vertical: Shorts, Reels, TikTok)


KINDS = ("largo", "corto")


def outputs(folder: Path, kind: str) -> tuple[Path, Path, Path]:
    """(vídeo, miniatura, control) de cada tipo. En el corto, la «miniatura» es un fotograma solo para la página."""
    base = folder / "salida" if kind == "largo" else folder / "salida" / "corto"
    return base / "video.mp4", base / "miniatura.jpg", base / "control.json"


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def metadata(script: Script, assets: list[Asset], kind: str = "largo") -> dict:
    """Lo que irá a YouTube junto al vídeo. Una sola función: lo que se aprueba y lo que se sube es lo mismo."""
    channel = CHANNELS.get(script.idioma)
    if channel is None:
        raise ReviewError(f"No hay canal para el idioma «{script.idioma}»")
    if kind == "largo":
        return {"title": script.titulo, "description": full_description(script, assets),
                "tags": list(script.etiquetas), "language": script.idioma, "channel": channel, "kind": kind}
    if kind != "corto":
        raise ReviewError(f"Tipo de vídeo desconocido: {kind}")
    # El corto lleva a la historia completa: el vídeo largo del canal (que es el que paga).
    more = {"es": "Historia completa en el canal", "en": "Full story on the channel"}.get(script.idioma, "")
    text = script.corto.descripcion or script.descripcion
    full = full_description(script, assets).replace(script.descripcion, f"{text}\n\n{more} {channel}", 1)
    return {"title": script.corto.titulo or script.titulo, "description": full,
            "tags": [*script.etiquetas, "shorts"], "language": script.idioma, "channel": channel, "kind": kind}


def fingerprint(folder: Path, meta: dict) -> Fingerprint:
    video, thumb, _ = outputs(folder, meta.get("kind", "largo"))
    if not video.exists() or not thumb.exists():
        raise ReviewError("Falta el vídeo o la miniatura: ejecuta --studio-montar (o --studio-corto)")
    # «kind» no entra en la huella: ya lo distinguen los ficheros, y así las revisiones de antes de los cortos valen.
    text = json.dumps({k: v for k, v in meta.items() if k != "kind"}, ensure_ascii=False, sort_keys=True).encode()
    return Fingerprint(_sha_file(video), _sha_file(thumb), hashlib.sha256(text).hexdigest())


class ReviewBook:
    def __init__(self, store: StudioStore, *, now: Callable[[], datetime] = datetime.now,
                 on_new: Callable[[Review], None] | None = None) -> None:
        self.store = store
        self.now = now
        self.on_new = on_new  # aviso al móvil cuando haya un vídeo nuevo (se conecta al juntar con el core)

    def _stamp(self) -> str:
        return self.now().replace(microsecond=0).isoformat()

    # --- crear ----------------------------------------------------------------

    def submit(self, project: str, kind: str = "largo") -> Review:
        """Pone en revisión el último montaje de un proyecto (debe haber pasado el control de calidad)."""
        if kind not in KINDS:
            raise ReviewError(f"Tipo de vídeo desconocido: {kind}")
        folder = project_dir(project)
        control = outputs(folder, kind)[2]
        if not control.exists() or not json.loads(control.read_text(encoding="utf-8")).get("ok"):
            raise ReviewError("El vídeo no ha pasado el control de calidad: no se manda a revisar")
        script = load_script(folder / "guion.toml")
        meta = metadata(script, self.store.assets(project), kind)
        fp = fingerprint(folder, meta)
        stamp = self._stamp()
        with self.store._lock, self.store._db:
            same = self.store._db.execute(
                "SELECT id FROM reviews WHERE project = ? AND kind = ? AND status IN ('pending', 'approved') "
                "AND video_sha256 = ? AND thumb_sha256 = ? AND meta_sha256 = ?",
                (project, kind, fp.video, fp.thumb, fp.meta)).fetchone()
            if same:
                review_id, created = same[0], False  # ya estaba en revisión tal cual: no se duplica
            else:
                # Un montaje nuevo sustituye a lo que estuviera esperando (o aprobado) del mismo proyecto.
                self.store._db.execute(
                    "UPDATE reviews SET status = 'replaced', updated_at = ? WHERE project = ? AND kind = ? AND status "
                    "IN ('pending', 'approved')", (stamp, project, kind))
                review_id = self.store._db.execute(
                    "INSERT INTO reviews (project, kind, language, channel, status, title, video_sha256, thumb_sha256, "
                    "meta_sha256, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                    (project, kind, script.idioma, meta["channel"], meta["title"], fp.video, fp.thumb, fp.meta,
                     stamp, stamp),
                ).lastrowid
                created = True
        review = self.get(review_id)
        if created:
            log.info("Studio: vídeo #%d (%s) listo para revisar", review.id, project)
            if self.on_new:
                try:
                    self.on_new(review)
                except Exception:
                    log.exception("Studio: no se pudo avisar del vídeo #%d", review.id)  # el vídeo sigue en revisión
        return review

    # --- consultar --------------------------------------------------------------

    def get(self, review_id: int) -> Review | None:
        with self.store._lock:
            row = self.store._db.execute(f"SELECT {_COLUMNS} FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return Review(*row) if row else None

    def list(self, limit: int = 30) -> list[Review]:
        """Primero lo que espera decisión; luego lo más reciente."""
        with self.store._lock:
            rows = self.store._db.execute(
                f"SELECT {_COLUMNS} FROM reviews ORDER BY status = 'pending' DESC, id DESC LIMIT ?",
                (max(1, min(limit, 200)),)).fetchall()
        return [Review(*r) for r in rows]

    def current(self, review: Review) -> Fingerprint:
        folder = project_dir(review.project)
        script = load_script(folder / "guion.toml")
        return fingerprint(folder, metadata(script, self.store.assets(review.project), review.kind))

    # --- decidir (solo desde la página, por una persona) ---------------------------

    def _decide(self, review_id: int, status: str, device: str, feedback: str | None = None) -> Review:
        review = self.get(review_id)
        if review is None:
            raise ReviewError("Ese vídeo no existe")
        if review.status != "pending":
            raise ReviewError(f"Ese vídeo ya no está esperando revisión (está «{LABELS.get(review.status)}»)")
        stamp = self._stamp()
        with self.store._lock, self.store._db:
            changed = self.store._db.execute(
                "UPDATE reviews SET status = ?, feedback = ?, decided_by = ?, decided_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'", (status, feedback, device, stamp, stamp, review_id)).rowcount
        if not changed:
            raise ReviewError("Otra pestaña acaba de decidir sobre este vídeo")
        log.info("Studio: vídeo #%d → %s (aparato %s)", review_id, status, device)
        return self.get(review_id)

    def approve(self, review_id: int, seen_video_sha256: str, device: str, confirm: str = "") -> Review:
        """`confirm`: si el revisor automático dejó dudas, hay que escribir «PUBLICAR» (2026-10-13: el vídeo de
        Séneca salió en público con 5 errores tras un «Aprobar» rápido desde el móvil)."""
        review = self.get(review_id)
        if review is None:
            raise ReviewError("Ese vídeo no existe")
        if self.warnings(review.project) and confirm.strip().upper() != CONFIRM_WORD:
            raise ReviewError(f"Este vídeo tiene dudas del revisor: escribe {CONFIRM_WORD} para confirmar que las "
                              "has comprobado")
        if seen_video_sha256 != review.video_sha256:
            raise ReviewError("El vídeo que has visto no es el que está en revisión: recarga la página")
        if self.current(review) != Fingerprint(review.video_sha256, review.thumb_sha256, review.meta_sha256):
            raise ReviewError("El vídeo, la miniatura o los textos han cambiado desde que se mandó a revisar: "
                              "hay que volver a montarlo y revisarlo")
        return self._decide(review_id, "approved", device)

    def request_changes(self, review_id: int, feedback: str, device: str) -> Review:
        feedback = " ".join(str(feedback or "").split())
        if not feedback:
            raise ReviewError("Escribe qué hay que cambiar")
        if len(feedback) > MAX_FEEDBACK:
            raise ReviewError(f"Demasiado largo (máximo {MAX_FEEDBACK} caracteres)")
        return self._decide(review_id, "changes", device, feedback)

    def discard(self, review_id: int, device: str) -> Review:
        return self._decide(review_id, "discarded", device)

    # --- para publicar (paso 5) ----------------------------------------------------

    def ready_to_publish(self, review_id: int) -> tuple[Review, dict]:
        """La revisión aprobada y lo que hay que subir, SOLO si nada ha cambiado desde la aprobación."""
        review = self.get(review_id)
        if review is None or review.status != "approved":
            raise ReviewError("Solo se publica un vídeo aprobado")
        folder = project_dir(review.project)
        script = load_script(folder / "guion.toml")
        meta = metadata(script, self.store.assets(review.project), review.kind)
        if fingerprint(folder, meta) != Fingerprint(review.video_sha256, review.thumb_sha256, review.meta_sha256):
            raise ReviewError("Algo ha cambiado desde que lo aprobaste: no se publica. Vuelve a revisarlo")
        if meta["channel"] != review.channel:
            raise ReviewError("El canal no coincide con el aprobado: no se publica")
        video, thumb, _ = outputs(folder, review.kind)
        # Un Short no admite miniatura propia por la API: el fotograma es solo para la página de revisión.
        privacy = "public" if (folder / CHECK_FILE).exists() else "private"
        return review, {**meta, "video": str(video), "thumbnail": str(thumb) if review.kind == "largo" else None,
                        "privacy": privacy}

    def warnings(self, project: str) -> list[str] | None:
        """Las dudas del revisor automático. None = no es un proyecto del piloto (o el fichero no se entiende)."""
        try:
            data = json.loads((project_dir(project) / CHECK_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        problems = data.get("problemas") if isinstance(data, dict) else None
        return [str(p)[:300] for p in problems[:10]] if isinstance(problems, list) else None

    def mark_published(self, review_id: int, youtube_id: str, warning: str | None = None) -> None:
        self._finish(review_id, "published", youtube_id=youtube_id, error=warning)

    def retry(self, review_id: int, device: str) -> Review:
        """Una subida que falló (red, límite diario…) vuelve a «aprobado», solo si el vídeo sigue siendo el mismo."""
        review = self.get(review_id)
        if review is None or review.status != "failed":
            raise ReviewError("Solo se puede reintentar un vídeo cuya subida falló")
        if self.current(review) != Fingerprint(review.video_sha256, review.thumb_sha256, review.meta_sha256):
            raise ReviewError("El vídeo ha cambiado desde que lo aprobaste: hay que volver a revisarlo")
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "UPDATE reviews SET status = 'approved', error = NULL, updated_at = ? WHERE id = ? AND status = 'failed'",
                (self._stamp(), review_id))
        log.info("Studio: reintento de publicar el vídeo #%d (aparato %s)", review_id, device)
        return self.get(review_id)

    def approved(self) -> list[Review]:
        """Lo aprobado que aún no se ha subido (lo recoge el publicador)."""
        with self.store._lock:
            rows = self.store._db.execute(
                f"SELECT {_COLUMNS} FROM reviews WHERE status = 'approved' ORDER BY id").fetchall()
        return [Review(*r) for r in rows]

    def mark_failed(self, review_id: int, error: str) -> None:
        self._finish(review_id, "failed", error=error[:1000])

    def _finish(self, review_id: int, status: str, *, youtube_id: str | None = None, error: str | None = None) -> None:
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "UPDATE reviews SET status = ?, youtube_id = COALESCE(?, youtube_id), error = ?, updated_at = ? "
                "WHERE id = ? AND status = 'approved'", (status, youtube_id, error, self._stamp(), review_id))


_COLUMNS = ("id, project, language, channel, status, title, video_sha256, thumb_sha256, meta_sha256, feedback, "
            "decided_by, decided_at, youtube_id, error, created_at, kind")
LABELS = {"pending": "esperando", "approved": "aprobado", "changes": "con cambios pedidos", "discarded": "descartado",
          "replaced": "sustituido por un montaje más nuevo", "published": "publicado", "failed": "falló al publicar"}
