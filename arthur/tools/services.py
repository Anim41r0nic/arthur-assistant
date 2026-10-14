"""Studio y Apuntes desde el chat de ARTHUR: «prepara un vídeo para hoy», «crea la carpeta Tema 4 en Cálculo»,
«guarda esta foto en Álgebra» (decisión de Marc del 2026-10-13: los tres ARTHUR comunicados).

ARTHUR no toca sus datos: les habla por su API, como el móvil, con una llave de aparato propia («core») que llega
como secreto de Docker. Así cada servicio sigue aplicando sus reglas (llaves, límites, comprobar las fotos por su
contenido, no publicar un vídeo con dudas sin escribir PUBLICAR…) y ARTHUR no puede saltárselas.

Riesgo de cada herramienta (el router pide confirmación según security/policy.py):
  - Mirar (qué hay en una carpeta, qué vídeos esperan): READ, sin preguntar.
  - Crear carpetas, guardar una foto, lanzar el piloto: WRITE, se confirma en el chat.
  - Publicar un vídeo: DANGEROUS, se confirma SIEMPRE. Y solo si el revisor no encontró dudas: con dudas, ARTHUR
    explica cuáles son y manda a la página de Studio, donde Marc tiene que escribir PUBLICAR (decisión de Marc).

Solo en el ARTHUR del servidor (perfil «core»), y solo si están ARTHUR_STUDIO_URL / ARTHUR_APUNTES_URL.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .base import Param, Risk, Tool, ToolError

TIMEOUT = 30
MAX_LIST = 25  # lo que se enseña de una carpeta: más no cabe en una respuesta hablada
JPEG, PNG = b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"


class ServiceLink:
    """Cliente mínimo de la API de un servicio (Studio o Apuntes) con la llave del aparato «core»."""

    def __init__(self, name: str, url: str, key: str, public_url: str = "",
                 opener: urllib.request.OpenerDirector | None = None) -> None:
        self.name = name
        self.url = url.rstrip("/")
        self.public_url = (public_url or url).rstrip("/")  # la dirección que abre Marc (por Tailscale)
        self._key = key
        self._opener = opener or urllib.request.build_opener()

    @classmethod
    def from_env(cls, env, name: str, prefix: str, secret: str) -> ServiceLink | None:
        url = env.get(f"{prefix}_URL", "").strip()
        if not url:
            return None
        path = env.get(f"{prefix}_LLAVE_FILE", f"/run/secrets/{secret}")
        try:
            key = Path(path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ValueError(f"No puedo leer la llave de {name} ({path}): {e}") from e
        return cls(name, url, key, env.get(f"{prefix}_PUBLIC_URL", ""))

    def call(self, path: str, body: dict | None = None, *, raw: bytes | None = None,
             content_type: str = "application/json") -> dict:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET")
        req.add_header("Authorization", f"Bearer {self._key}")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                try:
                    reason = json.loads(e.read(4096)).get("error", "")
                except (ValueError, AttributeError):
                    reason = ""
            raise ToolError(f"{self.name}: {reason or f'error {e.code}'}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise ToolError(f"{self.name} no contesta ({getattr(e, 'reason', e)}): ¿está en marcha?") from None
        except ValueError:
            raise ToolError(f"{self.name} ha devuelto algo que no se entiende") from None


def plain(text: str) -> str:
    """Para comparar nombres como los dice Marc: sin tildes, sin mayúsculas y con «/» como separador."""
    text = re.sub(r"\s*[/>›»]\s*", " / ", text)  # antes de quitar tildes: «›» no es ASCII y se perdería
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return " ".join(text.split())


def find_folder(folders: list[dict], wanted: str) -> dict:
    """La carpeta que Marc ha nombrado («cálculo / complejos», «complejos», «Álgebra»…). Si hay varias o ninguna,
    se le dice cuáles hay en vez de adivinar: guardar una foto en la asignatura equivocada es peor que preguntar."""
    target = plain(wanted)
    if not target or target in ("apuntes", "principal", "raiz", "inicio"):
        return folders[0]  # la principal
    paths = [(f, plain(f["path"])) for f in folders[1:]]
    for test in (lambda p: p == target, lambda p: p.endswith(" / " + target),
                 lambda p: target in p.rsplit(" / ", 1)[-1], lambda p: target in p):
        hits = [f for f, p in paths if test(p)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ToolError(f"Hay varias carpetas que encajan con «{wanted}»: "
                            + "; ".join(f["path"] for f in hits[:8]) + ". Dime cuál.")
    tops = [f["path"] for f in folders[1:] if " / " not in f["path"]]
    raise ToolError(f"No encuentro la carpeta «{wanted}». Asignaturas: {', '.join(tops) or 'ninguna todavía'}")


# --- Apuntes --------------------------------------------------------------------------------------------------------

def build_apuntes_tools(link: ServiceLink, latest_image: Callable[[], str | None]) -> list[Tool]:
    """`latest_image`: la última foto que Marc ha mandado en el chat (base64), o None."""

    def folders() -> list[dict]:
        return link.call("/api/folders")["folders"]

    def ver(args: dict) -> str:
        folder = find_folder(folders(), args["carpeta"])
        data = link.call(f"/api/notes?carpeta={folder['id']}")
        where = folder["path"] if folder["id"] else "Apuntes (principal)"
        lines = [f"{where}:"]
        lines += [f"📁 {f['name']} ({f['notes']} apuntes, {f['folders']} carpetas)" for f in data["folders"][:MAX_LIST]]
        lines += [f"📄 {n['title']} — {n['status_label']}" for n in data["notes"][:MAX_LIST]]
        hidden = len(data["folders"]) + len(data["notes"]) - 2 * MAX_LIST
        if hidden > 0:
            lines.append(f"… y {hidden} más (en la página: {link.public_url}/#c={folder['id']})")
        return "\n".join(lines) if len(lines) > 1 else f"{where} está vacía."

    def crear_carpeta(args: dict) -> str:
        parent = find_folder(folders(), args["dentro_de"])
        made = link.call("/api/folders", {"parent": parent["id"], "name": args["nombre"]})
        where = parent["path"] if parent["id"] else "la principal"
        return f"He creado la carpeta «{made['name']}» dentro de {where}."

    def photo() -> bytes:
        image = latest_image()
        if not image:
            raise ToolError("No me has mandado ninguna foto en este chat: adjúntala (📎) y dime dónde guardarla")
        try:
            data = base64.b64decode(image, validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("La foto ha llegado dañada: mándala otra vez") from None
        if not data.startswith((JPEG, PNG)):
            raise ToolError("Solo puedo guardar fotos JPEG o PNG: mándala otra vez desde la cámara")
        return data

    def check_photo(args: dict) -> None:
        photo()  # antes de preguntar «¿la guardo?»: si no hay foto, no tiene sentido confirmar nada
        find_folder(folders(), args["carpeta"])  # ni si la carpeta no existe o no está clara

    def guardar_foto(args: dict) -> str:
        data = photo()
        folder = find_folder(folders(), args["carpeta"])
        title = args["titulo"].strip() or "Foto del chat"
        note = link.call("/api/notes", {"title": title, "engine": "auto", "lang": "es", "folder": folder["id"]})
        link.call(f"/api/notes/{note['id']}/upload", raw=data,
                  content_type="image/png" if data.startswith(PNG) else "image/jpeg")
        where = folder["path"] if folder["id"] else "la carpeta principal"
        text = f"Guardada como «{note['title']}» en {where}"
        if args["pasar_a_texto"]:
            link.call(f"/api/notes/{note['id']}/start", {})
            text += ". La estoy pasando a texto"
        return f"{text}. Ábrela en {link.public_url}/#n={note['id']}"

    def where(args: dict) -> str:
        return args.get("carpeta") or args.get("dentro_de") or "la principal"

    return [
        Tool(
            name="apuntes_ver",
            description="Qué hay en una carpeta de ARTHUR Apuntes (asignaturas, temas, apuntes). "
                        "Sin carpeta: la lista de asignaturas.",
            risk=Risk.READ,
            params=(Param("carpeta", str, "p. ej. «Cálculo» o «Cálculo / Complejos»", required=False, default="",
                          max_length=200),),
            run=ver,
        ),
        Tool(
            name="apuntes_crear_carpeta",
            description="Crea una carpeta en ARTHUR Apuntes (una asignatura o un tema).",
            risk=Risk.WRITE,
            params=(Param("nombre", str, "nombre de la carpeta nueva", max_length=80),
                    Param("dentro_de", str, "carpeta donde crearla (vacío = las asignaturas)", required=False,
                          default="", max_length=200)),
            summarize=lambda a: f"Crear la carpeta «{a['nombre']}» en Apuntes, dentro de {where(a)}",
            run=crear_carpeta,
        ),
        Tool(
            name="apuntes_guardar_foto",
            description="Guarda en ARTHUR Apuntes la foto que Marc acaba de mandar en el chat, en la carpeta que "
                        "diga, y la pasa a texto.",
            risk=Risk.WRITE,
            params=(Param("carpeta", str, "p. ej. «Cálculo / Complejos»", max_length=200),
                    Param("titulo", str, "", required=False, default="", max_length=120),
                    Param("pasar_a_texto", bool, "", required=False, default=True)),
            check=check_photo,
            summarize=lambda a: f"Guardar la foto en Apuntes, en {where(a)}"
                                + (f", como «{a['titulo']}»" if a.get("titulo") else "")
                                + (" y pasarla a texto" if a.get("pasar_a_texto", True) else ""),
            run=guardar_foto,
        ),
    ]


# --- Studio ---------------------------------------------------------------------------------------------------------

STEP_NAMES = {"idea": "idea", "guion": "guion", "guion_en": "guion en inglés", "producir_es": "vídeo en español",
              "producir_en": "vídeo en inglés"}


def build_studio_tools(link: ServiceLink) -> list[Tool]:

    def pending() -> list[dict]:
        return [r for r in link.call("/api/reviews")["reviews"] if r["status"] == "pending"]

    def estado(args: dict) -> str:
        lines = []
        job = link.call("/api/pilot").get("job")
        if job and job["status"] in ("queued", "running"):
            topic = f", tema: {job['tema']}" if job.get("tema") else ""
            done = ", ".join(STEP_NAMES.get(s, s) for s in job["steps_done"]) or "nada aún"
            lines.append(f"Haciéndose un vídeo (trabajo #{job['id']}{topic}). Hecho: {done}.")
        elif job and job["status"] == "failed":
            lines.append(f"El último vídeo del piloto falló: {job['error'] or 'sin motivo'}.")
        waiting = pending()
        if waiting:
            lines.append("Esperando revisión:")
            lines += [f"#{r['id']} «{r['title']}» ({r['kind']}, {r['language']})" for r in waiting[:MAX_LIST]]
        else:
            lines.append("No hay vídeos esperando revisión.")
        return "\n".join(lines) + f"\nPágina de Studio: {link.public_url}/"

    def preparar(args: dict) -> str:
        job = link.call("/api/pilot", {"tema": args["tema"]})["job"]
        about = f" sobre «{job['tema']}»" if job.get("tema") else ""
        return (f"Hecho: el piloto ya está preparando un vídeo{about} (trabajo #{job['id']}). Tarda alrededor de "
                "una hora; te aviso cuando esté en la página de Studio para revisarlo.")

    def find_review(wanted: str) -> dict:
        waiting = pending()
        number = re.fullmatch(r"#?\s*(\d{1,9})", wanted.strip())
        if number:
            hits = [r for r in waiting if r["id"] == int(number.group(1))]
        else:
            hits = [r for r in waiting if plain(wanted) in plain(r["title"])]
        if len(hits) == 1:
            return hits[0]
        if not waiting:
            raise ToolError("No hay ningún vídeo esperando revisión")
        options = "; ".join(f"#{r['id']} «{r['title']}» ({r['kind']}, {r['language']})" for r in waiting[:8])
        raise ToolError(("Hay varios que encajan" if hits else f"No encuentro «{wanted}» entre los pendientes")
                        + f": {options}. Dime el número.")

    def no_doubts(review: dict) -> dict:
        detail = link.call(f"/api/reviews/{review['id']}")
        if detail.get("warnings"):
            # Decisión de Marc: con dudas, nunca desde el chat. Se le explica y se le manda a la página.
            raise ToolError(f"El vídeo #{review['id']} «{review['title']}» tiene {len(detail['warnings'])} dudas del "
                            "revisor: " + " | ".join(str(w) for w in detail["warnings"][:5])
                            + f". Revísalo y apruébalo tú en {link.public_url}/ escribiendo PUBLICAR.")
        return detail

    def check_publish(args: dict) -> None:
        no_doubts(find_review(args["video"]))  # antes de preguntar: con dudas ni se pregunta

    def publicar(args: dict) -> str:
        review = find_review(args["video"])
        detail = no_doubts(review)  # otra vez: por si cambió algo mientras Marc confirmaba
        link.call(f"/api/reviews/{review['id']}/approve", {"video_sha256": detail["video_sha256"]})
        return (f"Aprobado: el vídeo #{review['id']} «{review['title']}» se está subiendo a YouTube. Te aviso cuando "
                "termine.")

    def summarize_publish(args: dict) -> str:
        try:
            review = find_review(args["video"])
        except ToolError:
            return f"Publicar en YouTube el vídeo «{args['video']}»"
        return f"Publicar en YouTube el vídeo #{review['id']} «{review['title']}» ({review['kind']}, {review['language']})"

    return [
        Tool(
            name="studio_estado",
            description="Cómo va ARTHUR Studio: si se está haciendo un vídeo y qué vídeos esperan revisión.",
            risk=Risk.READ,
            run=estado,
        ),
        Tool(
            name="studio_preparar_video",
            description="Pide a ARTHUR Studio un vídeo nuevo AHORA (sin esperar al de las 6:00), con un tema "
                        "opcional.",
            risk=Risk.WRITE,
            params=(Param("tema", str, "p. ej. «Séneca y la ira» (vacío = lo elige el piloto)", required=False,
                          default="", max_length=200),),
            summarize=lambda a: "Preparar un vídeo nuevo en Studio ahora" + (f" sobre «{a['tema']}»" if a["tema"] else ""),
            run=preparar,
        ),
        Tool(
            name="studio_publicar",
            description="Publica en YouTube un vídeo de ARTHUR Studio que espera revisión (por su número o "
                        "título). Solo si el revisor no encontró dudas.",
            risk=Risk.DANGEROUS,
            params=(Param("video", str, "número (#7) o parte del título", max_length=120),),
            check=check_publish,
            summarize=summarize_publish,
            run=publicar,
        ),
    ]
