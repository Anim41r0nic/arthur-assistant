"""Fechas en español de las tareas: «mañana», «el viernes», «el 20»... sin tocar reloj ni red."""

import unittest
from datetime import date

from arthur.panel.dates import parse_task

HOY = date(2026, 10, 16)  # viernes


class ParseTaskTests(unittest.TestCase):
    def test_sin_fecha(self):
        self.assertEqual(parse_task("comprar pan", HOY), ("comprar pan", None))

    def test_manana_pasado_manana_y_hoy(self):
        self.assertEqual(parse_task("llamar a Ana mañana", HOY), ("llamar a Ana", date(2026, 10, 17)))
        self.assertEqual(parse_task("pasado mañana dentista", HOY), ("dentista", date(2026, 10, 18)))
        self.assertEqual(parse_task("pagar hoy", HOY), ("pagar", HOY))

    def test_dia_de_la_semana(self):
        self.assertEqual(parse_task("entregar práctica el viernes", HOY),
                         ("entregar práctica", date(2026, 10, 23)))  # mismo día = semana siguiente
        self.assertEqual(parse_task("gimnasio el lunes", HOY), ("gimnasio", date(2026, 10, 19)))
        self.assertEqual(parse_task("gimnasio El Martes", HOY), ("gimnasio", date(2026, 10, 20)))

    def test_semana_con_y_sin_acento(self):
        self.assertEqual(parse_task("cita el miércoles", HOY)[1], date(2026, 10, 21))
        self.assertEqual(parse_task("cita el miercoles", HOY)[1], date(2026, 10, 21))
        self.assertEqual(parse_task("fiesta el sábado", HOY)[1], date(2026, 10, 17))
        self.assertEqual(parse_task("fiesta el SABADO", HOY)[1], date(2026, 10, 17))

    def test_dia_del_mes(self):
        self.assertEqual(parse_task("examen el 20", HOY), ("examen", date(2026, 10, 20)))
        self.assertEqual(parse_task("pagar el 3", HOY), ("pagar", date(2026, 11, 3)))  # ya pasó: mes siguiente
        self.assertEqual(parse_task("pagar el 31", date(2026, 11, 5))[1], date(2026, 12, 31))

    def test_dia_del_mes_fuera_de_rango_no_es_fecha(self):
        self.assertEqual(parse_task("ver el 0", HOY), ("ver el 0", None))
        self.assertEqual(parse_task("ver el 32", HOY), ("ver el 32", None))
        self.assertEqual(parse_task("año el 2027", HOY), ("año el 2027", None))

    def test_titulo_vacio_conserva_el_texto(self):
        self.assertEqual(parse_task("mañana", HOY), ("mañana", date(2026, 10, 17)))
        self.assertEqual(parse_task("el lunes", HOY), ("el lunes", date(2026, 10, 19)))

    def test_varias_expresiones_gana_la_ultima(self):
        self.assertEqual(parse_task("hoy o mañana revisar", HOY), ("hoy o revisar", date(2026, 10, 17)))
        self.assertEqual(parse_task("mañana llamar el lunes", HOY), ("mañana llamar", date(2026, 10, 19)))

    def test_espacios_colapsados(self):
        self.assertEqual(parse_task("llamar   a  Ana  mañana  ", HOY), ("llamar a Ana", date(2026, 10, 17)))


if __name__ == "__main__":
    unittest.main()
