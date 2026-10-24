"""El juego de la revisión (arthur/panel/juego.py): puntos con topes, niveles sin límite, racha sin deuda e insignias.

Las cifras de los niveles son las del ejemplo de JUEGO.md (rama codex/juego), que es el contrato.
"""

import unittest
from datetime import date, timedelta

from arthur.panel import juego

LUNES = date(2026, 10, 19)


def dia(activo=True, pausa=False, **extra):
    return {"activo": activo, "pausa": pausa, **extra}


class NivelTests(unittest.TestCase):
    def test_umbrales_y_costes_de_la_tabla(self):
        self.assertEqual([juego.threshold(n) for n in (1, 2, 3, 10, 20, 50, 100, 200)],
                         [0, 100, 201, 936, 2071, 6076, 14751, 39601])
        self.assertEqual([juego.cost(n) for n in (1, 2, 20, 100)], [100, 101, 119, 199])

    def test_ejemplos_de_juego_md(self):
        self.assertEqual(juego.level_for(0), {"puntos": 0, "nivel": 1, "rango": 1, "avance": 0, "coste": 100,
                                              "siguiente_en": 100})
        n = juego.level_for(520)
        self.assertEqual((n["nivel"], n["rango"], n["avance"], n["coste"], n["siguiente_en"]), (6, 1, 10, 105, 615))
        n = juego.level_for(770)
        self.assertEqual((n["nivel"], n["avance"], n["coste"]), (8, 49, 107))
        n = juego.level_for(14851)
        self.assertEqual((n["nivel"], n["rango"], n["avance"], n["coste"]), (100, 10, 100, 199))

    def test_justo_en_el_umbral_y_uno_menos(self):
        self.assertEqual(juego.level_for(100)["nivel"], 2)
        self.assertEqual(juego.level_for(99)["nivel"], 1)
        self.assertEqual(juego.level_for(936)["nivel"], 10)
        self.assertEqual(juego.level_for(935)["nivel"], 9)

    def test_sin_tope_y_rango_cada_diez(self):
        grande = juego.threshold(5000)
        self.assertEqual(juego.level_for(grande)["nivel"], 5000)
        self.assertEqual(juego.level_for(grande)["rango"], 500)
        self.assertEqual([juego.rank(n) for n in (1, 10, 11, 20, 21)], [1, 1, 2, 2, 3])

    def test_puntos_negativos_no_existen(self):
        self.assertEqual(juego.level_for(-5)["nivel"], 1)


class PuntosTests(unittest.TestCase):
    def test_topes_por_dia(self):
        p = juego.day_points(repaso_preguntas=12, repaso_hecha=True, validas=7, completo=True)
        self.assertEqual(p, {"repaso": 50, "tareas": 30, "bono": 10, "total": 90})

    def test_dia_normal(self):
        p = juego.day_points(repaso_preguntas=3, repaso_hecha=False, validas=2, completo=False)
        self.assertEqual(p["total"], 50)

    def test_sesion_hecha_sin_numero_de_preguntas_cuenta_la_sesion_entera(self):
        self.assertEqual(juego.day_points(repaso_preguntas=None, repaso_hecha=True, validas=0, completo=False)["repaso"], 50)
        self.assertEqual(juego.day_points(repaso_preguntas=None, repaso_hecha=None, validas=0, completo=False)["total"], 0)


