#!/usr/bin/env -S python3 -I
"""Mide, en local y sin red, lo bien que la búsqueda del cerebro encuentra el apunte correcto.

Uso (desde la raíz del repo; `-I` porque lee ficheros ajenos al proyecto):

    python -I scripts/evaluar-cerebro.py [--evaluacion RUTA] [--amplio] [--json SALIDA]

Lee `evaluacion.json` (preguntas con `pregunta`, `fuente` = ruta absoluta y `tipo`), monta un índice temporal (carpeta
temporal del sistema) con las clases reales del cerebro (sources → chunks → index → search) y, por cada pregunta, mira:

- en las que SÍ están: ¿sale la `fuente` entre los 3 primeros resultados (top-3) y la primera (top-1)?
- en las `no_esta`: ¿`found` dice False? (el dato no está en el fichero citado; la fuente se indexa igual)

Dos modos de búsqueda:
- «palabras»: sin embedder (embedder=None), como cuando Workers AI está caído.
- «falso»: un embedder determinista de palabras con hash (sin red). NO mide el significado real (bge-m3): solo
  comprueba que el camino híbrido (embed_pending + coseno + RRF + found) no se rompe con vectores.

Corpus: por defecto SOLO las fuentes de la evaluación, copiadas a una carpeta temporal (los escáneres reales no
siguen enlaces simbólicos). Con `--amplio` se indexan además todos los PDF de la carpeta ARTHUR_EVAL_PDF y toda la bóveda
ARTHUR_EVAL_BOVEDA (en solo lectura), que es más parecido a lo que habrá en el servidor.

Rutas (variables de entorno, con estos valores por defecto):
  ARTHUR_EVAL_PREGUNTAS  ~/cerebro-evaluacion/evaluacion.json
  ARTHUR_EVAL_PDF        ~/PDF
  ARTHUR_EVAL_BOVEDA     ~/Obsidian

Nada se escribe en las carpetas de origen: el índice y las copias viven en un directorio temporal que se borra.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # con -I no entra la carpeta del script ni la actual

from arthur.cerebro.embed import DIM  # noqa: E402
from arthur.cerebro.index import Index  # noqa: E402
from arthur.cerebro.search import best_cosine, found, search, words_support  # noqa: E402
from arthur.cerebro.sources import obsidian, pdf  # noqa: E402

EVALUACION = Path(os.environ.get("ARTHUR_EVAL_PREGUNTAS") or Path.home() / "cerebro-evaluacion/evaluacion.json")
UNI = Path(os.environ.get("ARTHUR_EVAL_PDF") or Path.home() / "PDF")
VAULT = Path(os.environ.get("ARTHUR_EVAL_BOVEDA") or Path.home() / "Obsidian")
TMP_BASE = None  # la carpeta temporal del sistema
TOP = 3
K = 8  # los mismos trozos que pide answer.py


class HashEmbedder:
    """Embedder falso y determinista: cada palabra (sin tildes) suma 1 en la dimensión que da su hash."""

    def embed(self, texts, *, kind="embed"):
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in _plain(t.lower()).split():
                w = "".join(c for c in w if c.isalnum())
                if len(w) >= 3:
                    v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % DIM] += 1.0
            if not any(v):
                v[0] = 1.0
            n = math.sqrt(sum(x * x for x in v))
            out.append([x / n for x in v])
        return out


def _plain(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _short(path: str) -> str:
    """Carpeta y nombre: dos notas «Parcial 1.md» de asignaturas distintas no deben parecer la misma."""
    p = Path(path)
    return f"{p.parent.name}/{p.name}"


def _copy_sources(questions: list[dict], tmp: Path) -> tuple[Path, Path]:
    """Copia cada fuente a <tmp>/pdf o <tmp>/vault manteniendo su ruta relativa (así `ref` coincide)."""
    pdf_root, vault_root = tmp / "pdf", tmp / "vault"
    for src in sorted({q["fuente"] for q in questions}):
        p = Path(src)
        if p.suffix.lower() == ".pdf":
            dest = pdf_root / p.relative_to(UNI)
        else:
            dest = vault_root / p.relative_to(VAULT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
    return pdf_root, vault_root


def build_index(questions: list[dict], tmp: Path, amplio: bool) -> tuple[Index, dict[tuple[str, str], str]]:
    """El índice y un mapa (source, ref) → ruta absoluta original."""
    if amplio:
        pdf_root, vault_root = UNI, VAULT
    else:
        pdf_root, vault_root = _copy_sources(questions, tmp)
    index = Index(tmp / "indice.sqlite3")
    paths: dict[tuple[str, str], str] = {}
    pdfs = list(pdf.scan(pdf_root))
    index.sync(pdfs, source="pdf", alive_refs=None)
    for d in pdfs:
        paths[("pdf", d.ref)] = str(UNI / d.ref)
    notes = list(obsidian.scan(vault_root, "business-lab"))
    index.sync(notes, source="obsidian:business-lab", alive_refs=None)
    for d in notes:
        paths[(d.source, d.ref)] = str(VAULT / d.ref)
    if pdf.skipped:
        print(f"  PDF sin texto (no indexados): {len(pdf.skipped)}", file=sys.stderr)
    return index, paths


def evaluate(index: Index, embedder, questions: list[dict], paths: dict) -> list[dict]:
    rows = []
    for n, q in enumerate(questions, 1):
        hits = search(index, embedder, q["pregunta"], k=K)
        cos = best_cosine(hits)
        got = [paths.get((h.source, h.ref), f"{h.source}:{h.ref}") for h in hits]
        firsts: list[str] = []
        for g in got:  # documentos distintos en orden (varios trozos del mismo cuentan una vez)
            if g not in firsts:
                firsts.append(g)
        chunk_pos = next((i for i, g in enumerate(got, 1) if g == q["fuente"]), None)
        support = words_support(index, q["pregunta"], hits) if cos is None and hits else None
        is_found = found(hits, cos, support)  # lo mismo que hace answer.ask
        row = {"n": n, "tipo": q["tipo"], "pregunta": q["pregunta"], "fuente": q["fuente"],
               "found": is_found, "best_cosine": cos, "apoyo": support, "puesto_trozo": chunk_pos,
               "top": [_short(g) for g in got[:TOP]], "docs": [_short(g) for g in firsts[:TOP]],
               "texto_1": hits[0].text[:160].replace("\n", " ") if hits else ""}
        if q["tipo"] == "no_esta":
            row["ok"] = row["found_ok"] = not is_found
        else:
            row["top3"] = chunk_pos is not None and chunk_pos <= TOP
            row["found_ok"] = is_found
            # Acierto completo: la fuente sale arriba Y no se contesta «no lo encuentro».
            row["ok"] = row["top3"] and is_found
        rows.append(row)
    return rows


def summary(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Por tipo: preguntas, aciertos completos, fuente en top-3 (solo las que están) y `found` correcto."""
    tipos = ["exacta", "sin_tildes", "con_faltas", "catalan", "no_esta"]
    by = {t: {"total": 0, "ok": 0, "top3": 0, "found_ok": 0} for t in [*tipos, "TOTAL"]}
    for r in rows:
        for t in (r["tipo"], "TOTAL"):
            b = by.setdefault(t, {"total": 0, "ok": 0, "top3": 0, "found_ok": 0})
            b["total"] += 1
            b["ok"] += r["ok"]
            b["top3"] += r.get("top3", False)
            b["found_ok"] += r["found_ok"]
    return by


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--evaluacion", type=Path, default=EVALUACION)
    ap.add_argument("--amplio", action="store_true", help="indexa también todo UNI y toda la bóveda")
    ap.add_argument("--json", type=Path, help="guarda aquí los resultados detallados")
    args = ap.parse_args()
    questions = json.loads(args.evaluacion.read_text(encoding="utf-8"))
    results = {}
    with tempfile.TemporaryDirectory(dir=TMP_BASE, prefix="evaluar-cerebro-") as t:
        tmp = Path(t)
        index, paths = build_index(questions, tmp, args.amplio)
        st = index.stats()
        print(f"Corpus: {st['documentos']} documentos, {st['trozos']} trozos "
              f"({'amplio' if args.amplio else 'solo fuentes'})")
        results["palabras"] = evaluate(index, None, questions, paths)
        stored = index.embed_pending(HashEmbedder(), limit=10**9)
        results["falso"] = evaluate(index, HashEmbedder(), questions, paths)
        index.close()
    print(f"Embeddings falsos guardados: {stored}")
    for modo, rows in results.items():
        print(f"\n== Modo {modo} ==")
        print(f"  {'tipo':<11} acierto  top-3  found-bien")
        for tipo, b in summary(rows).items():
            si = b["total"] if tipo != "no_esta" else 0
            if tipo == "TOTAL":
                si = sum(1 for r in rows if r["tipo"] != "no_esta")
            print(f"  {tipo:<11} {b['ok']:>2}/{b['total']:<2}   {b['top3']:>2}/{si:<2}  {b['found_ok']:>2}/{b['total']}")
        for r in rows:
            mark = "OK " if r["ok"] else "MAL"
            extra = f"found={r['found']}" + (f" cos={r['best_cosine']:.2f}" if r["best_cosine"] is not None else "")
            extra += f" apoyo={r['apoyo']:.2f}" if r.get("apoyo") is not None else ""
            print(f"  {mark} #{r['n']:<2} {r['tipo']:<10} puesto={r['puesto_trozo']} {extra} top={r['docs']}")
    if args.json:
        args.json.write_text(json.dumps({"corpus": st, "resultados": results,
                                         "resumen": {m: summary(r) for m, r in results.items()}},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
