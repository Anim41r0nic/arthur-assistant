"""Comprueba que el ejemplo editorial se apoya en planos reales y sigue siendo usable."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from arthur.studio.frutas_validation import REPARTO, validar_guion


DOCS = Path(__file__).resolve().parents[1] / "docs"


class FrutasExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogo = json.loads((DOCS / "frutas-catalogo-ejemplo.json").read_text(encoding="utf-8"))
        cls.guion = json.loads((DOCS / "frutas-guion-ejemplo.json").read_text(encoding="utf-8"))

    def test_guion_pasa_con_catalogo_canonico(self):
        resultado = validar_guion(self.guion, self.catalogo)
        self.assertTrue(resultado.valido, resultado.motivos)

    def test_seis_personajes_y_planos_unicos(self):
        planos = self.catalogo["planos"]
        self.assertEqual({p["personaje"] for p in planos}, REPARTO)
        self.assertEqual(len({p["id"] for p in planos}), len(planos))
        self.assertTrue(all(p["validado"] is True for p in planos))
        self.assertFalse({"lola", "mara", "teo"} & {p["personaje"] for p in planos})

    def test_foto_no_declara_movimiento_corporal(self):
        for plano in self.catalogo["planos"]:
            self.assertTrue(plano["archivo"].endswith(".png"))
            self.assertNotIn("camara", plano["gestos"])
            self.assertFalse({"mueve_mano", "camina", "entrega_carpeta"} & set(plano["hechos_visibles"]))

    def test_estado_visible_detecta_carpeta_en_manos_equivocadas(self):
        malo = copy.deepcopy(self.guion)
        malo["escenas"][4]["plano"] = "M_CARPETA"
        malo["escenas"][4]["personaje"] = "mora"
        malo["escenas"][4]["gesto"] = "expresion_sorprendida"
        self.assertIn("continuidad rota", " ".join(
            validar_guion(malo, self.catalogo).motivos))

    def test_rutas_y_uso_en_episodios_privados(self):
        raiz = Path(self.catalogo["raiz_recursos"]).expanduser()
        if not raiz.is_dir():
            self.skipTest("Los recursos privados de Marc no están en esta máquina")
        guiones = list(raiz.glob("arco-0[23]/episodio-0[5678]/guion.json"))
        self.assertEqual(len(guiones), 4)
        usados = {escena["plano"] for ruta in guiones
                  for escena in json.loads(ruta.read_text(encoding="utf-8"))["escenas"]}
        for plano in self.catalogo["planos"]:
            ruta = raiz / plano["archivo"]
            self.assertTrue(ruta.is_file(), str(ruta))
            self.assertIn(plano["id"], usados)


if __name__ == "__main__":
    unittest.main()
