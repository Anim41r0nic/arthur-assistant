"""Búsqueda híbrida: por palabras (FTS5, BM25) y por significado (coseno de embeddings), fusionadas con RRF.

Cada método falla por un lado distinto: las palabras no saben que «a qué tiende la serie» es «límite de una
sucesión», y el significado a veces pierde un nombre propio o un número exacto. Se piden 30 candidatos a cada uno y
se mezclan con Reciprocal Rank Fusion (k = 60): cada trozo suma 1 / (60 + puesto) por cada lista en la que sale. Solo
cuenta el puesto, no la puntuación, así que no hace falta poner en la misma escala BM25 y coseno.

La consulta de palabras se CONSTRUYE (nunca se pasa la pregunta tal cual a FTS5, que tiene su propio lenguaje con
NEAR, OR, comillas, «columna:»…): palabras de 3 letras o más, sin palabras vacías, cada una entre comillas y unidas
con OR. Las tildes las quita el propio índice. Las de 5 letras o más van como prefijo (`*`) y, si pasan de 6, solo con
sus 6 primeras: es una raíz a lo bruto que junta las formas de una palabra y el catalán con el castellano («inventó»
encuentra «s'inventa», «postulados» encuentra «postulats», «limit» encuentra «límite»). Las de 3 y 4 letras van
exactas: con prefijo, «any» casaba con «anys» y «tema» con todos los «temas», y ese ruido ganaba al trozo bueno
(medido con scripts/evaluar-cerebro.py).
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger(__name__)

CANDIDATES = 30  # por lista
RRF_K = 60
PREFIX_MIN = 5  # letras mínimas para buscar por prefijo; las más cortas, exactas
ROOT = 6  # letras de la raíz con que se busca una palabra larga
# Coseno mínimo del mejor trozo para decir «lo tengo». bge-m3 da ~0,3-0,4 entre textos que no tienen nada que ver y
# 0,5 o más entre los que hablan de lo mismo; por debajo de 0,45 se contesta «no lo encuentro» sin llamar al modelo.
MIN_COSINE = 0.45
# Sin significado (solo palabras), «lo tengo» exige que el mejor de los 3 primeros trozos tenga al menos un 35 % del
# peso de la pregunta (ver words_support). Medido con scripts/evaluar-cerebro.py y sus 25 preguntas: las que sí están
# dan 0,40 o más y las que no están y casan por palabras sueltas, 0,29 o menos; 0,35 queda en medio.
MIN_SUPPORT = 0.35
SUPPORT_TOP = 3  # trozos que se miran: los mismos que se ven arriba del todo

# Palabras que salen en casi cualquier pregunta (castellano y catalán, sin tildes): con OR harían casar todo.
_STOPWORDS = frozenset("""
que cual cuales quien como donde cuando cuanto cuanta una uno unos unas los las del con por para pero sin sobre
entre hay son era fue ser este esta esto estos estas ese esa eso esos esas sus mis tus nos les mas muy tambien
algo todo toda todos todas otro otra tiene tengo hace hacer puede segun desde hasta porque pues
els amb per com quin quina quins quines dels aquest aquesta aquests aquestes aixo cap mes tambe
""".split())
# Palabras que hablan de la nota y no de lo que se pregunta («Solo según la nota…», «Segons el PDF…»): no cuentan
# para el apoyo (sí para buscar, que no estorban).
_ABOUT_NOTES = frozenset("solo segun segons nota notas apunte apuntes pdf".split())
_WORD = re.compile(r"\w+")


@dataclass
class Hit:
    chunk_id: int
    score: float  # puntuación RRF (más alta = mejor)
    title: str
    heading: str
    text: str
    source: str
    ref: str
    subject: str
    cosine: float | None = None  # coseno con la pregunta; None si no salió entre los 30 más parecidos o sin embedder
    by_words: bool = False  # ¿salió en la búsqueda por palabras?
    embedded: bool = True  # ¿tiene ya embedding? (si no, solo se puede encontrar por palabras)


def _plain(word: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", word) if unicodedata.category(c) != "Mn")


def fts_query(text: str) -> str | None:
    """La consulta FTS5 segura para una pregunta, o None si no queda ninguna palabra útil."""
    terms: dict[str, None] = {}
    for w in _WORD.findall(text.lower()):
        if len(w) < 3 or _plain(w) in _STOPWORDS:
            continue
        # \w no deja pasar comillas, pero se doblan igual: si algún día cambia el patrón, sigue siendo una cadena.
        term = '"' + w[:ROOT].replace('"', '""') + '"'
        terms.setdefault(term + "*" if len(w) >= PREFIX_MIN else term, None)
    return " OR ".join(terms) if terms else None


def search(index, embedder, query: str, *, k: int = 8, sources: tuple[str, ...] | None = None) -> list[Hit]:
    """Los `k` mejores trozos para la pregunta. Si el embedder es None o falla, busca solo por palabras."""
    expr = fts_query(query)
    by_words = index.chunks_fts(expr, CANDIDATES, sources) if expr else []
    by_meaning: list[tuple[int, float]] = []
    if embedder is not None:
        try:
            (vector,) = embedder.embed([query], kind="ask")
            by_meaning = index.nearest(vector, CANDIDATES, sources)
        except Exception as e:  # sin red, sin presupuesto o respuesta rara: las palabras siguen funcionando
            log.info("Cerebro: búsqueda solo por palabras (%s)", type(e).__name__)
            by_meaning = []
    words_rank = {cid: n for n, (cid, _) in enumerate(by_words, 1)}
    meaning_rank = {cid: n for n, (cid, _) in enumerate(by_meaning, 1)}
    cosines = dict(by_meaning)
    scores: dict[int, float] = {}
    for ranks in (words_rank, meaning_rank):
        for cid, n in ranks.items():
            scores[cid] = scores.get(cid, 0.0) + 1 / (RRF_K + n)
    inf = float("inf")
    # Empates: antes lo que casa por palabras (es más exacto), luego por significado y por último el id (estable).
    order = sorted(scores, key=lambda c: (-scores[c], words_rank.get(c, inf), meaning_rank.get(c, inf), c))
    hits = []
    for cid in order:
        c = index.chunk(cid)
        if c is None:  # borrado entre la búsqueda y ahora (el reindexador va en otro hilo)
            continue
        hits.append(Hit(chunk_id=cid, score=scores[cid], title=c["title"], heading=c["heading"], text=c["text"],
                        source=c["source"], ref=c["ref"], subject=c["subject"], cosine=cosines.get(cid),
                        by_words=cid in words_rank, embedded=bool(c.get("embedded", True))))
        if len(hits) >= k:
            break
    return hits


def best_cosine(hits: list[Hit]) -> float | None:
    """El coseno más alto entre los trozos encontrados, o None si no hubo búsqueda por significado."""
    values = [h.cosine for h in hits if h.cosine is not None]
    return max(values) if values else None


def _content_terms(text: str) -> dict[str, bool]:
    """Las palabras de la pregunta que dicen QUÉ se pregunta, sin tildes: {raíz o palabra: ¿va por prefijo?}.

    Las mismas reglas que fts_query (raíz de 6 letras para las largas, exactas las cortas), menos las palabras que
    solo nombran la nota."""
    terms: dict[str, bool] = {}
    for w in _WORD.findall(text.lower()):
        p = _plain(w)
        if len(w) < 3 or p in _STOPWORDS or p in _ABOUT_NOTES:
            continue
        long = len(w) >= PREFIX_MIN
        terms.setdefault(p[:ROOT] if long else p, long)
    return terms


def _one_letter_apart(a: str, b: str) -> bool:
    """¿Se pasa de `a` a `b` cambiando, quitando o poniendo una sola letra (o son iguales)?"""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _near_known(root: str, vocabulary: list[str]) -> bool:
    """¿Es `root` una falta (o el catalán) de alguna palabra del índice? «buelta» → «vuelta», «cadene» → «cadenas»."""
    n = len(root)
    return any(_one_letter_apart(root, v[:n]) or _one_letter_apart(root, v[:n + 1]) or
               _one_letter_apart(root, v[:n - 1]) for v in vocabulary)


def words_support(index, query: str, hits: list[Hit]) -> float:
    """Cuánto de la pregunta está en el mejor de los primeros trozos, de 0 a 1, para decidir sin significado.

    Con OR y prefijos casi cualquier pregunta casa alguna palabra suelta («Turing», «Marc», «plan»), así que haber
    resultados no basta. Dos comprobaciones:

    1. Una palabra de 5 letras o más que no sale en NINGUNA nota (ni en su título o ruta) y no está a una letra de
       ninguna (no es una falta ni el catalán de otra) → 0: lo que se pregunta no está («color favorito»,
       «calificación», «comenzó»).
    2. Si no, la parte de la pregunta que tiene el mejor de los SUPPORT_TOP primeros trozos, pesando cada palabra
       por lo rara que es en el índice (idf = log(trozos / trozos con ella)): «calificación» pesa mucho más que
       «examen». Sin palabras que mirar (solo palabras vacías), 1: no hay con qué vetar."""
    if not hits:
        return 0.0
    terms = _content_terms(query)
    total_chunks = max(index.chunk_count(), 1)
    weights: dict[str, float] = {}
    vocabulary: list[str] | None = None
    for term, prefix in terms.items():
        n = index.count_fts('"' + term.replace('"', '""') + '"' + ("*" if prefix else ""))
        if n:
            weights[term] = math.log(total_chunks / n) if n < total_chunks else 0.0
        elif prefix:  # las cortas desconocidas («año» frente a «any») no dicen nada: demasiado parecidas a todo
            if vocabulary is None:
                # Las del índice y las de los títulos y rutas («Bucles», «Tema1», «MTP 1»), que nombran la nota.
                names = {_plain(w) for n in index.doc_names() for w in _WORD.findall(n.lower())}
                vocabulary = index.vocabulary(PREFIX_MIN - 1) + [w for w in names if len(w) >= PREFIX_MIN - 1]
            if not _near_known(term, vocabulary):
                return 0.0
    total = sum(weights.values())
    if not total:
        return 1.0
    best = 0.0
    for h in hits[:SUPPORT_TOP]:
        words = {_plain(w) for w in _WORD.findall(f"{h.title} {h.heading} {h.text}".lower())}
        have = sum(wt for t, wt in weights.items()
                   if (any(w.startswith(t) for w in words) if terms[t] else t in words))
        best = max(best, have / total)
    return best


def found(hits: list[Hit], best_cosine: float | None, support: float | None = None) -> bool:
    """¿Hay algo que contestar o se dice «no lo encuentro en tus notas»?

    Con significado, manda el coseno: con OR y prefijos, casi cualquier pregunta casa alguna palabra suelta, así que
    las palabras solas no bastan… salvo en un trozo aún sin embedding, que solo se puede encontrar por palabras.
    Sin significado (Workers AI caído o sin embedder), manda el apoyo por palabras (words_support) si se ha medido;
    sin medirlo, cualquier coincidencia vale (como antes)."""
    if not hits:
        return False
    if best_cosine is None:
        return support is None or support >= MIN_SUPPORT
    if best_cosine >= MIN_COSINE:
        return True
    return any(h.by_words and not h.embedded for h in hits)
