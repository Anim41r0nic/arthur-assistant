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
from datetime import date, datetime, timedelta
from pathlib import Path

from . import channels
from .project import Script, full_description, load_script, project_dir
from .store import Asset, StudioStore

log = logging.getLogger(__name__)

# Los canales de estoicismo por idioma (los de antes de los perfiles). Ya NO deciden el canal: lo decide el perfil
# del guion (channels.py), porque Frutinovelas también es en español. Se queda por compatibilidad (tests, rótulos).
CHANNELS = {lang: channels.PROFILES[pid].handle for lang, pid in channels.DEFAULT_BY_LANGUAGE.items()}
# Lo escribe el piloto automático (paso 6) en cada proyecto suyo: las dudas del revisor. Si existe, el proyecto es
# del piloto y se publica en PÚBLICO (decisión de Marc, 2026-10-13); los hechos a mano siguen subiendo en privado.
CHECK_FILE = "comprobacion.json"
# Lo escribe importer.py en la carpeta de un Short hecho fuera de Studio (Frutinovelas): sus textos y de dónde vino.
# Si existe, los textos salen de aquí y no de guion.toml (no hay guion: el vídeo llegó terminado).
IMPORT_FILE = "importado.json"
# Lo escribe frutas_diario.py en cada episodio diario de Frutinovelas (0.66.0): sus textos. Lo monta Studio (no es
# importado), así que con su comprobacion.json se aprueba solo a las 18:00 si no tiene dudas y sube en público.
FRUTAS_FILE = "frutinovela.json"
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
    profile: str = ""     # el perfil del canal (channels.py); vacío solo en filas raras de antes del esquema 6
    origin: str = ""      # importado (esquema 7): de dónde se copió el vídeo. Vacío = lo montó Studio

    @property
    def imported(self) -> bool:
        """Un Short hecho fuera de Studio: nunca se aprueba solo, solo con «Aprobar» de Marc."""
        return bool(self.origin)

    @property
    def target(self) -> channels.Profile:
        """El perfil al que va esta revisión (las antiguas sin perfil, por el @ que se aprobó)."""
        return channels.for_review(self.profile, self.channel, self.language)


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


MAX_DESCRIPTION_BYTES = 5000  # límite de YouTube (en bytes: «é» cuenta 2)


def metadata(script: Script, assets: list[Asset], kind: str = "largo") -> dict:
    """Lo que irá a YouTube junto al vídeo. Una sola función: lo que se aprueba y lo que se sube es lo mismo."""
    try:
        profile = channels.for_script(script.perfil, script.idioma)
    except channels.UnknownProfile as e:
        raise ReviewError(str(e)) from None
    channel = profile.handle
    if kind == "largo":
        return {"title": script.titulo, "description": full_description(script, assets),
                "tags": list(script.etiquetas), "language": script.idioma, "channel": channel, "kind": kind,
                "profile": profile.id}
    if kind != "corto":
        raise ReviewError(f"Tipo de vídeo desconocido: {kind}")
    # El corto lleva a la historia completa: el vídeo largo del canal (que es el que paga).
    more = {"es": "Historia completa en el canal", "en": "Full story on the channel"}.get(script.idioma, "")
    text = script.corto.descripcion or script.descripcion
    full = full_description(script, assets).replace(script.descripcion, f"{text}\n\n{more} {channel}", 1)
    return {"title": script.corto.titulo or script.titulo, "description": full,
            "tags": [*script.etiquetas, "shorts"], "language": script.idioma, "channel": channel, "kind": kind,
            "profile": profile.id}


