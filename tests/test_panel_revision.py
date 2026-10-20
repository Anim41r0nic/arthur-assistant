"""La revisión nocturna y semanal (arthur/panel/revision.py): foto del plan, cuentas, textos y la ejecución.

Todo con falsos: reloj, motores de IA, aviso al móvil y el día del panel. Nada de red ni escritorio.
"""

import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from arthur.panel import revision
from arthur.panel.store import PanelError, PanelStore

MAD = ZoneInfo("Europe/Madrid")
SABADO = date(2026, 10, 24)


def at(d: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(d, time(h, m), tzinfo=MAD)


def evento(d: date, h: int, titulo: str, m: int = 0, dur: int = 50) -> dict:
    start = at(d, h, m)
    return {"title": titulo, "start": start.isoformat(), "end": (start + timedelta(minutes=dur)).isoformat(),
            "all_day": False}


class Reloj:
    def __init__(self, momento):
        self.momento = momento

    def __call__(self):
        return self.momento


class Motor:
    def __init__(self, name, reply=None, error=None):
        self.name, self.reply, self.error, self.prompts = name, reply, error, []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply


class Aviso:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, text, *, destination=None):
        self.sent.append((text, destination))
        return self.ok


class Base(unittest.TestCase):
    def setUp(self):
        self.reloj = Reloj(at(SABADO, 8, 30))
        self.store = PanelStore.in_memory(now=self.reloj)
        self.addCleanup(self.store.close)
        self.events = []
        self.obsidian = []
        self.repaso = None
        self.motores = []
        self.aviso = Aviso()
        self.rev = revision.Revision(self.store, self.today, now=self.reloj, engines=lambda: self.motores,
                                     notifier=self.aviso)

    def today(self):
        d = self.reloj().date()
        day = {"date": d.isoformat(), "events": [dict(e) for e in self.events],
               "tasks": [dict(t) for t in self.obsidian],
               "repaso": dict(self.repaso) if self.repaso else None}
        return self.rev.decorate(day)

    def caja(self, texto, done=False, fichero="Tareas.md", linea=1):
        return {"id": f"obsidian-{fichero}:{linea}", "title": texto, "source": "obsidian", "done": done,
                "file": fichero, "line": linea}

    def hacer(self, task, h, m=0):
        antes = self.reloj.momento
        self.reloj.momento = at(self.reloj().date(), h, m)
        self.store.toggle(task.id)
        self.reloj.momento = antes


class UtilidadesTests(unittest.TestCase):
    def test_bloques_de_estudio_sin_mirar_tildes_ni_mayusculas(self):
        self.assertTrue(revision.is_study("Estudio: MTP"))
        self.assertTrue(revision.is_study("ESTÚDIO cálculo"))
        self.assertFalse(revision.is_study("Clase de MTP"))
        self.assertFalse(revision.is_study("Estudio", prefix=" "))
        self.assertEqual(revision.subject_of("Estudio: MTP"), "MTP")
        self.assertEqual(revision.subject_of("Estudio"), "Sin asignatura")

    def test_porcentajes_con_enteros(self):
        self.assertIsNone(revision.percent(0, 0))
        self.assertEqual(revision.percent(4, 6), 67)
        self.assertEqual(revision.percent(1, 8), 13)   # 12,5 → 13: los medios hacia arriba
        self.assertEqual(revision.medios(5), "2,5")
        self.assertEqual(revision.medios(4), "2")

    def test_limpieza_del_texto_de_la_ia(self):
        self.assertIsNone(revision.clean_ai("Has hecho 4 tareas."))   # cifras: no vale
        self.assertIsNone(revision.clean_ai("   "))
        self.assertEqual(revision.clean_ai("**Buen día.** Sigue así."), "Buen día. Sigue así.")
        largo = " ".join(["palabra"] * 30) + ". " + " ".join(["otra"] * 40) + "."
        corto = revision.clean_ai(largo)
        self.assertLessEqual(len(corto.split()), revision.MAX_PALABRAS)
        self.assertTrue(corto.endswith("."))
        self.assertTrue(revision.clean_ai(" ".join(["sin"] * 80)).endswith("…"))

    def test_titulos(self):
        self.assertEqual(revision.day_title(SABADO), "Sábado 24 de octubre")
        self.assertEqual(revision.week_title(date(2026, 10, 19)), "Semana del 19 al 25 de octubre")
        self.assertEqual(revision.week_title(date(2026, 10, 26)), "Semana del 26 de octubre al 1 de noviembre")


