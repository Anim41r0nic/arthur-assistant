"""Hacer los PDF de los apuntes.

Dos tipos (decisión de Marc del 2026-10-12: «las dos opciones»):

- LIMPIO: el texto transcrito (Markdown con fórmulas en LaTeX) maquetado con Typst. Camino:
  Markdown --pandoc--> Typst --typst--> PDF. Typst dibuja las fórmulas como LaTeX (integrales, sumatorios,
  matrices, lógica...), que es lo que Marc necesita para cálculo y discreta.
- ESCANEO: las fotos originales enderezadas y limpias, con el texto invisible encima para poder buscar y copiar
  (ocrmypdf + Tesseract, paquetes de Debian). Se parece al original; el texto escondido es peor que el limpio con
  letra a mano, pero sirve para buscar.

Por qué hay que «arreglar» lo que escribe pandoc: el pandoc de Debian 13 (3.1.11) es de 2023 y usa nombres de
símbolos que Typst 0.14-0.15 ha quitado (p. ej. `sect` para ∩, `diff` para ∂, `times.circle` para ⊗). Medido el
2026-10-12 con ~230 órdenes de LaTeX: fix_math() los cambia por los nombres nuevos SOLO dentro de las fórmulas
(en el texto normal «diff» es una palabra y no se toca).

Seguridad: pandoc escapa todo (`#`, `$`...) y se le quitan las extensiones que dejan pasar código tal cual
(raw_tex, raw_html, raw_attribute), así que nada de lo transcrito puede convertirse en código de Typst. Además
Typst compila en una carpeta temporal con --root (no puede leer otros ficheros) y sin caché de paquetes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

PANDOC_FROM = "markdown+tex_math_dollars-raw_tex-raw_html-raw_attribute-citations-yaml_metadata_block"
TIMEOUT = 120          # segundos para pandoc y Typst (un tema largo tarda 1-2 s)
SCAN_TIMEOUT = 1800    # ocrmypdf en el servidor sin gráfica: ~20-40 s por página

# Nombre viejo (pandoc 3.1) -> nombre de Typst 0.15. Se compara por trozos separados por puntos desde el
# principio: «sect.big» se convierte en «inter.big».
RENAMED = {
    "sect": "inter", "diff": "partial", "planck.reduce": "planck", "ohm.inv": "Omega.inv",
    "times.circle": "times.o", "dot.circle": "dot.o", "ast.circle": "ast.o", "plus.circle": "plus.o",
    "minus.circle": "minus.o", "div.circle": "div.o",
    "angle.l": "chevron.l", "angle.r": "chevron.r",
    "lt.tri": "lt.closed", "gt.tri": "gt.closed",
}

STYLES = {
    # nombre: (letra del texto, color de los títulos, márgenes)
    "clasico": ("New Computer Modern", "#000000", "2.5cm"),   # como un documento de LaTeX
    "moderno": ("Noto Sans", "#0f6b3a", "2.2cm"),             # sin remates, títulos en verde (como el portfolio)
    "compacto": ("New Computer Modern", "#000000", "1.5cm"),  # más texto por hoja, para imprimir
}
DEFAULT_STYLE = "clasico"
LANGS = {"es": "es", "ca": "ca", "en": "en"}


class RenderError(Exception):
    pass


def tool(name: str) -> str:
    """Ruta del programa: ARTHUR_TYPST / ARTHUR_PANDOC / ARTHUR_OCRMYPDF / ARTHUR_IMG2PDF o el del sistema."""
    return os.environ.get(f"ARTHUR_{name.upper()}", name)


def available(name: str) -> bool:
    return shutil.which(tool(name)) is not None


# --- arreglar las fórmulas de pandoc ----------------------------------------------------------------------------

def _rename(name: str) -> str:
    parts = name.split(".")
    for k in range(len(parts), 0, -1):
        head = ".".join(parts[:k])
        if head in RENAMED:
            return ".".join([RENAMED[head], *parts[k:]])
    return name


def _fix_formula(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == '"':  # texto entre comillas dentro de la fórmula: tal cual
            j = i + 1
            while j < len(body) and body[j] != '"':
                j += 2 if body[j] == "\\" else 1
            out.append(body[i:j + 1])
            i = j + 1
        elif c == "\\":
            out.append(body[i:i + 2])
            i += 2
        elif c.isalpha() and (i == 0 or not (body[i - 1].isalnum() or body[i - 1] in "._")):
            j = i
            while j < len(body) and (body[j].isalpha() or (body[j] == "." and j + 1 < len(body) and body[j + 1].isalpha())):
                j += 1
            out.append(_rename(body[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return _space_cases("".join(out).replace('delim: "||"', "delim: bar.double"))


def _space_cases(body: str) -> str:
    """En una función a trozos (\\begin{cases}) Typst pega el valor y la condición: «−x x<0» sale «−xx<0».
    Se añade un hueco («quad») tras cada «&» SOLO dentro de cases(...), no en aligned (ahí & alinea el «=»)."""
    out, i = [], 0
    while True:
        start = body.find("cases(", i)
        if start < 0 or (start > 0 and (body[start - 1].isalnum() or body[start - 1] == ".")):
            if start < 0:
                out.append(body[i:])
                return "".join(out)
            out.append(body[i:start + 6])
            i = start + 6
            continue
        depth, j = 0, start + 5
        while j < len(body):
            depth += {"(": 1, ")": -1}.get(body[j], 0)
            if depth == 0:
                break
            j += 1
        out.append(body[i:start] + body[start:j + 1].replace(" & ", " & quad "))
        i = j + 1


def fix_math(typst: str) -> str:
    """Cambia los nombres viejos de símbolos SOLO dentro de las fórmulas ($...$) del Typst que escribe pandoc.

    Se recorre carácter a carácter: `\\$` es un dólar escrito (no abre fórmula), y dentro de la fórmula lo que va
    entre comillas es texto. Fuera de las fórmulas no se toca nada.
    """
    out, i, n = [], 0, len(typst)
    while i < n:
        c = typst[i]
        if c == "\\":
            out.append(typst[i:i + 2])
            i += 2
        elif c == "$":
            j, quoted = i + 1, False
            while j < n:
                if typst[j] == "\\":
                    j += 2
                    continue
                if typst[j] == '"':
                    quoted = not quoted
                elif typst[j] == "$" and not quoted:
                    break
                j += 1
            if j >= n:  # sin cerrar: se deja como está (Typst ya dirá el error)
                out.append(typst[i:])
                break
            out.append("$" + _fix_formula(typst[i + 1:j]) + "$")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --- PDF limpio ---------------------------------------------------------------------------------------------------

def _typst_string(text: str) -> str:
    """Un texto dentro de "..." de Typst: se escapan la barra y las comillas, y nada de saltos de línea."""
    text = " ".join(text.split())
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_markup(text: str) -> str:
    """Texto suelto dentro de [...] de Typst: se escapan los caracteres que significan algo."""
    text = " ".join(text.split())
    return "".join("\\" + c if c in "\\#$*_`<>@[]~/=-+\"'" else c for c in text)


def preamble(title: str, style: str, lang: str, when: date | None = None) -> str:
    font, color, margin = STYLES.get(style, STYLES[DEFAULT_STYLE])
    when = when or date.today()
    size = "10pt" if style == "compacto" else "11pt"
    return f"""#set document(title: {_typst_string(title)})
