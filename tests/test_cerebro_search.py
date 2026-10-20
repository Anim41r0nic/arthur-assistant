"""Búsqueda híbrida del cerebro (cerebro/search.py): palabras (FTS5) + significado (coseno), fusionadas con RRF.

Sin red: el embedder es falso y acerca textos por «conceptos» (palabras clave que deciden el vector).
"""

import math
import tempfile
import unittest
from pathlib import Path

from arthur.cerebro.chunks import Doc
from arthur.cerebro.embed import DIM
from arthur.cerebro.index import Index
from arthur.cerebro.search import (MIN_COSINE, MIN_SUPPORT, Hit, best_cosine, found, fts_query, search,
                                   words_support)
from arthur.notes.engines import EngineError

# Concepto → palabras que lo activan (sin tildes y en minúsculas). El falso «entiende» sinónimos y catalán.
CONCEPTOS = {
    0: ("limite", "tiende", "limit", "successio", "limte", "sucesion"),
    1: ("lentejas", "receta", "cocina"),
    2: ("derivada", "hopital"),
}


def plano(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


class Conceptos:
    def __init__(self, falla=False):
        self.falla, self.kinds = falla, []

    def embed(self, texts, *, kind="embed"):
        self.kinds.append(kind)
        if self.falla:
            raise EngineError("Workers AI caído")
        out = []
        for t in texts:
            v = [0.0] * DIM
            t = plano(t)
            for dim, palabras in CONCEPTOS.items():
                if any(p in t for p in palabras):
                    v[dim] = 1.0
            if "mongolia" in t:
                # Nada que ver con las notas: coseno 0,30 con el concepto 0 y 0 con los demás.
                v[0], v[9] = 0.3, math.sqrt(1 - 0.09)
            if not any(v):
                v[10] = 1.0
            out.append(v)
        return out


def doc(ref, text, *, source="apuntes", title="Nota", subject="Cálculo"):
    return Doc(source=source, ref=ref, title=title, subject=subject, changed_at="2026-10-17T20:05:00+02:00",
               text=text, links=())


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = Index(Path(self.tmp.name) / "indice.sqlite3")
        self.index.sync([doc("apunte:1", "# Definición\nEl límite de una sucesión es el valor al que se acerca.",
                             title="Límite de una sucesión"),
                         doc("apunte:2", "# Regla\nLa derivada del cociente con L'Hôpital.", title="Derivadas")],
                        source="apuntes", alive_refs=None)
        self.index.sync([doc("cocina/lentejas.md", "# Lentejas\nReceta de la abuela con chorizo.",
                             source="obsidian:cerebro", title="Lentejas", subject="cocina")],
                        source="obsidian:cerebro", alive_refs=None)
        self.emb = Conceptos()
        self.index.embed_pending(self.emb)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def refs(self, hits):
        return [h.ref for h in hits]

    def test_sinonimo_por_significado(self):
        self.assertEqual(self.index.chunks_fts(fts_query("a qué tiende la serie"), 30, None), [])
        hits = search(self.index, self.emb, "¿a qué tiende la serie?")
        self.assertEqual(hits[0].ref, "apunte:1")
        self.assertEqual(hits[0].title, "Límite de una sucesión")
        self.assertEqual(hits[0].heading, "Definición")
        self.assertIn("valor al que se acerca", hits[0].text)
        self.assertEqual((hits[0].source, hits[0].subject), ("apuntes", "Cálculo"))
        self.assertAlmostEqual(hits[0].cosine, 1.0, places=2)
        self.assertTrue(found(hits, best_cosine(hits)))
        self.assertEqual(self.emb.kinds[-1], "ask")  # la pregunta de Marc va con la prioridad más alta

    def test_catalan_y_faltas(self):
        # Review Focus 2: catalán sin tildes y una falta de ortografía siguen dando el apunte.
        for q in ("limit d'una successio", "limte sucesion"):
            hits = search(self.index, self.emb, q)
            self.assertEqual(hits[0].ref, "apunte:1", q)
            self.assertTrue(found(hits, best_cosine(hits)), q)
        # Sin significado, «limit*» y «sucesion*» casan por prefijo y sin tildes.
        self.assertEqual(search(self.index, None, "limit d'una successio")[0].ref, "apunte:1")
        self.assertEqual(search(self.index, None, "limte sucesion")[0].ref, "apunte:1")

    def test_sin_embedder(self):
        for emb in (None, Conceptos(falla=True)):
            hits = search(self.index, emb, "receta de lentejas")
            self.assertEqual(self.refs(hits), ["cocina/lentejas.md"])
            self.assertIsNone(hits[0].cosine)
            self.assertTrue(hits[0].by_words)
            self.assertIsNone(best_cosine(hits))
            self.assertTrue(found(hits, None))

    def test_no_lo_encuentra(self):
        hits = search(self.index, self.emb, "capital de Mongolia")
        self.assertAlmostEqual(best_cosine(hits), 0.30, places=2)
        self.assertFalse(any(h.by_words for h in hits))
        self.assertFalse(found(hits, best_cosine(hits)))
        self.assertFalse(found([], None))

    def test_pendiente_por_palabras_cuenta(self):
        # Un trozo aún sin embedding (presupuesto gastado) que casa por palabras es un «sí lo tengo».
        self.index.sync([doc("apunte:9", "Las integrales impropias de Mongolia.", title="Integrales")],
                        source="apuntes", alive_refs=None)
        hits = search(self.index, self.emb, "integrales mongolia")
        self.assertEqual(hits[0].ref, "apunte:9")
        self.assertFalse(hits[0].embedded)
        self.assertTrue(found(hits, best_cosine(hits)))

    def test_filtro_de_fuentes(self):
        hits = search(self.index, self.emb, "receta lentejas", sources=("apuntes",))
        self.assertNotIn("cocina/lentejas.md", self.refs(hits))
        hits = search(self.index, self.emb, "receta lentejas", sources=("obsidian",))
        self.assertEqual(self.refs(hits), ["cocina/lentejas.md"])

    def test_otra_forma_de_la_palabra(self):
        self.index.sync([doc("t1.pdf", "• 1947: s'inventa el transistor; gran revolució.", source="pdf",
                             title="Tema1", subject="PDF"),
                         doc("t2.pdf", "Ja fa molts anys que hi ha ordinadors.", source="pdf", title="Tema2",
                             subject="PDF")], source="pdf", alive_refs=None)
        hits = search(self.index, None, "¿en qué año se inventó?")
        self.assertEqual(self.refs(hits), ["t1.pdf"])
        self.assertEqual(self.refs(search(self.index, None, "any")), [])  # «any» no es «anys»

    def test_k(self):
        self.assertLessEqual(len(search(self.index, self.emb, "límite derivada lentejas", k=2)), 2)


class FtsQueryTest(unittest.TestCase):
    def test_prefijos_con_or(self):
        self.assertEqual(fts_query("limit d'una successio"), '"limit"* OR "succes"*')
        self.assertEqual(fts_query("Límite sucesión"), '"límite"* OR "sucesi"*')

    def test_palabras_largas_por_su_raiz(self):
        # Seis letras de raíz: «inventó» encuentra «s'inventa», «postulados» encuentra «postulats» (catalán).
        self.assertEqual(fts_query("inventó postulados"), '"invent"* OR "postul"*')
        # Dos formas con la misma raíz dan una sola condición.
        self.assertEqual(fts_query("sucesión sucesiones"), '"sucesi"*')

    def test_palabras_cortas_exactas(self):
        # Con prefijo, «any»* casaría con «anys», «anything»…, y «tema»* con cualquier «temas»: ruido que gana.
        self.assertEqual(fts_query("qui any tema bucle"), '"qui" OR "any" OR "tema" OR "bucle"*')

    def test_nunca_pasa_la_consulta_tal_cual(self):
        q = fts_query('NEAR("x" y) OR col:abc AND -zzz "comillas"')
        for token in q.split(" OR "):
            self.assertRegex(token, r'^"[^"]+"\*?$')
        self.assertIn('"near"', q)
        self.assertIn('"comill"*', q)

    def test_solo_cortas_o_vacias(self):
        self.assertIsNone(fts_query("a y o"))
        self.assertIsNone(fts_query("   "))
        self.assertIsNone(fts_query("¿qué es?"))  # solo palabras vacías

    def test_sin_repetir(self):
        self.assertEqual(fts_query("serie serie Serie"), '"serie"*')


class FakeIndex:
    """Listas fijas para comprobar la fusión."""

    def __init__(self, fts, cos):
        self.fts, self.cos, self.calls = fts, cos, []

    def chunks_fts(self, query, limit, sources):
        self.calls.append(("fts", limit, sources))
        return [(i, 10.0 - n) for n, i in enumerate(self.fts)]

    def nearest(self, vector, n, sources):
        self.calls.append(("cos", n, sources))
        return [(i, 0.9 - n / 10) for n, i in enumerate(self.cos)]

    def chunk(self, cid):
        return {"id": cid, "doc_id": cid, "heading": "", "text": f"t{cid}", "title": f"T{cid}",
                "source": "apuntes", "ref": f"apunte:{cid}", "subject": "", "embedded": True}


class RrfTest(unittest.TestCase):
    def test_rrf(self):
        idx = FakeIndex([1, 2, 3], [3, 1, 4])
        hits = search(idx, Conceptos(), "límite", sources=("apuntes",))
        self.assertEqual([h.chunk_id for h in hits], [1, 3, 2, 4])
        self.assertAlmostEqual(hits[0].score, 1 / 61 + 1 / 62)
        self.assertEqual(idx.calls, [("fts", 30, ("apuntes",)), ("cos", 30, ("apuntes",))])
        self.assertIsInstance(hits[0], Hit)
        self.assertEqual([h.by_words for h in hits], [True, True, True, False])
        self.assertEqual(hits[3].cosine, 0.7)
        self.assertIsNone(hits[2].cosine)  # el 2 no salió entre los 30 más parecidos

    def test_umbral(self):
        self.assertEqual(MIN_COSINE, 0.45)
        h = Hit(1, 0.1, "T", "", "t", "apuntes", "apunte:1", "")
        self.assertTrue(found([h], 0.45))
        self.assertFalse(found([h], 0.44))

    def test_apoyo_solo_cuenta_sin_significado(self):
        h = Hit(1, 0.1, "T", "", "t", "apuntes", "apunte:1", "")
        self.assertEqual(MIN_SUPPORT, 0.35)
        self.assertTrue(found([h], None))  # sin medir el apoyo, como antes
        self.assertFalse(found([h], None, support=0.0))
        self.assertFalse(found([h], None, support=0.34))
        self.assertTrue(found([h], None, support=0.35))
        self.assertFalse(found([], None, support=1.0))
        # Con coseno manda el coseno: el apoyo por palabras no se mira.
        self.assertTrue(found([h], 0.5, support=0.0))
        self.assertFalse(found([h], 0.3, support=1.0))


class WordsSupportTest(unittest.TestCase):
    """«No lo encuentro» cuando solo hay búsqueda por palabras (presupuesto gastado o Workers AI caído).

    Con OR y prefijos casi cualquier pregunta casa algo, así que se mira cuánto de la pregunta está en el mejor trozo
    (palabras raras pesan más) y si trae palabras que no salen en ninguna nota ni son una falta de otra."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = Index(Path(self.tmp.name) / "indice.sqlite3")
        self.index.sync([
            doc("t1.pdf", "• 1935-36: A. Church i A. Turing: fonaments de la informàtica teòrica.\n"
                          "• 1947: s'inventa el transistor; gran revolució en el camp de l'electrònica.",
                source="pdf", title="Tema1", subject="MTP 1"),
            doc("t2.pdf", "Els ordinadors fan servir l'àlgebra de Boole des de fa molts anys.", source="pdf",
                title="Tema2", subject="ETC 1"),
        ], source="pdf", alive_refs=None)
        self.index.sync([
            doc("Bucles.md", "# Do-while\nUn do-while ejecuta el cuerpo al menos una vez: la condición se "
                             "comprueba al final de cada vuelta.", source="obsidian:cerebro", title="04 Bucles"),
            doc("Plan.md", "# Métricas\nSe mide con las descargas de la muestra. Umbral para seguir: 15.",
                source="obsidian:cerebro", title="Plan de validación"),
            doc("Agenda.md", "# Clases\nMarc tiene clase de cálculo los lunes; el número de teléfono de la "
                             "secretaría está en el tablón.", source="obsidian:cerebro", title="Agenda"),
            doc("Diario.md", "# Hoy\nMarc fue a clase.", source="obsidian:cerebro", title="Diario"),
        ], source="obsidian:cerebro", alive_refs=None)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def support(self, q):
        hits = search(self.index, None, q)
        return hits, words_support(self.index, q, hits)

    def test_lo_que_si_esta(self):
        for q in ("¿En qué año se inventó el transistor?",
                  "Según la nota de bucles, ¿un do-while ejecuta el cuerpo al menos una vez?",
                  "En el plan de validación, ¿con qué se mide la muestra?"):
            hits, s = self.support(q)
            self.assertGreaterEqual(s, MIN_SUPPORT, q)
            self.assertTrue(found(hits, None, support=s), q)

    def test_palabra_que_no_sale_en_ninguna_nota(self):
        # «color» y «favorito» no salen en ninguna nota ni se parecen a ninguna palabra: no se puede contestar.
        hits, s = self.support("Solo según «Tema1.pdf», ¿cuál era el color favorito de A. Turing?")
        self.assertTrue(hits)  # por palabras algo casa («Turing», «Tema1»)…
        self.assertEqual(s, 0.0)
        self.assertFalse(found(hits, None, support=s))  # …pero eso no es la respuesta

    def test_falta_de_ortografia_no_es_palabra_desconocida(self):
        # «buelta» está a una letra de «vuelta» y «cuerpp» de «cuerpo»: son faltas, no palabras que falten.
        hits, s = self.support("En la nota de bucles, ¿el do-while ejecuta el cuerpp al menos una buelta?")
        self.assertEqual(hits[0].ref, "Bucles.md")
        self.assertGreaterEqual(s, MIN_SUPPORT)
        # Dos letras de diferencia ya no es una falta: «almenos» (sin espacio) se toma por palabra que no está.
        self.assertEqual(self.support("¿el do-while ejecuta el cuerpo almenos una vez?")[1], 0.0)

    def test_titulos_y_rutas_son_palabras_conocidas(self):
        # «Bucles» solo sale en el título de la nota (el índice de palabras guarda encabezado y texto): no es una
        # palabra desconocida.
        self.assertEqual(self.index.count_fts('"bucles"*'), 0)
        hits, s = self.support("Según la nota de bucles, ¿el do-while ejecuta el cuerpo?")
        self.assertGreaterEqual(s, MIN_SUPPORT)

    def test_las_palabras_estan_repartidas(self):
        # Todas las palabras existen, pero cada una en una nota distinta: ningún trozo tiene la respuesta.
        hits, s = self.support("¿El transistor, la secretaría, el umbral y la vuelta?")
        self.assertEqual(len(hits), 4)
        self.assertAlmostEqual(s, 0.25)  # cuatro igual de raras y el mejor trozo tiene una
        self.assertFalse(found(hits, None, support=s))

    def test_las_raras_pesan_mas(self):
        # «Marc» y «clase» salen en dos notas; «secretaría», en una. Tener la rara vale más que tener las comunes.
        self.assertEqual(self.index.chunk_count(), 6)
        self.assertAlmostEqual(self.support("¿Marc, clase o secretaría?")[1], 1.0)  # la agenda lo tiene todo
        # «Marc» está en 2 de 6 trozos (idf log 3) y «umbral» en 1 (log 6): el plan, con «umbral», gana a la
        # agenda, con «Marc», y el apoyo es log 6 / (log 3 + log 6) ≈ 0,62, no la mitad.
        self.assertAlmostEqual(self.support("¿Marc o el umbral?")[1], math.log(6) / (math.log(3) + math.log(6)))

    def test_sin_resultados_o_sin_palabras(self):
        self.assertEqual(words_support(self.index, "capital de Mongolia", []), 0.0)
        hits = search(self.index, None, "transistor")
        self.assertEqual(words_support(self.index, "¿qué es?", hits), 1.0)  # nada que comprobar: no se veta

    def test_respuesta_dice_no_lo_encuentro(self):
        from arthur.cerebro.answer import NOT_FOUND, Links, ask
        r = ask("Solo según «Tema1.pdf», ¿cuál era el color favorito de A. Turing?", index=self.index,
                embedder=None, cloud=None, local=None, budget=None, links=Links(""))
        self.assertFalse(r["found"])
        self.assertTrue(r["answer"].startswith(NOT_FOUND))
        r = ask("¿En qué año se inventó el transistor?", index=self.index, embedder=None, cloud=None, local=None,
                budget=None, links=Links(""))
        self.assertTrue(r["found"])
        self.assertTrue(r["answer"].startswith("Esto es lo que tengo:"))


if __name__ == "__main__":
    unittest.main()