class FotoYCuentasTests(Base):
    def test_foto_a_primera_hora_y_lo_de_despues_es_extra(self):
        atrasada = self.store.add_task("Ejercicios de límites", SABADO - timedelta(1))
        hoy = self.store.add_task("Subir corto a TikTok", SABADO, source="studio")
        self.store.add_task("sin fecha", None)
        self.events = [evento(SABADO, 10, "Estudio: Álgebra"), evento(SABADO, 11, "Estudio: Cálculo"),
                       evento(SABADO, 9, "Clase de MTP")]
        self.obsidian = [self.caja("Marc: llamar al banco"), self.caja("Marc: hecha ya", done=True, linea=2)]
        self.reloj.momento = at(SABADO, 5, 0)
        self.today()
        self.assertIsNone(self.store.photo(SABADO), "antes de las 08:00 no hay foto")
        self.reloj.momento = at(SABADO, 8, 30)
        self.today()
        foto = self.store.photo(SABADO)
        self.assertEqual([t["id"] for t in foto["tareas"]], [hoy.id])
        self.assertEqual([b["titulo"] for b in foto["bloques"]], ["Estudio: Álgebra", "Estudio: Cálculo"])
        self.assertEqual([o["titulo"] for o in foto["obsidian"]], ["Marc: llamar al banco"])

        nueva = self.store.add_task("añadida a mediodía", SABADO)
        self.hacer(atrasada, 9)
        self.hacer(nueva, 13)
        self.obsidian[0]["done"] = True
        bloque = self.store.photo(SABADO)["bloques"][0]["clave"]
        self.rev.mark(SABADO.isoformat(), bloque, "hecho")
        self.reloj.momento = at(SABADO, 21, 30)
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertEqual((n["tareas"]["hechas"], n["tareas"]["total"], n["tareas"]["porcentaje"]), (0, 1, 0))
        self.assertEqual(sorted(x["titulo"] for x in n["extra"]["lista"]),
                         ["Ejercicios de límites", "Marc: llamar al banco", "añadida a mediodía"])
        self.assertEqual(n["tareas"]["pendientes_lista"][0]["titulo"], "Subir corto a TikTok")
        e = n["estudio"]
        self.assertEqual((e["total"], e["hecho"], e["sin_marcar"], e["porcentaje"]), (2, 1, 1, 50))
        self.assertEqual(n["plan"], {"total": 3, "hechas": 1})
        self.assertTrue(n["activo"])
        self.assertFalse(n["completo"])
        self.assertEqual(n["puntos"]["total"], 10)   # 1 bloque; los extra no dan puntos
        self.assertEqual(n["hasta"], "21:30")

    def test_foto_tardia_no_castiga_lo_ya_hecho(self):
        t = self.store.add_task("de hoy", SABADO)
        self.hacer(t, 8)
        self.reloj.momento = at(SABADO, 12, 0)   # el servidor arrancó a mediodía
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertEqual((n["tareas"]["hechas"], n["tareas"]["total"]), (1, 1))
        self.assertEqual(n["extra"]["total"], 0)

    def test_foto_anterior_con_atrasada_no_la_cuenta_otra_vez(self):
        vencida = self.store.add_task("Atrasada", SABADO - timedelta(2))
        self.store.save_photo(SABADO, {"tareas": [{"id": vencida.id, "titulo": vencida.title,
                                                   "fuente": "arthur", "due": vencida.due}],
                                       "bloques": [], "obsidian": []})
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertEqual((n["tareas"]["total"], n["tareas"]["porcentaje"]), (0, None))
        self.hacer(vencida, 12)
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertEqual((n["tareas"]["total"], n["extra"]["total"]), (0, 1))

    def test_bloques_sin_marcar_no_son_fallo(self):
        self.events = [evento(SABADO, 10, "Estudio: MTP")]
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertIsNone(n["activo"])
        clave = n["estudio"]["bloques"][0]["clave"]
        self.rev.mark(SABADO.isoformat(), clave, "no")
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertIs(n["activo"], False)
        self.rev.mark(SABADO.isoformat(), clave, "medio")
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertTrue(n["activo"])                    # ½ hace el día activo…
        self.assertEqual(n["puntos"]["total"], 0)       # …pero no da puntos
        self.assertEqual(n["estudio"]["porcentaje"], 50)

    def test_repaso_y_dia_completo(self):
        t = self.store.add_task("de hoy", SABADO)
        self.repaso = {"racha": 3, "pendientes": 0, "hecha": True, "hoy": {"preguntas": 10, "aciertos": 7}}
        self.today()
        self.hacer(t, 10)
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertTrue(n["completo"])
        self.assertEqual(n["repaso"], {"hecha": True, "preguntas": 10, "aciertos": 7})
        self.assertEqual(n["puntos"], {"repaso": 50, "tareas": 10, "bono": 10, "total": 70})

    def test_repaso_sin_hacer_impide_dia_completo_pero_desconocido_no(self):
        t = self.store.add_task("de hoy", SABADO)
        self.hacer(t, 10)
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertTrue(n["completo"])          # el cerebro no contesta: no se sabe, no se exige
        self.repaso = {"racha": 0, "pendientes": 10, "hecha": False}
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertFalse(n["completo"])

    def test_dias_pasados_del_repaso_que_el_panel_no_vio(self):
        self.repaso = {"racha": 2, "pendientes": 0, "hecha": False,
                       "dias": {"2026-10-23": {"hecha": True, "preguntas": 10, "aciertos": 9}}}
        self.today()
        viernes = self.store.day(date(2026, 10, 23))
        self.assertEqual(viernes["repaso"]["aciertos"], 9)
        self.rev.close_days()
        self.assertTrue(self.store.day(date(2026, 10, 23))["datos"]["activo"])   # activo sin foto: el Repaso lo dice

    def test_marcas_solo_de_hoy_o_ayer_y_de_bloques_conocidos(self):
        self.events = [evento(SABADO, 10, "Estudio: MTP")]
        self.today()
        clave = self.store.photo(SABADO)["bloques"][0]["clave"]
        with self.assertRaises(PanelError):
            self.rev.mark("2026-10-20", clave, "hecho")
        with self.assertRaises(PanelError):
            self.rev.mark(SABADO.isoformat(), clave, "genial")
        with self.assertRaises(PanelError):
            self.rev.mark("ayer", clave, "hecho")
        with self.assertRaises(KeyError):
            self.rev.mark(SABADO.isoformat(), "inventado", "hecho")
        # un bloque añadido al calendario después de la foto también se puede marcar (cuenta como extra)
        self.events.append(evento(SABADO, 18, "Estudio: ETC"))
        self.today()
        tarde = revision.block_key(self.events[1]["start"], "Estudio: ETC")
        self.rev.mark(SABADO.isoformat(), tarde, "hecho")
        n = self.rev.numbers(SABADO, today=self.today())
        self.assertEqual(n["estudio"]["total"], 1)
        self.assertEqual([x["titulo"] for x in n["extra"]["lista"]], ["Estudio: ETC"])

    def test_marcar_ayer_tras_cerrar_solo_sube_puntos(self):
        viernes = SABADO - timedelta(1)
        self.reloj.momento = at(viernes, 8, 30)
        self.events = [evento(viernes, 10, "Estudio: MTP")]
        self.today()
        clave = self.store.photo(viernes)["bloques"][0]["clave"]
        self.reloj.momento = at(SABADO, 8, 30)
        self.events = []
        self.rev.close_days()
        self.assertEqual(self.store.day(viernes)["puntos"], 0)
        self.assertIsNone(self.store.day(viernes)["datos"]["activo"])
        self.rev.mark(viernes.isoformat(), clave, "hecho")
        d = self.store.day(viernes)
        self.assertEqual((d["puntos"], d["datos"]["activo"], d["cerrado"]), (20, True, True))   # bloque + día completo
        self.rev.mark(viernes.isoformat(), clave, None)
        self.assertEqual(self.store.day(viernes)["puntos"], 20)   # nada se resta nunca