def imported_metadata(folder: Path, kind: str = "corto") -> dict:
    """Lo que irá a YouTube con un Short importado: sus textos guardados al importarlo (importado.json).
    El mismo diccionario que metadata(): así se aprueba y se sube por el camino de siempre."""
    if kind != "corto":
        raise ReviewError("Un vídeo importado solo puede ser un corto")
    try:
        data = json.loads((folder / IMPORT_FILE).read_text(encoding="utf-8"))
        profile = channels.get(str(data["perfil"]))
        title, description = str(data["titulo"]), str(data["descripcion"])
        tags = [str(t) for t in data["etiquetas"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ReviewError(f"No se pueden leer los textos del vídeo importado: {e}") from None
    return {"title": title, "description": description, "tags": tags, "language": profile.language,
            "channel": profile.handle, "kind": kind, "profile": profile.id}


def frutas_metadata(folder: Path, kind: str = "corto") -> dict:
    """Los textos de un episodio diario de Frutinovelas (frutinovela.json). `synthetic`: YouTube pide declarar el
    contenido alterado o sintético (imágenes y voces hechas con IA); va a la subida y a la huella de los textos."""
    if kind != "corto":
        raise ReviewError("Un episodio de Frutinovelas solo puede ser un corto")
    try:
        data = json.loads((folder / FRUTAS_FILE).read_text(encoding="utf-8"))
        profile = channels.get(str(data["perfil"]))
        title, description = str(data["titulo"]), str(data["descripcion"])
        tags = [str(t) for t in data["etiquetas"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ReviewError(f"No se pueden leer los textos del episodio: {e}") from None
    if profile.product != "corto":
        # Un fichero así en un proyecto de estoicismo no lo convierte en Frutinovelas ni le da la aprobación solo.
        raise ReviewError("Solo un canal de cortos (Frutinovelas) puede tener episodios diarios")
    return {"title": title, "description": description, "tags": tags, "language": profile.language,
            "channel": profile.handle, "kind": kind, "profile": profile.id, "synthetic": True}


def is_frutas(folder: Path) -> bool:
    return (folder / FRUTAS_FILE).exists()


def is_imported(folder: Path) -> bool:
    return (folder / IMPORT_FILE).exists()


def project_meta(store: StudioStore, project: str, kind: str) -> dict:
    """Los textos de un proyecto, venga de un guion (lo monta Studio) o de un Short importado."""
    folder = project_dir(project)
    if is_imported(folder):
        return imported_metadata(folder, kind)
    if is_frutas(folder):
        return frutas_metadata(folder, kind)
    return metadata(load_script(folder / "guion.toml"), store.assets(project), kind)


def _origin(folder: Path) -> str:
    """De dónde se copió un vídeo importado («» si lo montó Studio)."""
    if not is_imported(folder):
        return ""
    try:
        origin = json.loads((folder / IMPORT_FILE).read_text(encoding="utf-8")).get("origen")
    except (OSError, ValueError, AttributeError):
        origin = None
    # Nunca vacío: una revisión con origen vacío se trataría como montada por Studio (y podría aprobarse sola).
    return str(origin or "importado")[:1000]


def fingerprint(folder: Path, meta: dict) -> Fingerprint:
    video, thumb, _ = outputs(folder, meta.get("kind", "largo"))
    if not video.exists() or not thumb.exists():
        raise ReviewError("Falta el vídeo o la miniatura: ejecuta --studio-montar (o --studio-corto)")
    # «kind» no entra en la huella: ya lo distinguen los ficheros, y así las revisiones de antes de los cortos valen.
    # «profile» tampoco: cada perfil tiene su @ propio y el @ («channel») sí está en la huella; así las aprobaciones
    # de antes de los perfiles siguen valiendo.
    text = json.dumps({k: v for k, v in meta.items() if k not in ("kind", "profile")}, ensure_ascii=False,
                      sort_keys=True).encode()
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
        meta = project_meta(self.store, project, kind)
        origin = _origin(folder)
        size = len(meta["description"].encode("utf-8"))
        if size > MAX_DESCRIPTION_BYTES:  # YouTube la rechaza con un 400 al subir: mejor saberlo ahora
            raise ReviewError(f"La descripción ocupa {size} bytes y YouTube admite {MAX_DESCRIPTION_BYTES}: "
                              "acorta la descripción o las fuentes del guion")
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
                    "INSERT INTO reviews (project, kind, language, channel, profile, status, title, video_sha256, "
                    "thumb_sha256, meta_sha256, created_at, updated_at, origin) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                    (project, kind, meta["language"], meta["channel"], meta["profile"], meta["title"], fp.video,
                     fp.thumb, fp.meta, stamp, stamp, origin),
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

    def list(self, limit: int = 30, profile: str | None = None) -> list[Review]:
        """Primero lo que espera decisión; luego lo más reciente. Con `profile` (nombre completo de channels.py),
        solo las de ese canal, y el límite se cuenta dentro de ese canal (si no, 30 vídeos de estoicismo dejarían
        fuera a los de Frutinovelas)."""
        limit = max(1, min(limit, 200))
        if profile is None:
            with self.store._lock:
                rows = self.store._db.execute(
                    f"SELECT {_COLUMNS} FROM reviews ORDER BY status = 'pending' DESC, id DESC LIMIT ?",
                    (limit,)).fetchall()
            return [Review(*r) for r in rows]
        # Las filas sin perfil (de antes del esquema 6 y con un @ que la migración no conocía) no se pueden filtrar
        # en SQL: se traen también y se decide en Python con la misma regla que usa el publicador (Review.target).
        with self.store._lock:
            rows = self.store._db.execute(
                f"SELECT {_COLUMNS} FROM reviews WHERE profile = ? OR profile = '' "
                "ORDER BY status = 'pending' DESC, id DESC", (profile,)).fetchall()
        found = []
        for row in rows:
            review = Review(*row)
            if _profile_of(review) == profile:
                found.append(review)
                if len(found) == limit:
                    break
        return found

    def pending_counts(self) -> dict[str, int]:
        """Cuántos vídeos esperan decisión en cada canal, con los tres perfiles siempre (0 si no hay ninguno)."""
        counts = dict.fromkeys(channels.ORDER, 0)
        with self.store._lock:
            rows = self.store._db.execute(f"SELECT {_COLUMNS} FROM reviews WHERE status = 'pending'").fetchall()
        for row in rows:
            name = _profile_of(Review(*row))
            if name in counts:
                counts[name] += 1
        return counts

    def published_shorts(self, since: date) -> list[dict]:
        """Cortos publicados desde un día: para el panel, sin depender del límite de la página de revisión."""
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT id, title, updated_at FROM reviews "
                "WHERE kind = 'corto' AND status = 'published' AND substr(updated_at, 1, 10) >= ? "
                "ORDER BY updated_at DESC", (since.isoformat(),)).fetchall()
        return [{"id": row[0], "title": row[1], "published_at": row[2]} for row in rows]

    def current(self, review: Review) -> Fingerprint:
        return fingerprint(project_dir(review.project), project_meta(self.store, review.project, review.kind))

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
        meta = project_meta(self.store, review.project, review.kind)
        if fingerprint(folder, meta) != Fingerprint(review.video_sha256, review.thumb_sha256, review.meta_sha256):
            raise ReviewError("Algo ha cambiado desde que lo aprobaste: no se publica. Vuelve a revisarlo")
        if meta["channel"] != review.channel or meta["profile"] != review.target.id:
            raise ReviewError("El canal no coincide con el aprobado: no se publica")
        video, thumb, _ = outputs(folder, review.kind)
        # Un Short no admite miniatura propia por la API: el fotograma es solo para la página de revisión.
        # Un importado, en privado aunque alguien deje un comprobacion.json en su carpeta; salvo en un canal de un
        # vídeo al día (Frutinovelas, decisión de Marc del 19-oct-2026): ahí lo aprobó él a mano y sale en público.
        # Esos Shorts también están hechos con IA, así que se declaran sintéticos como los episodios diarios. Ni la
        # privacidad ni esa marca entran en la huella: no invalidan lo que ya estaba aprobado.
        daily = review.target.per_day > 0
        if review.imported:
            privacy = "public" if daily else "private"
            if daily and review.target.product == "corto":
                meta = {**meta, "synthetic": True}
        else:
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

    def finished_on(self, profile: str, day: date) -> int:
        """Cuántas subidas de ese perfil acabaron (bien o mal) ese día. Para el límite de un vídeo al día: también
        cuenta un fallo, para que un día sin cuota no tire uno detrás de otro todos los de la cola."""
        start = datetime.combine(day, datetime.min.time())
        with self.store._lock:
            rows = self.store._db.execute(
                f"SELECT {_COLUMNS} FROM reviews WHERE status IN ('published', 'failed') "
                "AND updated_at >= ? AND updated_at < ?",
                (start.isoformat(), (start + timedelta(days=1)).isoformat())).fetchall()
        return sum(1 for r in rows if _profile_of(Review(*r)) == profile)

    def mark_failed(self, review_id: int, error: str) -> None:
        self._finish(review_id, "failed", error=error[:1000])

    def _finish(self, review_id: int, status: str, *, youtube_id: str | None = None, error: str | None = None) -> None:
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "UPDATE reviews SET status = ?, youtube_id = COALESCE(?, youtube_id), error = ?, updated_at = ? "
                "WHERE id = ? AND status = 'approved'", (status, youtube_id, error, self._stamp(), review_id))


def _profile_of(review: Review) -> str:
    """El perfil efectivo de una revisión (también de las antiguas sin perfil). Una fila imposible de asignar
    (perfil o idioma que ya no existen) devuelve "": no sale en ningún canal en vez de romper la lista entera."""
    try:
        return review.target.id
    except channels.UnknownProfile:
        return ""


_COLUMNS = ("id, project, language, channel, status, title, video_sha256, thumb_sha256, meta_sha256, feedback, "
            "decided_by, decided_at, youtube_id, error, created_at, kind, profile, origin")
LABELS = {"pending": "esperando", "approved": "aprobado", "changes": "con cambios pedidos", "discarded": "descartado",
          "replaced": "sustituido por un montaje más nuevo", "published": "publicado", "failed": "falló al publicar"}
