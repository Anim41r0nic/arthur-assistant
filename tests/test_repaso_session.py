import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from arthur.cerebro.repaso.session import accuracy, daily, free, streak
from arthur.cerebro.repaso.store import RepasoStore

MADRID = ZoneInfo("Europe/Madrid")
AHORA = datetime(2026, 10, 17, 20, 5, tzinfo=MADRID)


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RepasoStore.in_memory(now=lambda: AHORA)
        self.addCleanup(self.store.close)

    def nueva(self, asignatura: str = "Cálculo", carpeta: list[str] | None = None) -> int:
        return self.store.add_question({
            "chunk_id": 1, "asignatura": asignatura, "carpeta": carpeta or [asignatura, "Tema 1"],
            "tipo": "vf", "enunciado": "¿La serie armónica converge?", "respuesta": False,
            "explicacion": "Diverge, aunque muy despacio."})

    def atrasada(self, dias: int, asignatura: str = "Cálculo", carpeta: list[str] | None = None) -> int:
        """Contestada «Bien» hace `dias` días: vence a los 2, así que lleva `dias - 2` de retraso."""
        qid = self.nueva(asignatura, carpeta)
        self.store.answer(qid, 3, True, "libre", AHORA - timedelta(days=dias))
        return qid

    def test_daily_atrasadas_primero(self) -> None:
        ids = {d: self.atrasada(d) for d in range(3, 15)}
        for _ in range(4):
            self.nueva()
        sesion = daily(self.store, {"Cálculo"}, AHORA)
        self.assertEqual([q["id"] for q in sesion], [ids[d] for d in range(14, 4, -1)])
        self.assertFalse(any(q["nueva"] for q in sesion))

    def test_daily_max_5_nuevas(self) -> None:
        pendientes = [self.atrasada(5), self.atrasada(4)]
        for _ in range(20):
            self.nueva()
        sesion = daily(self.store, {"Cálculo"}, AHORA)
        self.assertEqual(len(sesion), 7)
        self.assertEqual([q["id"] for q in sesion[:2]], pendientes)
        self.assertTrue(all(q["nueva"] for q in sesion[2:]))

    def test_daily_no_cuenta_las_que_aun_no_tocan(self) -> None:
        self.atrasada(1)  # vence mañana
        self.assertEqual(daily(self.store, {"Cálculo"}, AHORA), [])

    def test_daily_solo_asignaturas_activas(self) -> None:
        mtp = [self.nueva("MTP"), self.atrasada(3, "MTP")]
        self.nueva("Cálculo")
        self.atrasada(3, "Cálculo")
        self.assertEqual(sorted(q["id"] for q in daily(self.store, {"MTP"}, AHORA)), sorted(mtp))
        self.assertEqual(daily(self.store, set(), AHORA + timedelta(days=1)), [])

    def test_retiradas_no_salen(self) -> None:
        mala_nueva, mala_vieja = self.nueva(), self.atrasada(3)
        buena = self.nueva()
        self.store.retire(mala_nueva, "dos respuestas valen")
        self.store.retire(mala_vieja, "no sale del apunte")
        self.assertEqual([q["id"] for q in daily(self.store, {"Cálculo"}, AHORA)], [buena])
        self.assertEqual([q["id"] for q in free(self.store, ["Cálculo"], AHORA)], [buena])

    def test_sesion_hecha(self) -> None:
        for _ in range(3):
            self.nueva()
        sesion = daily(self.store, {"Cálculo"}, AHORA)
        self.assertEqual(len(sesion), 3)
        self.store.answer(sesion[0]["id"], 3, True, "diaria", AHORA)
        # Recargar la página no cambia la sesión: salen las que faltan, en el mismo orden
        self.assertEqual([q["id"] for q in daily(self.store, {"Cálculo"}, AHORA)], [q["id"] for q in sesion[1:]])
        self.store.answer(sesion[1]["id"], 1, False, "diaria", AHORA)
        self.assertFalse(self.store.day(AHORA.date())["done"])
        self.store.answer(sesion[2]["id"], 3, True, "diaria", AHORA)
        # La fallada vuelve: hasta acertarla la sesión no está hecha
        self.assertFalse(self.store.day(AHORA.date())["done"])
        self.assertEqual([q["id"] for q in daily(self.store, {"Cálculo"}, AHORA)], [sesion[1]["id"]])
        self.store.answer(sesion[1]["id"], 3, True, "diaria", AHORA + timedelta(minutes=1))
        self.assertTrue(self.store.day(AHORA.date())["done"])
        self.assertEqual(daily(self.store, {"Cálculo"}, AHORA), [])

    def test_sesion_vacia_no_se_guarda(self) -> None:
        # Sin preguntas a las 8 no se fija una sesión vacía: las que lleguen después aún pueden salir hoy
        self.assertEqual(daily(self.store, {"Cálculo"}, AHORA), [])
        qid = self.nueva()
        self.assertEqual([q["id"] for q in daily(self.store, {"Cálculo"}, AHORA)], [qid])

    def test_racha(self) -> None:
        for d in (15, 16, 17):
            self.store.mark_day(date(2026, 10, d), done=True)
        self.assertEqual(streak(self.store, date(2026, 10, 17)), 3)
        # Hoy aún no está hecha: la racha sigue viva hasta que acabe el día
        self.assertEqual(streak(self.store, date(2026, 10, 18)), 3)
        self.assertEqual(streak(self.store, date(2026, 10, 19)), 0)
        self.store.mark_day(date(2026, 10, 16), done=False)
        self.assertEqual(streak(self.store, date(2026, 10, 17)), 1)

    def test_racha_cambio_de_hora(self) -> None:
        # El 25 de octubre de 2026 tiene 25 horas (a las 03:00 vuelve a ser las 02:00). Las 23:30 del 25 y las
        # 00:10 del 26 caen las dos en el 25 en UTC (22:30 y 23:10): si el día se contara en UTC, el 26 se
        # quedaría sin hacer y la racha se rompería. El día que cuenta es el de Madrid.
        momentos = (datetime(2026, 10, 24, 20, 0, tzinfo=MADRID), datetime(2026, 10, 25, 23, 30, tzinfo=MADRID),
                    datetime(2026, 10, 26, 0, 10, tzinfo=MADRID))
        self.assertEqual(momentos[1].astimezone(ZoneInfo("UTC")).date(), momentos[2].astimezone(ZoneInfo("UTC")).date())
        for momento in momentos:
            self.nueva()
            sesion = daily(self.store, {"Cálculo"}, momento)
            self.assertTrue(sesion, momento)
            for q in sesion:
                self.store.answer(q["id"], 3, True, "diaria", momento)
            self.assertTrue(self.store.day(momento.date())["done"], momento)
        self.assertEqual(streak(self.store, date(2026, 10, 26)), 3)

    def test_retirar_la_ultima_completa_el_dia(self) -> None:
        # «Esta pregunta está mal» con la última que faltaba: la sesión está acabada y la racha no se rompe
        for _ in range(2):
            self.nueva()
        primera, ultima = (q["id"] for q in daily(self.store, {"Cálculo"}, AHORA))
        self.store.answer(primera, 3, True, "diaria", AHORA)
        self.assertFalse(self.store.day(AHORA.date())["done"])
        self.store.retire(ultima, "dos respuestas valen")
        self.assertTrue(self.store.day(AHORA.date())["done"])
        self.assertEqual(streak(self.store, AHORA.date()), 1)

    def test_retirar_la_sesion_entera_completa_el_dia(self) -> None:
        for _ in range(2):
            self.nueva()
        for q in daily(self.store, {"Cálculo"}, AHORA):
            self.store.retire(q["id"], "mal")
        self.assertTrue(self.store.day(AHORA.date())["done"])
        self.assertEqual(daily(self.store, {"Cálculo"}, AHORA), [])

    def test_retirar_sin_sesion_no_marca_nada(self) -> None:
        self.store.retire(self.nueva(), "mal")
        self.assertFalse(self.store.day(AHORA.date())["done"])

    def test_sesion_que_cruza_medianoche(self) -> None:
        # Empieza a las 23:55 y acaba a las 00:05: lo de después de medianoche es de la sesión de ayer
        noche = datetime(2026, 10, 17, 23, 55, tzinfo=MADRID)
        madrugada = datetime(2026, 10, 18, 0, 5, tzinfo=MADRID)
        for _ in range(2):
            self.nueva()
        primera, segunda = (q["id"] for q in daily(self.store, {"Cálculo"}, noche))
        self.store.answer(primera, 3, True, "diaria", noche)
        self.store.answer(segunda, 3, True, "diaria", madrugada)
        self.assertEqual([a["mode"] for a in self.store.answers_since(noche)], ["diaria", "diaria"])
        self.assertTrue(self.store.day(noche.date())["done"])
        self.assertFalse(self.store.day(madrugada.date())["done"])
        self.assertEqual(streak(self.store, madrugada.date()), 1)

    def test_retirar_tras_medianoche_completa_ayer(self) -> None:
        noche = datetime(2026, 10, 17, 23, 55, tzinfo=MADRID)
        madrugada = datetime(2026, 10, 18, 0, 5, tzinfo=MADRID)
        store = RepasoStore.in_memory(now=lambda: madrugada)
        self.addCleanup(store.close)
        ids = [store.add_question({"chunk_id": 1, "asignatura": "Cálculo", "tipo": "vf", "enunciado": "¿Sí?",
                                   "respuesta": True}) for _ in range(2)]
        store.save_session(noche.date(), ids)
        store.answer(ids[0], 3, True, "diaria", noche)
        store.retire(ids[1], "mal")
        self.assertTrue(store.day(noche.date())["done"])

    def test_sesion_de_hoy_va_antes_que_la_de_ayer(self) -> None:
        # Si tras medianoche ya se abrió la sesión de hoy y la pregunta está en ella, cuenta para hoy
        noche = datetime(2026, 10, 17, 23, 55, tzinfo=MADRID)
        madrugada = datetime(2026, 10, 18, 0, 5, tzinfo=MADRID)
        qid = self.nueva()
        self.store.save_session(noche.date(), [qid, self.nueva()])
        self.store.save_session(madrugada.date(), [qid])
        self.store.answer(qid, 3, True, "diaria", madrugada)
        self.assertTrue(self.store.day(madrugada.date())["done"])
        self.assertFalse(self.store.day(noche.date())["done"])

    def test_dia_sin_preguntas_rompe_la_racha(self) -> None:
        # Decisión de Marc (2026-10-18): un día sin sesión hecha rompe la racha, aunque no hubiera preguntas
        self.store.mark_day(date(2026, 10, 15), done=True)
        self.assertEqual(daily(self.store, {"Cálculo"}, datetime(2026, 10, 16, 20, 0, tzinfo=MADRID)), [])
        self.store.mark_day(date(2026, 10, 17), done=True)
        self.assertEqual(streak(self.store, date(2026, 10, 17)), 1)

    def test_fallada_vuelve_al_final(self) -> None:
        a, b, c = (self.nueva() for _ in range(3))
        self.assertEqual([q["id"] for q in daily(self.store, {"Cálculo"}, AHORA)], [a, b, c])
        self.assertTrue(self.store.record_answer(a, 1, False, "diaria", AHORA)["otra_vez"])
        self.assertEqual([q["id"] for q in daily(self.store, {"Cálculo"}, AHORA)], [b, c, a])
        self.store.answer(b, 3, True, "diaria", AHORA)
        self.store.answer(c, 3, True, "diaria", AHORA)
        self.assertFalse(self.store.day(AHORA.date())["done"])  # falta acertar la fallada
        # El reintento es un repaso más del mismo día: mueve la tarjeta otra vez
        antes = self.store.card(a)
        r = self.store.record_answer(a, 3, True, "diaria", AHORA + timedelta(minutes=2))
        self.assertEqual((r["mode"], r["otra_vez"], r["card"].reps), ("diaria", False, antes.reps + 1))
        self.assertTrue(self.store.day(AHORA.date())["done"])
        self.assertEqual(daily(self.store, {"Cálculo"}, AHORA), [])

    def test_tres_fallos_no_atascan(self) -> None:
        a, b = self.nueva(), self.nueva()
        daily(self.store, {"Cálculo"}, AHORA)
        self.store.answer(b, 3, True, "diaria", AHORA)
        for i in range(3):
            self.assertFalse(self.store.day(AHORA.date())["done"], i)
            r = self.store.record_answer(a, 1, False, "diaria", AHORA + timedelta(minutes=i))
        self.assertFalse(r["otra_vez"])
        self.assertEqual(self.store.card(a).reps, 3)
        self.assertTrue(self.store.day(AHORA.date())["done"])
        self.assertEqual(daily(self.store, {"Cálculo"}, AHORA), [])
        # Un cuarto envío ya no cuenta para la sesión
        self.assertTrue(self.store.record_answer(a, 1, False, "diaria", AHORA + timedelta(minutes=9))["repetida"])

    def test_libre_cuenta_para_fsrs(self) -> None:
        qid = self.nueva()
        self.assertEqual([q["id"] for q in daily(self.store, {"Cálculo"}, AHORA)], [qid])
        antes = self.store.card(qid)
        self.store.answer(qid, 3, True, "libre", AHORA)
        self.assertNotEqual(self.store.card(qid), antes)
        self.assertFalse(self.store.day(AHORA.date())["done"])

    def test_libre_por_carpeta_y_urgencia(self) -> None:
        tema2 = ["Cálculo", "Tema 2"]
        al_dia = self.atrasada(1, carpeta=tema2)
        nueva = self.nueva(carpeta=tema2)
        muy_atrasada = self.atrasada(9, carpeta=tema2 + ["Series"])
        atrasada = self.atrasada(4, carpeta=tema2)
        self.nueva(carpeta=["Cálculo", "Tema 1"])
        self.nueva(carpeta=["Cálculo", "Tema 20"])  # empieza igual pero es otra carpeta
        ids = [q["id"] for q in free(self.store, tema2, AHORA)]
        self.assertEqual(ids, [muy_atrasada, atrasada, nueva, al_dia])
        self.assertEqual(len(free(self.store, ["Cálculo"], AHORA)), 6)

    def test_aciertos_por_asignatura(self) -> None:
        # Solo cuenta la sesión diaria (una respuesta por pregunta y día): repetir en libre no infla el %
        calc, mtp = self.nueva("Cálculo"), self.nueva("MTP")
        otras = [self.nueva("Cálculo") for _ in range(3)]
        ayer = AHORA - timedelta(days=1)
        self.store.save_session(ayer.date(), [calc, *otras])
        for qid, acierto in zip([calc, *otras], (True, True, True, False)):
            self.store.answer(qid, 3 if acierto else 1, acierto, "diaria", ayer)
        for _ in range(5):
            self.store.answer(calc, 1, False, "libre", ayer)  # repasos libres: no cuentan
        self.store.save_session(AHORA.date(), [mtp])
        self.store.answer(mtp, 1, False, "diaria", AHORA)
        self.assertEqual(accuracy(self.store, AHORA), {"Cálculo": 0.75, "MTP": 0.0})
        self.assertEqual(accuracy(self.store, AHORA + timedelta(days=30), days=7), {})

if __name__ == "__main__":
    unittest.main()