class TextosTests(Base):
    def nums(self):
        t = self.store.add_task("Ejercicios <script>alert(1)</script>", SABADO)
        self.store.add_task("Subir corto", SABADO)
        self.hacer(t, 9)
        return self.rev.numbers(SABADO, today=self.today())

    def test_prompt_con_datos_etiquetados_y_sin_angulos(self):
        prompt = revision.build_prompt("noche", self.nums())
        self.assertIn("<hecho>Ejercicios script alert(1) /script</hecho>", " ".join(prompt.split()))
        self.assertIn("<pendiente>Subir corto</pendiente>", prompt)
        self.assertIn("NO escribas ninguna cifra", prompt)
        self.assertNotIn("<script>", prompt)

    def test_cadena_de_motores_y_plantilla(self):
        nums = self.nums()
        cf = Motor("workers-ai", error=RuntimeError("cuota"))
        local = Motor("local", reply="Has hecho 1 tarea.")   # con cifra: no vale
        with self.assertLogs("arthur.panel.revision", "INFO"):
            text, motor = revision.write_text("noche", nums, [cf, local], None)
        self.assertEqual(motor, "plantilla")
        self.assertIn("mañana puedes empezar por «Subir corto»", text)
        bueno = Motor("workers-ai", reply="Buen sábado: los ejercicios ya están.")
        self.assertEqual(revision.write_text("noche", nums, [bueno], None), ("Buen sábado: los ejercicios ya están.", "workers-ai"))

    def test_plantillas_sin_culpa(self):
        nums = self.nums()
        nums["activo"] = False
        self.assertIn("Hoy no pasó", revision.template("noche", nums, {"racha": 3, "comodin_disponible": False}))
        self.assertIn("El comodín", revision.template("noche", nums, {"racha": 3, "comodin_disponible": False}))
        self.assertIn("puntos y logros", revision.template("noche", nums, {"racha": 0, "comodin_disponible": True}))
        nums["pausa"] = True
        self.assertIn("descanso", revision.template("noche", nums, None))
        for frase in ("peligro", "pierdes", "fallado"):
            self.assertNotIn(frase, revision.template("noche", nums, None))

    def test_aviso_de_la_noche(self):
        nums = self.nums()
        game = {"racha": {"racha": 5}, "nivel": {"nivel": 4}}
        self.assertEqual(revision.notice("noche", nums, game), "Tu día: 1 de 2 cosas hechas · racha 5 días")
        nums["repaso"]["hecha"] = True
        self.assertEqual(revision.notice("noche", nums, None), "Tu día: 1 de 2 cosas hechas · Repaso ✓")

    def test_nota_markdown(self):
        nums = self.nums()
        md = revision.to_markdown("noche", nums, "Buen día.", None, SABADO)
        self.assertIn("# Sábado 24 de octubre · hasta las 08:30", md)
        self.assertIn("## Has hecho\n- Ejercicios <script>alert(1)</script>", md)
        self.assertIn("## Ha quedado sin hacer\n- Subir corto", md)
        self.assertIn("- Tareas del plan: 1 de 2 · 50 %", md)
        self.assertIn("> Buen día.", md)
        self.assertNotIn("Nivel", md)


