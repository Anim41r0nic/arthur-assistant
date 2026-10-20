import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arthur.panel.sources import SourceError
from arthur.panel.sources.obsidian import LineChanged, ObsidianVault

TAREAS = (
    "# Tareas\n"          # 1
    "\n"                  # 2
    "- [ ] Claude: cosa\n"  # 3
    "- [x] Yo: hecha\n"   # 4
    "```\n"               # 5
    "- [ ] Yo: en código\n"  # 6
    "```\n"               # 7
    "- [ ] Yo: revisar la web\n"  # 8
    "  * [ ] Yo: anidada\n"  # 9
)


class ObsidianTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "boveda"
        (self.root / "00-Inicio").mkdir(parents=True)
        (self.root / ".obsidian").mkdir()
        self.fuera = Path(self._tmp.name) / "fuera.md"
        self.fuera.write_text("- [ ] Yo: ajena\n")
        self.tareas = self.root / "00-Inicio" / "Tareas.md"
        self.tareas.write_bytes(TAREAS.encode())
        (self.root / ".obsidian" / "x.md").write_text("- [ ] Yo: oculta\n")
        self.vault = ObsidianVault(self.root)

    def test_lee_solo_casillas_de_marc(self):
        cajas = self.vault.checkboxes()
        self.assertEqual(
            [(c.file, c.line, c.text, c.done) for c in cajas],
            [("00-Inicio/Tareas.md", 4, "Yo: hecha", True),
             ("00-Inicio/Tareas.md", 8, "Yo: revisar la web", False),
             ("00-Inicio/Tareas.md", 9, "Yo: anidada", False)],
        )

    def test_dos_casillas_a_la_vez_no_se_pisan(self):
        # Se alarga la escritura para que, sin cerrojo, los dos hilos leyeran el fichero original.
        escribir = ObsidianVault._escribir

        def lento(destino, datos, esperado):
            time.sleep(0.05)
            escribir(destino, datos, esperado)

        with patch.object(ObsidianVault, "_escribir", staticmethod(lento)):
            hilos = [threading.Thread(target=self.vault.toggle, args=("00-Inicio/Tareas.md", n, t))
                     for n, t in ((8, "Yo: revisar la web"), (9, "Yo: anidada"))]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join()
        datos = self.tareas.read_bytes()
        self.assertIn(b"- [x] Yo: revisar la web", datos)
        self.assertIn(b"  * [x] Yo: anidada", datos)

    def test_toggle_cambia_solo_esa_linea(self):
        caja = self.vault.toggle("00-Inicio/Tareas.md", 8, "Yo: revisar la web")
        self.assertTrue(caja.done)
        self.assertEqual(
            self.tareas.read_bytes(),
            TAREAS.replace("- [ ] Yo: revisar", "- [x] Yo: revisar").encode(),
        )
        self.vault.toggle("00-Inicio/Tareas.md", 9, "Yo: anidada")
        self.assertIn(b"  * [x] Yo: anidada\n", self.tareas.read_bytes())
        caja = self.vault.toggle("00-Inicio/Tareas.md", 8, "Yo: revisar la web")
        self.assertFalse(caja.done)
        self.assertEqual(self.tareas.read_bytes(), TAREAS.replace("  * [ ] Yo: anidada",
                                                               "  * [x] Yo: anidada").encode())

    def test_conserva_crlf_sin_salto_final_y_permisos(self):
        crlf = b"a\r\n- [ ] Yo: x\r\nfin"
        self.tareas.write_bytes(crlf)
        os.chmod(self.tareas, 0o640)
        self.vault.toggle("00-Inicio/Tareas.md", 2, "Yo: x")
        self.assertEqual(self.tareas.read_bytes(), b"a\r\n- [x] Yo: x\r\nfin")
        self.assertEqual(stat.S_IMODE(self.tareas.stat().st_mode), 0o640)
        self.assertEqual([p.name for p in self.tareas.parent.iterdir()], ["Tareas.md"])

    def test_linea_cambiada(self):
        for linea, texto in ((7, "Yo: revisar la web"), (8, "Yo: otra"), (99, "Yo: x"), (6, "Yo: en código")):
            with self.assertRaises(LineChanged):
                self.vault.toggle("00-Inicio/Tareas.md", linea, texto)
        self.assertEqual(self.tareas.read_bytes(), TAREAS.encode())

    def test_rutas_no_validas(self):
        (self.root / "enlace.md").symlink_to(self.fuera)
        (self.root / "nota.txt").write_text("- [ ] Yo: x\n")
        for ruta in ("../fuera.md", str(self.fuera), "enlace.md", "nota.txt",
                     ".obsidian/x.md", "no_existe.md", ""):
            with self.assertRaises(SourceError, msg=ruta):
                self.vault.toggle(ruta, 1, "Yo: ajena")
        self.assertEqual(self.fuera.read_text(), "- [ ] Yo: ajena\n")
        self.assertNotIn("ajena", [c.text for c in self.vault.checkboxes()])

    def test_boveda_inexistente(self):
        with self.assertRaises(SourceError) as cm:
            ObsidianVault(Path(self._tmp.name) / "nada").checkboxes()
        self.assertEqual(str(cm.exception), "La carpeta de Obsidian no está")

    def test_maximo_100(self):
        (self.root / "mucho.md").write_text("- [ ] Yo: t\n" * 150)
        self.assertEqual(len(ObsidianVault(self.root, ("mucho.md",)).checkboxes()), 100)

    def test_solo_notas_incluidas_en_los_ajustes(self):
        otra = self.root / "otra.md"
        otra.write_text("- [ ] Yo: empezar una guía\n")
        self.assertEqual(len(self.vault.checkboxes()), 3)
        with self.assertRaises(SourceError):
            self.vault.toggle("otra.md", 1, "Yo: empezar una guía")
        configurada = ObsidianVault(self.root, ("00-Inicio/Tareas.md", "otra.md"))
        self.assertEqual(len(configurada.checkboxes()), 4)

    def test_no_pisa_una_nota_editada_mientras_prepara_el_guardado(self):
        original_fsync = os.fsync

        def edicion_concurrente(fd):
            original_fsync(fd)
            self.tareas.write_text("- [ ] Yo: cambiado en Obsidian\n")

        with patch("arthur.panel.sources.obsidian.os.fsync", side_effect=edicion_concurrente):
            with self.assertRaises(LineChanged):
                self.vault.toggle("00-Inicio/Tareas.md", 8, "Yo: revisar la web")
        self.assertEqual(self.tareas.read_text(), "- [ ] Yo: cambiado en Obsidian\n")


if __name__ == "__main__":
    unittest.main()
