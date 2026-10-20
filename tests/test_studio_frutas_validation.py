"""Guiones de frutas y biblioteca falsos: no acceden a red ni al escritorio."""

from __future__ import annotations

import copy
import unittest

from arthur.studio.frutas_validation import validar_guion


def catalogo():
    return {
        "estado_inicial": {"carpeta": "vera", "formulario": "sin_firmar"},
        "planos": [
            {"id": "fina-carpeta", "personaje": "fina", "validado": True,
             "gestos": ["camara"], "objetos_visibles": ["carpeta"],
             "hechos_visibles": ["recibe_carpeta"]},
            {"id": "fina-reaccion", "personaje": "fina", "validado": True,
             "gestos": ["camara"], "objetos_visibles": [], "hechos_visibles": []},
            {"id": "mora-reaccion", "personaje": "mora", "validado": True,
             "gestos": ["camara"], "objetos_visibles": [], "hechos_visibles": []},
            {"id": "berto-formulario", "personaje": "berto", "validado": True,
             "gestos": ["camara"], "objetos_visibles": ["formulario"],
             "hechos_visibles": ["hoja_en_blanco"]},
            {"id": "kiro-reaccion", "personaje": "kiro", "validado": True,
             "gestos": ["camara"], "objetos_visibles": [], "hechos_visibles": []},
            {"id": "vera-reaccion", "personaje": "vera", "validado": True,
             "gestos": ["camara"], "objetos_visibles": [], "hechos_visibles": []},
        ],
    }


def guion():
    parejas = [
        ("vera", "vera-reaccion"), ("fina", "fina-carpeta"),
        ("mora", "mora-reaccion"), ("berto", "berto-formulario"),
        ("kiro", "kiro-reaccion"), ("fina", "fina-reaccion"),
        ("mora", "mora-reaccion"), ("fina", "fina-carpeta"),
    ]
    escenas = []
    for numero, (personaje, plano) in enumerate(parejas, 1):
        escenas.append({"numero": numero, "personaje": personaje, "plano": plano,
                        "gesto": "camara", "objetos": [], "hechos": [],
                        "estado_antes": {}, "cambios": []})
    escenas[1].update(objetos=["carpeta"], hechos=["recibe_carpeta"],
                      estado_antes={"carpeta": "vera"},
                      cambios=[{"objeto": "carpeta", "desde": "vera", "hasta": "fina",
                                "hecho": "recibe_carpeta"}])
    escenas[3].update(objetos=["formulario"], hechos=["hoja_en_blanco"],
                      estado_antes={"formulario": "sin_firmar"})
    escenas[7].update(objetos=["carpeta"], estado_antes={"carpeta": "fina"})
    return {"escenas": escenas}


class FrutasValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalogo = catalogo()
        self.guion = guion()

    def test_guion_valido_puro(self):
        original_guion, original_catalogo = copy.deepcopy(self.guion), copy.deepcopy(self.catalogo)
        resultado = validar_guion(self.guion, self.catalogo)
        self.assertTrue(resultado.valido, resultado.motivos)
        self.assertEqual(self.guion, original_guion)
        self.assertEqual(self.catalogo, original_catalogo)

    def test_plano_inexistente(self):
        self.guion["escenas"][0]["plano"] = "fina-inventado"
        resultado = validar_guion(self.guion, self.catalogo)
        self.assertFalse(resultado.valido)
        self.assertIn("plano inexistente", " ".join(resultado.motivos))

    def test_objeto_equivocado(self):
        self.guion["escenas"][3]["objetos"] = ["anillo"]
        self.assertIn("objeto 'anillo' no se ve", " ".join(
            validar_guion(self.guion, self.catalogo).motivos))

    def test_continuidad_rota(self):
        self.guion["escenas"][7]["estado_antes"] = {"carpeta": "vera"}
        self.assertIn("continuidad rota", " ".join(
            validar_guion(self.guion, self.catalogo).motivos))

    def test_cambio_sin_hecho_visible(self):
        self.guion["escenas"][1]["hechos"] = []
        self.assertIn("carece del hecho visible", " ".join(
            validar_guion(self.guion, self.catalogo).motivos))

    def test_reparto_y_gesto_no_reales(self):
        self.guion["escenas"][0]["personaje"] = "lola"
        self.guion["escenas"][0]["gesto"] = "saca_anillo"
        resultado = validar_guion(self.guion, self.catalogo)
        self.assertFalse(resultado.valido)
        self.assertIn("fuera del reparto", " ".join(resultado.motivos))
        self.assertIn("no se ve", " ".join(resultado.motivos))

    def test_plano_no_validado(self):
        self.catalogo["planos"][0]["validado"] = False
        self.assertIn("no está validado", " ".join(
            validar_guion(self.guion, self.catalogo).motivos))

    def test_limites_y_datos_mal_formados(self):
        self.guion["escenas"] = self.guion["escenas"][:7]
        self.assertIn("entre 8 y 12", " ".join(
            validar_guion(self.guion, self.catalogo).motivos))
        self.guion["escenas"][0]["personaje"] = []
        self.assertFalse(validar_guion(self.guion, self.catalogo).valido)


if __name__ == "__main__":
    unittest.main()
