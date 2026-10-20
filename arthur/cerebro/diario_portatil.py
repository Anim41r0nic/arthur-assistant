"""Nota diaria «Hecho con IA» (lado del portátil): junta lo del día, lo manda a resumir y guarda la nota.

Lo lanza un temporizador de systemd a las 23:30 (`scripts/systemd/arthur-cerebro-diario.timer`, Persistent=true:
si el portátil estaba apagado, al encenderlo). El material vive aquí (el Registro de EQUIPO-IA.md, la memoria de
Claude y los repos de ~/Proyectos), así que se junta aquí; se manda al panel por Tailscale, porque el cerebro no
tiene puerto fuera de la red de Docker, y el panel se lo pasa con su llave. La nota se escribe en la bóveda
~/Cerebro y Syncthing la lleva al servidor.

Nunca se mandan conversaciones enteras, y lo que parezca un secreto se tapa antes de salir del portátil.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..security.audit import SECRET_PATTERNS

MADRID = ZoneInfo("Europe/Madrid")
CUTOFF = time(23, 30)   # antes de esta hora el día de hoy aún no ha terminado
MAX_DAYS = 7            # tras una semana apagado no se reconstruye un mes entero: solo la última semana
MAX_MEMORY_NOTE = 2_000  # caracteres por nota de memoria
GIT_TIMEOUT = 20


class DiarioError(Exception):
    """No se pudo mandar el material o el servidor no lo aceptó: se reintenta la próxima vez."""


def hide_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[OCULTO]", text)
    return text


def registro_del_dia(text: str, day: date) -> str:
    """Las entradas del Registro que empiezan por «- 2026-10-17 ·», con sus líneas de continuación (sangradas)."""
    prefix = f"- {day.isoformat()} ·"
    keep, inside = [], False
    for line in text.splitlines():
        if line.startswith("- "):
            inside = line.startswith(prefix)
        elif not line.strip() or not line.startswith((" ", "\t")):
            inside = False
        if inside:
            keep.append(line.rstrip())
    return "\n".join(keep)


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _day_bounds(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time(0), MADRID)
    end = datetime.combine(day + timedelta(days=1), time(0), MADRID)
    return start.isoformat(), end.isoformat()


def collect(day: date, *, equipo: Path, memory_dir: Path, repos: list[Path],
            run: Callable = subprocess.run) -> dict | None:
    """El material de un día, ya sin secretos. None si ese día no pasó nada."""
    try:
        registro = registro_del_dia(equipo.read_text(encoding="utf-8"), day)
    except OSError:
        registro = ""
    memoria = []
    for path in sorted(memory_dir.glob("*.md")) if memory_dir.is_dir() else []:
        # MEMORY.md es solo el índice: repetiría lo que ya dicen las notas
        if path.name == "MEMORY.md":
            continue
        if datetime.fromtimestamp(path.stat().st_mtime, MADRID).date() != day:
            continue
        body = _strip_frontmatter(path.read_text(encoding="utf-8")).strip()
        if body:
            memoria.append(body[:MAX_MEMORY_NOTE])
    since, until = _day_bounds(day)
    commits, seen = [], set()
    for repo in repos:
        try:
            out = run(["git", "-C", str(repo), "log", f"--since={since}", f"--until={until}", "--format=%h %s"],
                      capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            continue  # un repo roto o sin git no puede dejar el día sin nota
        for line in out.splitlines():
            line = line.strip()
            # Las copias de trabajo de un mismo repo comparten historia: cada commit, una vez
            if line and line.split()[0] not in seen:
                seen.add(line.split()[0])
                commits.append(f"{repo.name}: {line}")
    if not (registro or memoria or commits):
        return None
    return {"day": day.isoformat(), "registro": hide_secrets(registro),
            "memoria": [hide_secrets(m) for m in memoria], "commits": [hide_secrets(c) for c in commits]}


def _unique_path(out_dir: Path, day: date) -> Path:
    path = out_dir / f"{day.isoformat()}.md"
    n = 2
    while path.exists():
        path = out_dir / f"{day.isoformat()} ({n}).md"
        n += 1
    return path


def _load_last(state: Path) -> date | None:
    try:
        return date.fromisoformat(json.loads(state.read_text())["ultimo"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_last(state: Path, day: date) -> None:
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = state.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ultimo": day.isoformat()}))
    os.replace(tmp, state)


def write_days(now: datetime, out_dir: Path, send: Callable[[dict], dict], state: Path, *,
               collect_fn: Callable[[date], dict | None]) -> list[Path]:
    """Escribe la nota de cada día que falte (máx. MAX_DAYS) y devuelve las rutas escritas.

    Hoy solo cuenta desde las 23:30. Si el envío falla, para ahí sin marcar ese día: se reintenta la próxima vez."""
    now = now.astimezone(MADRID)
    until = now.date() if now.time() >= CUTOFF else now.date() - timedelta(days=1)
    last = _load_last(state)
    first = until if last is None else max(last + timedelta(days=1), until - timedelta(days=MAX_DAYS - 1))
    written = []
    day = first
    while day <= until:
        material = collect_fn(day)
        if material is not None:
            try:
                markdown = send(material)["markdown"]
            except (DiarioError, KeyError, TypeError):
                break
            out_dir.mkdir(parents=True, exist_ok=True)
            path = _unique_path(out_dir, day)
            path.write_text(markdown, encoding="utf-8")
            written.append(path)
        _save_last(state, day)
        day += timedelta(days=1)
    return written


def panel_sender(base_url: str, key: str, *, opener=None, timeout: float = 90) -> Callable[[dict], dict]:
    """Manda el material al panel (POST /api/cerebro/diario), que lo reenvía al cerebro."""
    opener = opener or urllib.request.build_opener()
    url = base_url.rstrip("/") + "/api/cerebro/diario"

    def send(material: dict) -> dict:
        request = urllib.request.Request(url, data=json.dumps(material).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}", "X-Arthur": "1"})
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            e.close()
            raise DiarioError(f"El panel ha respondido con error {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise DiarioError(f"No puedo hablar con el panel: {type(e).__name__}") from None
    return send


# ── Notas de la revisión (0.67.0) ──────────────────────────────────────────────────────────────────────────────
# El panel hace la revisión de las 21:30 (el domingo, la semanal). Como la bóveda del servidor es de solo lectura, la
# nota la escribe el portátil, a la vez que «Hecho con IA»: la pide al panel y la guarda en ~/Cerebro/Diario/Revision.

NOTE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}|Semana-\d{4}-W\d{2})\.md$")


def revision_fetcher(base_url: str, key: str, *, opener=None, timeout: float = 30) -> Callable[[date, str], dict | None]:
    """GET /api/revision/nota del panel. None si ese día no hubo revisión (404); DiarioError si no se puede hablar."""
    opener = opener or urllib.request.build_opener()
    base = base_url.rstrip("/") + "/api/revision/nota"

    def fetch(day: date, kind: str) -> dict | None:
        url = f"{base}?fecha={day.isoformat()}&tipo={kind}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 404:
                return None
            raise DiarioError(f"El panel ha respondido con error {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise DiarioError(f"No puedo hablar con el panel: {type(e).__name__}") from None
    return fetch


def write_revision_notes(now: datetime, out_dir: Path, fetch: Callable[[date, str], dict | None],
                         state: Path) -> list[Path]:
    """Guarda la nota de revisión de cada día que falte (y la semanal de los domingos), sin pisar ninguna nota
    que ya exista. Si el panel no contesta, para ahí sin marcar ese día: se reintenta la próxima vez."""
    now = now.astimezone(MADRID)
    until = now.date() if now.time() >= CUTOFF else now.date() - timedelta(days=1)
    last = _load_last(state)
    day = until if last is None else max(last + timedelta(days=1), until - timedelta(days=MAX_DAYS - 1))
    written = []
    while day <= until:
        kinds = ("noche", "semana") if day.weekday() == 6 else ("noche",)
        try:
            notes = [fetch(day, kind) for kind in kinds]
        except DiarioError:
            break
        for note in notes:
            if not isinstance(note, dict) or not isinstance(note.get("markdown"), str):
                continue
            name = note.get("nombre")
            if not isinstance(name, str) or not NOTE_NAME.match(name):
                continue   # solo nombres de la forma esperada: nunca una ruta que salga de la carpeta
            path = out_dir / name
            if path.exists():
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(note["markdown"], encoding="utf-8")
            written.append(path)
        _save_last(state, day)
        day += timedelta(days=1)
    return written


def default_repos(projects: Path) -> list[Path]:
    # Solo los repos principales (.git es carpeta): las copias de trabajo tienen un fichero .git y repetirían todo
    return sorted(p for p in projects.iterdir() if (p / ".git").is_dir()) if projects.is_dir() else []


def main(env: Mapping[str, str] = os.environ, now: datetime | None = None, out=print) -> int:
    home = Path(env.get("HOME", str(Path.home())))
    panel = env.get("ARTHUR_PANEL_URL", "").strip()
    key_file = Path(env.get("ARTHUR_CEREBRO_DIARIO_KEY_FILE", home / ".config/arthur/cerebro-portatil.key"))
    if not panel:
        out("Falta ARTHUR_PANEL_URL (la dirección del panel, p. ej. http://100.64.0.10:8775)", file=sys.stderr)
        return 2
    try:
        if key_file.stat().st_mode & 0o077:
            out(f"{key_file} lo pueden leer otros usuarios: chmod 600", file=sys.stderr)
            return 2
        key = key_file.read_text().strip()
    except OSError:
        out(f"No encuentro la llave del portátil en {key_file}", file=sys.stderr)
        return 2
    # Dónde está el material (cada una se puede cambiar con su variable). La memoria de Claude Code vive en una
    # carpeta con el nombre de la carpeta personal con «-» en vez de «/» (/home/ana → -home-ana).
    projects = Path(env.get("ARTHUR_CEREBRO_PROYECTOS", home / "Proyectos"))
    equipo = Path(env.get("ARTHUR_CEREBRO_EQUIPO", projects / "EQUIPO-IA.md"))
    memory_dir = Path(env.get("ARTHUR_CEREBRO_MEMORIA",
                              home / ".claude/projects" / str(home).replace("/", "-") / "memory"))
    vault = Path(env.get("ARTHUR_CEREBRO_BOVEDA", home / "Cerebro"))
    state = home / ".local/state/arthur/cerebro-diario.json"

    def collect_fn(day: date) -> dict | None:
        return collect(day, equipo=equipo, memory_dir=memory_dir, repos=default_repos(projects))

    written = write_days(now or datetime.now(MADRID), vault / "Diario" / "IA", panel_sender(panel, key), state,
                         collect_fn=collect_fn)
    # Solo nombres de fichero en el registro de systemd, nunca el contenido
    out(f"Notas «Hecho con IA» escritas: {len(written)}" + (f" ({', '.join(p.name for p in written)})" if written else ""))
    revisions = write_revision_notes(now or datetime.now(MADRID), vault / "Diario" / "Revision",
                                     revision_fetcher(panel, key), home / ".local/state/arthur/revision-notas.json")
    out(f"Notas de revisión escritas: {len(revisions)}"
        + (f" ({', '.join(p.name for p in revisions)})" if revisions else ""))
    return 0