#set page(paper: "a4", margin: {margin}, numbering: "1 / 1")
#set text(font: ("{font}", "New Computer Modern"), size: {size}, lang: "{LANGS.get(lang, "es")}")
#set par(justify: true)
#show heading: set text(fill: rgb("{color}"))
#show math.equation: set text(font: "New Computer Modern Math")
#show raw: set text(font: "DejaVu Sans Mono")
#align(center)[#text(size: 18pt, weight: "bold", fill: rgb("{color}"))[{_escape_markup(title)}] \\
#text(size: 9pt, fill: luma(110))[{when.strftime("%d/%m/%Y")}]]
#v(0.8em)
"""


def _run(cmd: list[str], *, cwd: Path, timeout: int, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "LANG": "C.UTF-8",
           "TYPST_PACKAGE_PATH": str(cwd / "no-packages"), "TYPST_PACKAGE_CACHE_PATH": str(cwd / "no-packages")}
    try:
        return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, timeout=timeout, env=env, check=False)
    except FileNotFoundError:
        raise RenderError(f"Falta el programa «{cmd[0]}» (en el servidor va dentro del contenedor)") from None
    except subprocess.TimeoutExpired:
        raise RenderError(f"«{Path(cmd[0]).name}» ha tardado demasiado") from None


def _first_error(stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if line.strip()]
    return " ".join(lines[:6])[:400] or "sin detalles"


def markdown_to_typst(markdown: str, workdir: Path) -> str:
    result = _run([tool("pandoc"), "--from", PANDOC_FROM, "--to", "typst", "--wrap", "preserve"],
                  cwd=workdir, timeout=TIMEOUT, stdin=markdown.encode("utf-8"))
    if result.returncode != 0:
        raise RenderError("pandoc no ha podido leer el texto: " + _first_error(result.stderr.decode(errors="replace")))
    return fix_math(result.stdout.decode("utf-8"))


def clean_pdf(markdown: str, title: str, *, style: str = DEFAULT_STYLE, lang: str = "es",
              when: date | None = None) -> bytes:
    """El PDF limpio (bytes). Lanza RenderError con el motivo si algo falla."""
    if style not in STYLES:
        raise RenderError(f"Estilo desconocido: {style}")
    with tempfile.TemporaryDirectory(prefix="apuntes-") as tmp:
        work = Path(tmp)
        body = markdown_to_typst(markdown, work)
        (work / "apuntes.typ").write_text(preamble(title, style, lang, when) + body, encoding="utf-8")
        cmd = [tool("typst"), "compile", "--root", str(work)]
        for fonts in ("/usr/share/fonts/truetype/noto", "/usr/share/fonts/opentype/noto"):
            if Path(fonts).is_dir():
                cmd += ["--font-path", fonts]
        result = _run(cmd + ["apuntes.typ", "apuntes.pdf"], cwd=work, timeout=TIMEOUT)
        if result.returncode != 0:
            raise RenderError("Typst no ha podido maquetar: " + _first_error(result.stderr.decode(errors="replace")))
        return (work / "apuntes.pdf").read_bytes()


# --- PDF escaneado con texto buscable ----------------------------------------------------------------------------

def scan_pdf(sources: list[Path], out: Path, *, langs: str = "spa+cat+eng") -> None:
    """Fotos (JPEG/PNG) y PDF originales -> un PDF enderezado y limpio con texto invisible encima (ocrmypdf).

    Las fotos se juntan primero en un PDF con img2pdf (respeta el giro del móvil si es válido). Los PDF que ya
    traen texto conservan el suyo (--skip-text): no se estropea un PDF bueno.
    """
    if not sources:
        raise RenderError("No hay páginas")
    with tempfile.TemporaryDirectory(prefix="escaneo-") as tmp:
        work = Path(tmp)
        parts: list[Path] = []
        images: list[Path] = []

        def flush_images():
            if images:
                part = work / f"parte{len(parts):03d}.pdf"
                result = _run([tool("img2pdf"), "--rotation=ifvalid", *map(str, images), "-o", str(part)],
                              cwd=work, timeout=TIMEOUT)
                if result.returncode != 0:
                    raise RenderError("img2pdf: " + _first_error(result.stderr.decode(errors="replace")))
                parts.append(part)
                images.clear()

        for src in sources:
            if src.suffix == ".pdf":
                flush_images()
                parts.append(src)
            else:
                images.append(src)
        flush_images()

        if len(parts) == 1:
            joined = parts[0]
        else:
            joined = work / "junto.pdf"
            result = _run([tool("qpdf"), "--empty", "--pages", *map(str, parts), "--", str(joined)],
                          cwd=work, timeout=TIMEOUT)
            if result.returncode not in (0, 3):  # 3 = avisos sin importancia
                raise RenderError("qpdf: " + _first_error(result.stderr.decode(errors="replace")))
        tmp_out = work / "escaneo.pdf"
        result = _run([tool("ocrmypdf"), "--skip-text", "--rotate-pages", "--deskew", "--clean-final",
                       "--language", langs, "--optimize", "1", "--output-type", "pdf", "--jobs", "2",
                       str(joined), str(tmp_out)], cwd=work, timeout=SCAN_TIMEOUT)
        if result.returncode != 0:
            raise RenderError("ocrmypdf: " + _first_error(result.stderr.decode(errors="replace")))
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = out.with_suffix(".tmp")
        shutil.copyfile(tmp_out, partial)
        os.replace(partial, out)