class RachaTests(unittest.TestCase):
    def test_dias_activos_seguidos(self):
        dias = {LUNES + timedelta(i): dia() for i in range(3)}
        r = juego.streak(dias, LUNES + timedelta(2))
        self.assertEqual((r["racha"], r["activos"], r["protegidos"], r["dias_activos"]), (3, 3, 0, 3))

    def test_hoy_sin_hacer_no_rompe(self):
        dias = {LUNES: dia(), LUNES + timedelta(1): dia(activo=False)}
        r = juego.streak(dias, LUNES + timedelta(1))
        self.assertEqual(r["racha"], 1)
        self.assertTrue(r["comodin_disponible"])

    def test_comodin_una_vez_por_semana(self):
        # lunes activo, martes nada (comodín), miércoles activo, jueves nada (sin comodín: se rompe), viernes activo
        estados = [True, False, True, False, True]
        dias = {LUNES + timedelta(i): dia(a) for i, a in enumerate(estados)}
        r = juego.streak(dias, LUNES + timedelta(4))
        self.assertEqual((r["racha"], r["activos"], r["protegidos"]), (1, 1, 0))
        self.assertEqual(r["dias_activos"], 3)
        self.assertFalse(r["comodin_disponible"])
        self.assertEqual(r["semana"][1]["estado"], "protegido")
        self.assertEqual(r["semana"][3]["estado"], "inactivo")

    def test_el_comodin_vuelve_el_lunes_y_no_se_acumula(self):
        domingo = LUNES - timedelta(1)
        dias = {domingo - timedelta(1): dia(), domingo: dia(activo=False), LUNES: dia(activo=False),
                LUNES + timedelta(1): dia()}
        r = juego.streak(dias, LUNES + timedelta(1))
        # sábado activo, domingo protegido (comodín de esa semana), lunes protegido (comodín nuevo), martes activo
        self.assertEqual((r["racha"], r["activos"], r["protegidos"]), (4, 2, 2))

    def test_el_comodin_no_empieza_una_racha(self):
        dias = {LUNES: dia(activo=False), LUNES + timedelta(1): dia()}
        r = juego.streak(dias, LUNES + timedelta(1))
        self.assertEqual((r["racha"], r["protegidos"]), (1, 0))
        self.assertTrue(r["comodin_disponible"])

    def test_pausa_congela(self):
        dias = {LUNES: dia(), LUNES + timedelta(1): dia(activo=False, pausa=True),
                LUNES + timedelta(2): dia(activo=False, pausa=True), LUNES + timedelta(3): dia()}
        r = juego.streak(dias, LUNES + timedelta(3))
        self.assertEqual((r["racha"], r["protegidos"]), (2, 0))
        self.assertTrue(r["comodin_disponible"])

    def test_sin_datos_no_es_fallo(self):
        # martes sin fila (servidor apagado) y miércoles «por confirmar» (bloques sin marcar): no rompen ni gastan
        dias = {LUNES: dia(), LUNES + timedelta(2): dia(activo=None), LUNES + timedelta(3): dia()}
        r = juego.streak(dias, LUNES + timedelta(3))
        self.assertEqual((r["racha"], r["protegidos"]), (2, 0))
        self.assertEqual(r["semana"][1]["estado"], "sin_datos")
        self.assertEqual(r["semana"][2]["estado"], "sin_datos")

    def test_semana_con_futuro_y_pausa_de_hoy(self):
        dias = {LUNES: dia(), LUNES + timedelta(1): dia(activo=False, pausa=True)}
        r = juego.streak(dias, LUNES + timedelta(1))
        self.assertTrue(r["en_pausa"])
        self.assertEqual([d["estado"] for d in r["semana"]],
                         ["activo", "pausa", "futuro", "futuro", "futuro", "futuro", "futuro"])

    def test_sin_historia(self):
        r = juego.streak({}, LUNES)
        self.assertEqual((r["racha"], r["dias_activos"]), (0, 0))


class InsigniasTests(unittest.TestCase):
    def hist(self, n, **campos):
        base = {"activo": True, "pausa": False, "preguntas": 0, "errores": 0, "validas": 0, "completo": False}
        return [(LUNES + timedelta(i), {**base, **campos}) for i in range(n)]

    def test_catalogo_con_svg_de_codex(self):
        ids = [b["id"] for b in juego.BADGES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("primer-paso", ids)
        for b in juego.BADGES:
            self.assertTrue(b["nombre"] and b["condicion"] and b["texto"])

    def test_fechas_en_que_se_consiguen(self):
        got = juego.badges(self.hist(10, preguntas=5, validas=1), semana_leida=None)
        self.assertEqual(got["primer-paso"], LUNES)
        self.assertEqual(got["diez-repasos"], LUNES + timedelta(1))
        self.assertEqual(got["cincuenta-repasos"], LUNES + timedelta(9))
        self.assertNotIn("cien-repasos", got)
        self.assertEqual(got["primera-tarea"], LUNES)
        self.assertEqual(got["diez-tareas"], LUNES + timedelta(9))
        self.assertEqual(got["tres-activos"], LUNES + timedelta(2))
        self.assertEqual(got["diez-activos"], LUNES + timedelta(9))
        self.assertNotIn("primer-completo", got)
        self.assertNotIn("corregir", got)
        self.assertNotIn("revisar-semana", got)

    def test_retomar_tras_dos_dias_sin_actividad_o_pausa_pero_no_sin_datos(self):
        h = [(LUNES, {"activo": True, "pausa": False}), (LUNES + timedelta(1), {"activo": False, "pausa": False}),
             (LUNES + timedelta(2), {"activo": False, "pausa": True}), (LUNES + timedelta(3), {"activo": True, "pausa": False})]
        self.assertEqual(juego.badges(h, semana_leida=None)["retomar"], LUNES + timedelta(3))
        sin_datos = [h[0], h[1], (LUNES + timedelta(3), {"activo": True, "pausa": False})]  # el miércoles no hay fila
        self.assertNotIn("retomar", juego.badges(sin_datos, semana_leida=None))

    def test_completo_corregir_y_semana_leida(self):
        h = self.hist(1, preguntas=4, errores=1, completo=True)
        got = juego.badges(h, semana_leida=LUNES + timedelta(6))
        self.assertEqual(got["primer-completo"], LUNES)
        self.assertEqual(got["corregir"], LUNES)
        self.assertEqual(got["revisar-semana"], LUNES + timedelta(6))


if __name__ == "__main__":
    unittest.main()
