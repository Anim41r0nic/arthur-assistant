"""Índice del cerebro (cerebro/index.py) y embeddings de Workers AI (cerebro/embed.py).

Sin red: el Embedder de verdad usa un opener falso y el índice, embedders falsos. Sin numpy instalado se prueba el
camino de Python puro; si numpy está, se prueba también ese camino.
"""

import io
import json
import struct
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from arthur.cerebro import index as index_mod
from arthur.cerebro.budget import Budget
from arthur.cerebro.chunks import Doc
from arthur.cerebro.embed import DIM, Embedder
from arthur.cerebro.index import Index
from arthur.notes.engines import EngineError, EngineRefused

CUENTA = "0123456789abcdef0123456789abcdef"
TOKEN = "TokEnSecReto_" + "x" * 27


def doc(ref: str, text: str, *, source="obsidian:cerebro", title="Nota", subject="Cálculo", links=()) -> Doc:
    return Doc(source=source, ref=ref, title=title, subject=subject, changed_at="2026-10-17T20:05:00+02:00",
               text=text, links=links)


def unit(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


class FakeEmbedder:
    """Da un vector por texto; a partir de `allow` textos dice que el presupuesto no da para más."""

    def __init__(self, allow: int = 10**9):
        self.allow, self.calls, self.texts = allow, [], []

    def embed(self, texts, *, kind="embed"):
        if len(self.texts) + len(texts) > self.allow:
            raise EngineError("Presupuesto del cerebro gastado por hoy")
        self.calls.append((list(texts), kind))
        self.texts.extend(texts)
        return [unit(len(self.texts) - len(texts) + k) for k in range(len(texts))]


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = Index(Path(self.tmp.name) / "cerebro" / "indice.sqlite3")

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def rows(self, sql, args=()):
        return self.index._q(sql, args)

    def test_sync_cuenta_y_guarda(self):
        r = self.index.sync([doc("a.md", "# A\nUno."), doc("b.md", "# B\nDos.", links=("A",))],
                            source="obsidian:cerebro", alive_refs=None)
        self.assertEqual(r, {"nuevos": 2, "cambiados": 0, "borrados": 0})
        self.assertEqual(self.index.stats()["trozos"], 2)
        self.assertEqual([tuple(x) for x in self.rows("SELECT target FROM links")], [("A",)])

    def test_solo_reindexa_lo_cambiado(self):
        docs = [doc("a.md", "# A\nUno."), doc("b.md", "# B\nDos.")]
        self.index.sync(docs, source="obsidian:cerebro", alive_refs=None)
        ids = [r["id"] for r in self.rows("SELECT id FROM chunks ORDER BY id")]
        self.assertEqual(self.index.sync(docs, source="obsidian:cerebro", alive_refs=None),
                         {"nuevos": 0, "cambiados": 0, "borrados": 0})
        # Los trozos no se han rehecho: mismos ids (y por tanto sus embeddings se conservan).
        self.assertEqual([r["id"] for r in self.rows("SELECT id FROM chunks ORDER BY id")], ids)
        r = self.index.sync([doc("a.md", "# A\nUno cambiado."), docs[1]], source="obsidian:cerebro", alive_refs=None)
        self.assertEqual(r, {"nuevos": 0, "cambiados": 1, "borrados": 0})
        self.assertEqual(len(self.index.chunks_fts("cambiado", 10, None)), 1)
        self.assertEqual(self.index.chunks_fts("uno", 10, None), self.index.chunks_fts("cambiado", 10, None))

    def test_cambia_titulo_sin_cambiar_texto(self):
        self.index.sync([doc("apunte:1", "Texto.", source="apuntes", title="Viejo")], source="apuntes",
                        alive_refs=None)
        self.index.sync([doc("apunte:1", "Texto.", source="apuntes", title="Nuevo")], source="apuntes",
                        alive_refs=None)
        (cid, _), = self.index.chunks_fts("texto", 5, None)
        self.assertEqual(self.index.chunk(cid)["title"], "Nuevo")

    def test_renombrar_no_duplica(self):
        # Review Focus 3: mover o renombrar una nota en Obsidian es, para el índice, una ref nueva con el mismo texto.
        self.index.sync([doc("Cálculo/vieja.md", "# Límites\nLa definición de límite.")],
                        source="obsidian:cerebro", alive_refs=None)
        fake = FakeEmbedder()
        self.index.embed_pending(fake)
        r = self.index.sync([doc("Tema 2/nueva.md", "# Límites\nLa definición de límite.", subject="Tema 2")],
                            source="obsidian:cerebro", alive_refs=None)
        self.assertEqual(r["nuevos"] + r["cambiados"], 1)
        self.assertEqual([tuple(x) for x in self.rows("SELECT ref, subject FROM docs")], [("Tema 2/nueva.md", "Tema 2")])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM chunks")[0][0], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM chunks_fts")[0][0], 1)
        (cid, _), = self.index.chunks_fts("definicion", 5, None)
        self.assertEqual(self.index.chunk(cid)["ref"], "Tema 2/nueva.md")
        # El embedding ya pagado se conserva: nada pendiente y ninguna llamada más.
        self.assertEqual(self.index.embed_pending(fake), 0)
        self.assertEqual(len(fake.calls), 1)

    def test_copia_no_roba_la_original(self):
        # Una copia con el mismo texto (las dos refs siguen vivas) son dos notas, no un renombrado.
        self.index.sync([doc("a.md", "Igual.")], source="obsidian:cerebro", alive_refs=None)
        r = self.index.sync([doc("a.md", "Igual."), doc("b.md", "Igual.")], source="obsidian:cerebro",
                            alive_refs=None)
        self.assertEqual(r, {"nuevos": 1, "cambiados": 0, "borrados": 0})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM docs")[0][0], 2)

    def test_borra_lo_desaparecido(self):
        self.index.sync([doc("apunte:1", "Uno.", source="apuntes"), doc("apunte:2", "Dos.", source="apuntes")],
                        source="apuntes", alive_refs=None)
        # Apuntes manda solo lo cambiado (vacío) y aparte la lista de los que siguen: el 2 ya no está.
        r = self.index.sync([], source="apuntes", alive_refs={"apunte:1"})
        self.assertEqual(r, {"nuevos": 0, "cambiados": 0, "borrados": 1})
        self.assertEqual(self.index.chunks_fts("dos", 5, None), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM chunks_fts")[0][0], 1)
        self.assertEqual([tuple(x) for x in self.rows("SELECT ref FROM docs")], [("apunte:1",)])

    def test_escaneo_completo_borra_lo_que_falta(self):
        self.index.sync([doc("a.md", "Uno."), doc("b.md", "Dos.")], source="obsidian:cerebro", alive_refs=None)
        r = self.index.sync([doc("a.md", "Uno.")], source="obsidian:cerebro", alive_refs=None)
        self.assertEqual(r["borrados"], 1)

    def test_escaneo_vacio_no_borra_todo(self):
        # Una bóveda que no está montada (Syncthing caído) da 0 notas: eso no es «Marc lo ha borrado todo».
        self.index.sync([doc("a.md", "Uno.")], source="obsidian:cerebro", alive_refs=None)
        with self.assertLogs("arthur.cerebro.index", "WARNING"):
            r = self.index.sync([], source="obsidian:cerebro", alive_refs=None)
        self.assertEqual(r["borrados"], 0)
        # Con alive_refs explícito y vacío sí se vacía (lo ha dicho la fuente, no es un silencio).
        self.assertEqual(self.index.sync([], source="obsidian:cerebro", alive_refs=set())["borrados"], 1)

    def test_fuentes_separadas(self):
        self.index.sync([doc("a.md", "Uno.")], source="obsidian:cerebro", alive_refs=None)
        self.index.sync([doc("a.md", "Uno.", source="obsidian:business-lab")], source="obsidian:business-lab",
                        alive_refs=None)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM docs")[0][0], 2)
        with self.assertRaises(ValueError):
            self.index.sync([doc("x.md", "x", source="pdf")], source="apuntes", alive_refs=None)

    def test_embed_pendientes(self):
        self.index.sync([doc(f"n{i}.md", f"Nota número {i}.") for i in range(7)], source="obsidian:cerebro",
                        alive_refs=None)
        fake = FakeEmbedder(allow=4)
        self.assertEqual(self.index.embed_pending(fake, limit=500), 0)  # lote de 7 > 4: el presupuesto dice que no
        self.assertEqual(self.index.stats()["pendientes"], 7)
        index_mod.BATCH, old = 3, index_mod.BATCH
        try:
            self.assertEqual(self.index.embed_pending(fake, limit=500), 3)  # el segundo lote ya no cabe
        finally:
            index_mod.BATCH = old
        self.assertEqual(self.index.stats()["pendientes"], 4)
        self.assertEqual(fake.calls[0][1], "embed")
        self.assertIn("Presupuesto", self.index.stats()["error_embeddings"])
        blobs = [r[0] for r in self.rows("SELECT embedding FROM chunks WHERE embedding IS NOT NULL")]
        self.assertEqual([len(b) for b in blobs], [2048] * 3)  # float16: 2 bytes × 1 024
        first = struct.unpack("<1024e", blobs[0])
        self.assertEqual(first[0], 1.0)
        # El texto que se manda lleva título y encabezado: dan contexto al significado del trozo.
        self.assertIn("Nota", fake.calls[0][0][0])
        self.assertEqual(self.index.embed_pending(FakeEmbedder(), limit=2), 2)
        self.assertEqual(self.index.stats()["pendientes"], 2)
        self.assertIsNone(self.index.stats()["error_embeddings"])

    def test_embedding_de_trozo_cambiado_no_se_guarda(self):
        # Si el trozo cambia mientras se calcula su embedding, el vector viejo no se pega al trozo nuevo.
        self.index.sync([doc("a.md", "Uno.")], source="obsidian:cerebro", alive_refs=None)
        index = self.index

        class Lento(FakeEmbedder):
            def embed(self, texts, *, kind="embed"):
                if not self.calls:  # solo la primera vez: la nota cambia una vez mientras se calcula
                    index.sync([doc("a.md", "Otro texto.")], source="obsidian:cerebro", alive_refs=None)
                return super().embed(texts, kind=kind)

        lento = Lento()
        # Se guarda solo el del trozo nuevo (siguiente lote), nunca el vector viejo en el trozo nuevo.
        self.assertEqual(self.index.embed_pending(lento), 1)
        self.assertEqual(lento.calls[1][0], ["Nota\nOtro texto."])
        self.assertEqual(self.index.stats()["pendientes"], 0)
        (cid, _), = self.index.chunks_fts("otro", 5, None)
        blob = self.rows("SELECT embedding FROM chunks WHERE id = ?", (cid,))[0][0]
        self.assertEqual(struct.unpack("<1024e", blob)[1], 1.0)  # el segundo vector, no el primero

    def test_fts_sin_tildes(self):
        self.index.sync([doc("apunte:3", "El límite de una sucesión convergente es único.", source="apuntes",
                             title="Límite de una sucesión")], source="apuntes", alive_refs=None)
        self.index.sync([doc("otra.md", "Receta de lentejas.")], source="obsidian:cerebro", alive_refs=None)
        hits = self.index.chunks_fts("limite sucesion", 10, None)
        self.assertEqual(len(hits), 1)
        cid, score = hits[0]
        self.assertGreater(score, 0)
        c = self.index.chunk(cid)
        self.assertEqual({k: c[k] for k in ("title", "source", "ref", "subject")},
                         {"title": "Límite de una sucesión", "source": "apuntes", "ref": "apunte:3",
                          "subject": "Cálculo"})
        self.assertEqual(set(c) >= {"id", "doc_id", "heading", "text"}, True)
        # Filtro por fuente: «obsidian» vale para las dos bóvedas.
        self.assertEqual(self.index.chunks_fts("limite", 10, ("obsidian",)), [])
        self.assertEqual(len(self.index.chunks_fts("lentejas", 10, ("obsidian",))), 1)
        self.assertEqual(len(self.index.chunks_fts("limite", 10, ("apuntes", "pdf"))), 1)

    def test_fts_consulta_rota_no_lanza(self):
        self.index.sync([doc("a.md", "Uno.")], source="obsidian:cerebro", alive_refs=None)
        self.assertEqual(self.index.chunks_fts('"sin cerrar', 5, None), [])
        self.assertEqual(self.index.count_fts('"sin cerrar'), 0)

    def test_cuenta_trozos_y_vocabulario(self):
        # Lo que usa search.words_support para saber lo rara que es una palabra y si es una falta de otra.
        self.assertEqual(self.index.chunk_count(), 0)
        self.assertEqual(self.index.vocabulary(3), [])
        self.index.sync([doc("a.md", "# A\nLa vuelta del bucle."), doc("b.md", "# B\nOtra vuelta, y el límite.")],
                        source="obsidian:cerebro", alive_refs=None)
        self.assertEqual(self.index.chunk_count(), 2)
        self.assertEqual(self.index.count_fts('"vuelta"'), 2)  # trozos, no apariciones
        self.assertEqual(self.index.count_fts('"buc"*'), 1)
        self.assertEqual(self.index.count_fts('"nada"'), 0)
        vocab = self.index.vocabulary(5)
        self.assertEqual(sorted(vocab), ["bucle", "limite", "vuelta"])  # sin tildes, como el índice
        self.index.sync([doc("a.md", "# A\nNueva palabra: transistor.")], source="obsidian:cerebro",
                        alive_refs=None)
        self.assertIn("transistor", self.index.vocabulary(5))  # sigue al índice sin cachés viejas
        self.assertNotIn("bucle", self.index.vocabulary(5))
        # Los títulos y las rutas no están en el índice de palabras: se piden aparte.
        self.index.sync([doc("Calculo/05 Taylor.md", "Texto.", title="05 Polinomio de Taylor")],
                        source="obsidian:cerebro", alive_refs=None)
        self.assertIn("05 Polinomio de Taylor Calculo/05 Taylor.md", self.index.doc_names())

    def test_vectors_y_nearest(self):
        self.index.sync([doc("a.md", "Uno."), doc("b.md", "Dos.")], source="obsidian:cerebro", alive_refs=None)
        self.index.sync([doc("c.pdf", "Tres.", source="pdf")], source="pdf", alive_refs=None)
        self.index.embed_pending(FakeEmbedder())
        ids, matrix = self.index.vectors()
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(matrix), 3)
        self.assertEqual(len(matrix[0]), DIM)
        near = self.index.nearest(unit(1), 2, None)
        self.assertEqual(near[0][0], ids[1])
        self.assertAlmostEqual(near[0][1], 1.0, places=3)
        self.assertEqual(len(near), 2)
        self.assertEqual([i for i, _ in self.index.nearest(unit(1), 5, ("pdf",))], [ids[2]])
        self.assertEqual(self.index.nearest([0.0] * DIM, 5, None), [])  # vector nulo: nada que comparar

    def test_persiste_y_estadisticas(self):
        self.index.sync([doc("a.md", "Uno.")], source="obsidian:cerebro", alive_refs=None)
        self.index.sync([doc("apunte:1", "Dos.", source="apuntes")], source="apuntes", alive_refs=None)
        self.index.close()
        self.index = Index(Path(self.tmp.name) / "cerebro" / "indice.sqlite3")
        s = self.index.stats()
        self.assertEqual((s["documentos"], s["trozos"], s["pendientes"]), (2, 2, 2))
        self.assertEqual(s["fuentes"]["apuntes"], {"documentos": 1, "trozos": 1, "pendientes": 1})
        self.assertEqual(self.rows("PRAGMA journal_mode")[0][0], "wal")