class EjecucionTests(Base):
    def test_revision_nocturna_guarda_y_avisa(self):
        t = self.store.add_task("de hoy", SABADO)
        self.today()
        self.hacer(t, 10)
        self.motores = [Motor("workers-ai", reply="Buen día, Marc.")]
        self.reloj.momento = at(SABADO, 21, 30)
        out = self.rev.run("noche")
        self.assertEqual(out, {"tipo": "noche", "texto": "Buen día, Marc.", "motor": "workers-ai", "avisada": True})
        self.assertEqual(self.aviso.sent, [("Tu día: 1 de 1 cosas hechas · racha 1 día", "panel")])
        rev = self.store.revision(SABADO, "noche")
        self.assertEqual(rev["datos"]["tareas"]["hechas"], 1)
        live = self.rev.live()
        self.assertEqual(live["revision"]["texto"], "Buen día, Marc.")
        self.assertEqual(live["juego"]["puntos_hoy"]["total"], 20)   # tarea + día completo (Repaso desconocido)
        self.assertEqual(live["juego"]["nivel"]["nivel"], 1)
        self.assertEqual(live["juego"]["racha"]["racha"], 1)
        conseguidas = [b["id"] for b in live["juego"]["insignias"] if b["conseguida"]]
        self.assertEqual(conseguidas, ["primera-tarea", "primer-completo"])

    def test_aviso_que_falla_no_borra_la_revision(self):
        class Roto:
            def send(self, *a, **k):
                raise OSError("sin red")
        self.rev.notifier = Roto()
        self.reloj.momento = at(SABADO, 21, 30)
        with self.assertLogs("arthur.panel.revision", "WARNING"):
            out = self.rev.run("noche")
        self.assertFalse(out["avisada"])
        self.assertEqual(self.store.revision(SABADO, "noche")["motor"], "plantilla")

    def test_ocultar_el_juego(self):
        self.rev.hide(True)
        self.reloj.momento = at(SABADO, 21, 30)
        live = self.rev.live()
        self.assertTrue(live["oculto"])
        self.assertIsNone(live["juego"])
        self.rev.run("noche")
        self.assertNotIn("racha", self.aviso.sent[0][0])
        self.rev.hide(False)
        self.assertIsNotNone(self.rev.live()["juego"])

    def test_pausa_de_hoy(self):
        self.rev.pause(True)
        live = self.rev.live()
        self.assertTrue(live["pausa"])
        self.assertTrue(live["juego"]["racha"]["en_pausa"])
        self.rev.pause(False)
        self.assertFalse(self.rev.live()["pausa"])

    def test_semana_del_domingo_y_linea_del_lunes(self):
        lunes = date(2026, 10, 19)
        for i in range(7):
            d = lunes + timedelta(i)
            self.reloj.momento = at(d, 8, 30)
            t = self.store.add_task(f"tarea {i}", d)
            self.events = [evento(d, 10, "Estudio: MTP"), evento(d, 11, "Estudio: ETC")]
            self.today()
            if i % 2 == 0:
                self.hacer(t, 12)
                self.rev.mark(d.isoformat(), self.store.photo(d)["bloques"][0]["clave"], "hecho")
            if i == 1:
                self.rev.mark(d.isoformat(), self.store.photo(d)["bloques"][1]["clave"], "medio")
        domingo = lunes + timedelta(6)
        self.reloj.momento = at(domingo, 21, 30)
        self.rev.run("semana")
        semana = self.store.revision(domingo, "semana")["datos"]
        # Cada tarea cuenta una vez, el día de vencimiento; las atrasadas no rebajan los siguientes.
        self.assertEqual((semana["tareas"]["hechas"], semana["tareas"]["total"], semana["tareas"]["porcentaje"]),
                         (4, 7, 57))
        self.assertEqual((semana["estudio"]["medios"], semana["estudio"]["total"]), (9, 14))
        self.assertEqual(semana["estudio"]["por_asignatura"],
                         [{"asignatura": "ETC", "medios": 1, "total": 7}, {"asignatura": "MTP", "medios": 8, "total": 7}])
        self.assertEqual(semana["dias_activos"], 5)
        self.assertEqual(self.aviso.sent[-1][0], "Tu semana: 57 % de tareas · 5 días activos · nivel 1")
        live = self.rev.live()
        self.assertTrue(live["semana"]["mostrar"])
        self.assertEqual(live["semana"]["estado"], "cerrada")
        self.assertTrue(self.rev.week_read())
        self.assertTrue(self.rev.live()["semana"]["leida"])
        self.assertIn("revisar-semana",
                      [b["id"] for b in self.rev.live()["juego"]["insignias"] if b["conseguida"]])

        self.reloj.momento = at(domingo + timedelta(1), 8)
        self.assertEqual(self.rev.monday_line(domingo + timedelta(1)),
                         "La semana pasada: 4 de 7 tareas (57 %) · 5 días activos.")
        self.assertIsNone(self.rev.monday_line(domingo))
        live = self.rev.live()
        self.assertEqual(live["semana"]["inicio"], lunes.isoformat())
        self.assertTrue(live["semana"]["mostrar"])

        nota = self.rev.note(domingo, "semana")
        self.assertEqual(nota["nombre"], "Semana-2026-W43.md")
        self.assertIn("# Semana del 19 al 25 de octubre", nota["markdown"])
        self.assertIn("  - MTP: 4 de 7", nota["markdown"])
        self.assertIsNone(self.rev.note(domingo, "noche"))
        diaria = self.store.revision(domingo, "noche")
        self.assertIsNone(diaria, "el domingo la semanal sustituye a la nocturna")


if __name__ == "__main__":
    unittest.main()
