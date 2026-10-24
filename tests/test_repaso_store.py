import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.cerebro.repaso import store as store_mod
from arthur.cerebro.repaso.store import RepasoError, RepasoStore

MADRID = ZoneInfo("Europe/Madrid")
AHORA = datetime(2026, 10, 17, 20, 5, 30, 123456, tzinfo=MADRID)


def pregunta(**cambios) -> dict:
    q = {"chunk_id": 7, "asignatura": "Cálculo", "carpeta": ["Cálculo", "Tema 2"], "tipo": "test",
         "enunciado": "¿Cuánto vale el límite de 1/n?", "opciones": ["0", "1", "∞", "No existe"],
         "respuesta": "0", "explicacion": "1/n se hace tan pequeño como se quiera."}
    q.update(cambios)
    return q


class RepasoStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RepasoStore.in_memory(now=lambda: AHORA)
        self.addCleanup(self.store.close)

    def test_guarda_y_lee_pregunta(self) -> None:
        qid = self.store.add_question(pregunta())
        q = self.store.question(qid)
        self.assertEqual(q["id"], qid)
        self.assertEqual(q["carpeta"], ["Cálculo", "Tema 2"])
        self.assertEqual(q["opciones"], ["0", "1", "∞", "No existe"])
        self.assertEqual(q["created_at"], "2026-10-17T20:05:30+02:00")
        self.assertIsNone(q["retired_at"])
        self.assertTrue(q["nueva"])
        self.assertEqual(q["due"], AHORA)

    def test_pregunta_mal_formada_falla(self) -> None:
        for mala in (pregunta(tipo="dibujo"), pregunta(enunciado="  "), pregunta(asignatura=""),
                     pregunta(chunk_id="siete")):
            with self.assertRaises(RepasoError):
                self.store.add_question(mala)
        # Sin carpeta se usa la asignatura
        sin = pregunta()
        del sin["carpeta"]
        self.assertEqual(self.store.question(self.store.add_question(sin))["carpeta"], ["Cálculo"])

    def test_pregunta_que_no_existe(self) -> None:
        with self.assertRaises(KeyError):
            self.store.question(99)
        with self.assertRaises(KeyError):
            self.store.answer(99, 3, True, "diaria", AHORA)
        with self.assertRaises(KeyError):
            self.store.retire(99, "x")

    def test_retirar_guarda_motivo(self) -> None:
        qid = self.store.add_question(pregunta())
        self.store.retire(qid, "  La respuesta   es 1  ")
        q = self.store.question(qid)
        self.assertEqual(q["motivo"], "La respuesta es 1")
        self.assertEqual(q["retired_at"], "2026-10-17T20:05:30+02:00")
        self.store.retire(qid, "x" * 400)
        self.assertEqual(len(self.store.question(qid)["motivo"]), 300)
        with self.assertRaises(RepasoError):
            self.store.answer(qid, 3, True, "libre", AHORA)

    def test_respuesta_mueve_la_tarjeta_y_se_apunta(self) -> None:
        qid = self.store.add_question(pregunta())
        card = self.store.answer(qid, 3, True, "libre", AHORA)
        self.assertEqual(card.reps, 1)
        self.assertEqual(self.store.card(qid), card)
        self.assertFalse(self.store.question(qid)["nueva"])
        respuestas = self.store.answers_since(AHORA - timedelta(minutes=1))
        self.assertEqual(respuestas, [{"id": 1, "question_id": qid, "answered_at": "2026-10-17T20:05:30+02:00",
                                       "day": "2026-10-17", "rating": 3, "correct": True, "mode": "libre", "asignatura": "Cálculo"}])
        self.assertEqual(self.store.answers_since(AHORA + timedelta(minutes=1)), [])

    def test_respuesta_valida_nota_y_modo(self) -> None:
        qid = self.store.add_question(pregunta())
        with self.assertRaises(RepasoError):
            self.store.answer(qid, 5, True, "diaria", AHORA)
        with self.assertRaises(RepasoError):
            self.store.answer(qid, 3, True, "examen", AHORA)

    def test_diaria_fuera_de_sesion_es_libre(self) -> None:
        # El modo lo decide el almacén: una pregunta que no está en la sesión de hoy no cuenta para ella
        qid = self.store.add_question(pregunta())
        self.store.answer(qid, 3, True, "diaria", AHORA)
        self.assertEqual([a["mode"] for a in self.store.answers_since(AHORA - timedelta(minutes=1))], ["libre"])

    def test_doble_envio_no_cuenta_dos_veces(self) -> None:
        # El mismo «Comprobar» enviado dos veces (doble clic, reintento del navegador): la segunda no mueve
        # la tarjeta ni apunta otra fila, y devuelve la tarjeta tal como quedó con la primera
        qid, otra = self.store.add_question(pregunta()), self.store.add_question(pregunta())
        self.store.save_session(AHORA.date(), [qid, otra])
        primera = self.store.answer(qid, 3, True, "diaria", AHORA)
        segunda = self.store.answer(qid, 3, True, "diaria", AHORA + timedelta(seconds=1))
        self.assertEqual(segunda, primera)
        self.assertEqual(self.store.card(qid).reps, 1)
        self.assertEqual([a["mode"] for a in self.store.answers_since(AHORA - timedelta(minutes=1))], ["diaria"])
        # Un fallo enviado dos veces seguidas es un solo fallo; volver a intentarlo más tarde sí cuenta
        r = self.store.record_answer(otra, 1, False, "diaria", AHORA)
        self.assertTrue(r["otra_vez"])
        self.assertTrue(self.store.record_answer(otra, 1, False, "diaria", AHORA + timedelta(seconds=1))["repetida"])
        self.assertEqual(self.store.card(otra).reps, 1)
        r = self.store.record_answer(otra, 3, True, "diaria", AHORA + timedelta(minutes=1))
        self.assertEqual((r["repetida"], r["card"].reps), (False, 2))
        self.assertTrue(self.store.day(AHORA.date())["done"])
        # En modo libre sí se repasa otra vez
        self.assertEqual(self.store.answer(qid, 3, True, "libre", AHORA + timedelta(hours=1)).reps, 2)

    def test_respuesta_a_medias_no_se_guarda(self) -> None:
        # Tarjeta, respuesta y día van en una sola transacción: si algo falla a mitad, no queda nada a medias
        qid = self.store.add_question(pregunta())
        self.store.save_session(AHORA.date(), [qid])
        antes = self.store.card(qid)

        def roto(*a, **k):
            raise RuntimeError("disco lleno")
        self.store._maybe_complete = roto
        with self.assertRaises(RuntimeError):
            self.store.answer(qid, 3, True, "diaria", AHORA)
        self.assertEqual(self.store.card(qid), antes)
        self.assertEqual(self.store.answers_since(AHORA - timedelta(days=1)), [])
        # Y el almacén sigue funcionando
        del self.store._maybe_complete
        self.assertEqual(self.store.answer(qid, 3, True, "diaria", AHORA).reps, 1)

    def test_pregunta_sin_tarjeta_no_se_guarda(self) -> None:
        def roto(*a, **k):
            raise RuntimeError("disco lleno")
        self.store._save_card = roto
        with self.assertRaises(RuntimeError):
            self.store.add_question(pregunta())
        del self.store._save_card
        # Mirando la tabla de preguntas directamente: no queda una pregunta huérfana sin tarjeta
        self.assertEqual(self.store.chunks_with_questions(), set())
        self.store.add_question(pregunta())
        self.assertEqual(len(self.store.active_questions()), 1)

    def test_answers_since_con_zonas_distintas(self) -> None:
        # Se compara el momento real, no el texto: 19:00 en UTC es más tarde que 20:30 en Madrid (18:30 UTC)
        qid = self.store.add_question(pregunta())
        self.store.answer(qid, 3, True, "libre", datetime(2026, 10, 17, 20, 30, tzinfo=MADRID))
        self.assertEqual(self.store.answers_since(datetime(2026, 10, 17, 19, 0, tzinfo=ZoneInfo("UTC"))), [])

    def test_dias(self) -> None:
        d = date(2026, 10, 17)
        self.assertEqual(self.store.day(d), {"day": "2026-10-17", "done": False, "notified": False})
        self.store.mark_day(d, notified=True)
        self.store.mark_day(d, done=True)
        self.assertEqual(self.store.day(d), {"day": "2026-10-17", "done": True, "notified": True})

    def test_trozos_con_preguntas(self) -> None:
        self.store.add_question(pregunta(chunk_id=3))
        qid = self.store.add_question(pregunta(chunk_id=5))
        self.store.retire(qid, "mal")
        # Las retiradas cuentan: si no, cada noche se volvería a preguntar por el mismo trozo
        self.assertEqual(self.store.chunks_with_questions(), {3, 5})

    def test_clave_del_trozo(self) -> None:
        qid = self.store.add_question(pregunta(chunk_key="k" * 64))
        self.assertEqual(self.store.question(qid)["chunk_key"], "k" * 64)
        self.assertIsNone(self.store.question(self.store.add_question(pregunta()))["chunk_key"])
        with self.assertRaises(RepasoError):
            self.store.add_question(pregunta(chunk_key=7))

    def test_sigue_a_su_trozo(self) -> None:
        sigue = self.store.add_question(pregunta(chunk_id=3, chunk_key="a"))
        cambia = self.store.add_question(pregunta(chunk_id=4, chunk_key="b"))
        vieja = self.store.add_question(pregunta(chunk_id=5))         # de antes de las claves, su trozo sigue
        perdida = self.store.add_question(pregunta(chunk_id=6))       # de antes de las claves, su trozo no está
        mala = self.store.add_question(pregunta(chunk_id=8, chunk_key="c"))
        self.store.retire(mala, "mal")
        result = self.store.follow_chunks({"a": (30, "Cálculo"), "e": (5, "Cálculo"), "c": (80, "Cálculo")},
                                          "el apunte cambió")
        self.assertEqual(result, {"reenlazadas": 3, "retiradas": 2, "recuperadas": 0})
        self.assertEqual(self.store.question(sigue)["chunk_id"], 30)
        self.assertEqual(self.store.question(vieja)["chunk_key"], "e")
        self.assertEqual(self.store.question(cambia)["motivo"], "el apunte cambió")
        self.assertEqual(self.store.question(perdida)["motivo"], "el apunte cambió")
        # Una que Marc retiró por mala sigue retirada aunque su trozo exista
        self.assertEqual(self.store.question(mala)["motivo"], "mal")
        self.assertEqual(self.store.keys_with_questions(), {"a", "b", "c", "e"})
        # Si vuelve el texto, vuelve la pregunta (solo las que se retiraron por el cambio)
        result = self.store.follow_chunks({"b": (40, "Cálculo")}, "el apunte cambió")
        self.assertEqual(result["recuperadas"], 1)
        self.assertIsNone(self.store.question(cambia)["retired_at"])
        self.assertEqual(self.store.question(cambia)["chunk_id"], 40)
        # Sin ningún trozo (índice vacío o sin leer todavía) no se retira nada
        self.assertEqual(self.store.follow_chunks({}, "x"), {"reenlazadas": 0, "retiradas": 0, "recuperadas": 0})
        self.assertIsNone(self.store.question(cambia)["retired_at"])

    def test_fallos_por_clave(self) -> None:
        for _ in range(3):
            self.store.note_failed_key("x")
        self.store.note_failed_key("y")
        self.assertEqual(self.store.failed_keys(3), {"x"})

    def test_migra_preguntas_de_la_version_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "repaso.sqlite3"
            store = RepasoStore(ruta, now=lambda: AHORA)
            qid = store.add_question(pregunta())
            store.close()
            # Se deja el fichero como lo dejaba la versión 1 (sin la columna nueva ni la tabla de fallos por clave)
            db = sqlite3.connect(ruta)
            db.executescript("DROP INDEX questions_key; ALTER TABLE questions DROP COLUMN chunk_key; "
                             "DROP TABLE failed_keys; PRAGMA user_version = 1;")
            db.close()
            store = RepasoStore(ruta, now=lambda: AHORA)
            self.addCleanup(store.close)
            q = store.question(qid)
            self.assertEqual((q["chunk_id"], q["chunk_key"]), (7, None))
            self.assertEqual(q["enunciado"], "¿Cuánto vale el límite de 1/n?")
            store.note_failed_key("x")
            self.assertEqual(store._q("PRAGMA user_version")[0][0], store_mod.SCHEMA_VERSION)


    def test_persiste_en_disco(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "cerebro" / "repaso.sqlite3"
            store = RepasoStore(ruta, now=lambda: AHORA)
            qid = store.add_question(pregunta())
            store.answer(qid, 4, True, "libre", AHORA)
            store.close()
            otra = RepasoStore(ruta, now=lambda: AHORA)
            self.assertEqual(otra.card(qid).reps, 1)
            self.assertEqual(otra.card(qid).due.tzinfo, MADRID)
            otra.close()
            self.assertEqual(ruta.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