@unittest.skipIf(index_mod.np is None, "numpy no está instalado: solo se prueba el camino de Python puro")
class IndexSinNumpyTest(IndexTest):
    """Los mismos tests con numpy apagado a mano (cuando sí está instalado)."""

    def setUp(self):
        self._np = index_mod.np
        index_mod.np = None
        super().setUp()

    def tearDown(self):
        super().tearDown()
        index_mod.np = self._np


class Opener:
    def __init__(self, *results):
        self.results, self.requests = list(results), []

    def open(self, request, timeout=None):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return io.BytesIO(json.dumps(result).encode())


def ok(n):
    return {"success": True, "result": {"shape": [n, DIM], "data": [unit(i) for i in range(n)]}}


class EmbedderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.budget = Budget(Path(self.tmp.name) / "p.json", daily=3000,
                             now=lambda: datetime(2026, 10, 17, 12, tzinfo=timezone.utc))

    def tearDown(self):
        self.tmp.cleanup()

    def test_lotes_de_50(self):
        o = Opener(ok(50), ok(50), ok(20))
        e = Embedder(CUENTA, TOKEN, opener=o, budget=self.budget)
        vectors = e.embed([f"texto {i}" for i in range(120)])
        self.assertEqual(len(vectors), 120)
        self.assertEqual(len(o.requests), 3)
        req = o.requests[0]
        self.assertEqual(req.full_url,
                         f"https://api.cloudflare.com/client/v4/accounts/{CUENTA}/ai/run/@cf/baai/bge-m3")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data)["text"][:2], ["texto 0", "texto 1"])
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertGreater(self.budget.spent_today(), 0)
        self.assertNotIn(TOKEN, repr(e))

    def test_sin_presupuesto_no_llama(self):
        self.budget.spend("ask", 2999)
        o = Opener()
        with self.assertRaises(EngineError):
            Embedder(CUENTA, TOKEN, opener=o, budget=self.budget).embed(["hola"])
        self.assertEqual(o.requests, [])

    def test_pregunta_usa_su_parte(self):
        # Al 95 % del tope los embeddings del índice ya no caben (90 %), pero el de la pregunta de Marc sí (100 %).
        self.budget.spend("ask", 2850)
        e = Embedder(CUENTA, TOKEN, opener=Opener(ok(1)), budget=self.budget)
        with self.assertRaises(EngineError):
            e.embed(["x"])
        self.assertEqual(len(e.embed(["x"], kind="ask")), 1)

    def test_respuesta_rota(self):
        malo = {"result": {"data": [[0.1] * 10]}}
        for answer in (malo, {"result": {"data": [unit(0), unit(1)]}}, {"result": None},
                       {"result": {"data": [[float("nan")] + [0.0] * (DIM - 1)]}}):
            with self.assertRaises(EngineError):
                Embedder(CUENTA, TOKEN, opener=Opener(answer), budget=self.budget).embed(["x"])
        self.assertEqual(self.budget.spent_today(), 0)  # lo fallido no se apunta

    def test_errores_sin_token(self):
        err = urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b'{"errors":[{"code":10000}]}'))
        with self.assertRaises(EngineRefused) as cm:
            Embedder(CUENTA, TOKEN, opener=Opener(err), budget=self.budget).embed(["x"])
        self.assertNotIn(TOKEN, str(cm.exception))
        cuota = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b'{"errors":[{"code":3036}]}'))
        with self.assertRaises(EngineError):
            Embedder(CUENTA, TOKEN, opener=Opener(cuota), budget=self.budget).embed(["x"])

    def test_cuenta_o_token_invalidos(self):
        with self.assertRaises(EngineRefused):
            Embedder("../otra", TOKEN, budget=self.budget)
        with self.assertRaises(EngineRefused):
            Embedder(CUENTA, "con espacio y salto\n", budget=self.budget)


class ChunkKeyTest(unittest.TestCase):
    """La identidad estable de un trozo: lo que usa el Repaso para no perder (ni repetir) preguntas al reindexar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = Index(Path(self.tmp.name) / "indice.sqlite3")
        self.addCleanup(self.index.close)

    def test_misma_clave_aunque_cambie_el_numero(self):
        texto = "# A\nPrimer apartado.\n\n# B\nSegundo apartado."
        self.index.sync([doc("apunte:1", texto, source="apuntes")], source="apuntes", alive_refs={"apunte:1"})
        antes = dict(self.index.chunk_keys("apuntes"))
        self.index.sync([doc("apunte:1", texto + " Con una frase más.", source="apuntes")], source="apuntes",
                        alive_refs={"apunte:1"})
        despues = dict(self.index.chunk_keys("apuntes"))
        self.assertTrue(set(antes).isdisjoint(despues))  # ids nuevos (AUTOINCREMENT)
        # El apartado A no cambió: misma clave; el B sí
        self.assertEqual(len(set(antes.values()) & set(despues.values())), 1)
        for cid, key in despues.items():
            self.assertEqual(self.index.chunk(cid)["key"], key)

    def test_clave_ignora_espacios_y_separa_fuentes(self):
        self.assertEqual(index_mod.chunk_key("apuntes", "Hola  mundo\n"), index_mod.chunk_key("apuntes", "Hola mundo"))
        self.assertNotEqual(index_mod.chunk_key("apuntes", "Hola"), index_mod.chunk_key("pdf", "Hola"))
        self.assertNotEqual(index_mod.chunk_key("apuntes", "Hola"), index_mod.chunk_key("apuntes", "hola."))

    def test_chunk_keys_por_asignatura(self):
        self.index.sync([doc("apunte:1", "Uno.", source="apuntes"),
                         doc("apunte:2", "Dos.", source="apuntes", subject="Álgebra")],
                        source="apuntes", alive_refs={"apunte:1", "apunte:2"})
        self.assertEqual(len(self.index.chunk_keys("apuntes")), 2)
        [(cid, _)] = self.index.chunk_keys("apuntes", {"Álgebra"})
        self.assertEqual(self.index.chunk(cid)["text"], "Dos.")
        self.assertEqual(self.index.chunk_keys("apuntes", set()), [])

    def test_avisa_de_los_cambios(self):
        avisos = []
        self.index.on_change(avisos.append)
        self.index.on_change(lambda source: 1 / 0)  # un oyente que falla no tumba el reindexado
        d = doc("apunte:1", "Uno.", source="apuntes")
        with self.assertLogs("arthur.cerebro.index", level="ERROR"):
            self.index.sync([d], source="apuntes", alive_refs={"apunte:1"})
        self.index.sync([d], source="apuntes", alive_refs={"apunte:1"})  # nada cambia: nadie se entera
        self.index.sync([doc("apunte:1", "Uno.", source="apuntes", subject="Otra")], source="apuntes",
                        alive_refs={"apunte:1"})
        self.assertEqual(avisos, ["apuntes", "apuntes"])


if __name__ == "__main__":
    unittest.main()
