"""Fuentes del cerebro (Obsidian y PDF) y corte en trozos (cerebro/chunks.py). Sin red ni pdftotext real."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from arthur.cerebro.chunks import Chunk, Doc, split
from arthur.cerebro.sources import obsidian, pdf


def doc(text: str, **kw) -> Doc:
    base = {"source": "obsidian:cerebro", "ref": "nota.md", "title": "Nota", "subject": "",
            "changed_at": "2026-10-17T20:05:00+02:00", "text": text, "links": ()}
    base.update(kw)
    return Doc(**base)


def frases(n: int, palabra: str = "frase") -> str:
    # Frases distintas (con su número) para poder reconocer cuál pasa al trozo siguiente.
    return " ".join(f"Esta es la {palabra} número {i} del apunte de prueba." for i in range(n))


class SplitTest(unittest.TestCase):
    def test_corta_por_encabezado(self):
        chunks = split(doc("# A\nTexto de A.\n\n## B\nTexto de B.\n"))
        self.assertEqual([(c.heading, c.text) for c in chunks], [("A", "Texto de A."), ("A › B", "Texto de B.")])
        self.assertEqual([c.n for c in chunks], [0, 1])
        self.assertIsInstance(chunks[0], Chunk)

    def test_encabezado_hermano_sustituye(self):
        chunks = split(doc("# A\n## B\nb.\n## C\nc.\n# D\nd.\n"))
        self.assertEqual([c.heading for c in chunks], ["A › B", "A › C", "D"])

    def test_no_parte_formulas(self):
        # Review Focus 1: una fórmula de varias líneas más larga que el tamaño del trozo no se corta.
        formula = "$$\n" + "\n".join(f"a_{{{i}}} + b_{{{i}}} = c_{{{i}}} \\\\" for i in range(60)) + "\n$$"
        self.assertGreater(len(formula), 1200)
        chunks = split(doc(f"# Fórmulas\n{frases(10)}\n\n{formula}\n\n{frases(10, 'idea')}\n"))
        self.assertTrue(any(formula in c.text for c in chunks))
        for c in chunks:
            # Ningún trozo tiene un número impar de $$: o la fórmula entera o nada de ella.
            self.assertEqual(c.text.count("$$") % 2, 0, c.text[:80])

    def test_encabezado_dentro_de_codigo_no_corta(self):
        texto = "# A\n```python\n# esto es un comentario\nx = 1\n```\n"
        chunks = split(doc(texto))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].heading, "A")
        self.assertIn("# esto es un comentario", chunks[0].text)

    def test_solapa_una_frase(self):
        chunks = split(doc("# A\n" + frases(60)))
        self.assertGreater(len(chunks), 2)
        for antes, despues in zip(chunks, chunks[1:]):
            ultima = antes.text.rsplit(". ", 1)[-1]
            self.assertTrue(despues.text.startswith(ultima), (ultima, despues.text[:80]))
        for c in chunks:
            self.assertLessEqual(len(c.text), 800)

    def test_quita_secretos(self):
        secreto = "ghp_" + "a" * 20
        chunks = split(doc(f"# Claves\nEl token de GitHub es {secreto} y no se comparte.\n"))
        self.assertTrue(chunks)
        for c in chunks:
            self.assertNotIn(secreto, c.text)
        # Lo de alrededor se sigue pudiendo encontrar: solo se tapa la clave.
        self.assertIn("no se comparte", chunks[0].text)

    def test_sin_texto_no_da_trozos(self):
        self.assertEqual(split(doc("# Solo título\n\n")), [])


def escribir(root: Path, rel: str, texto: str) -> Path:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(texto, encoding="utf-8")
    return f


class ObsidianTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_doc_de_una_nota(self):
        f = escribir(self.root, "Cálculo/Tema 2/Límites.md", "---\ntags: [uni]\n---\n# Límite de una sucesión\nTexto.\n")
        os.utime(f, (1_792_260_300, 1_792_260_300))  # 2026-10-17 20:05 en Madrid
        (d,) = list(obsidian.scan(self.root, "cerebro"))
        self.assertEqual(d.source, "obsidian:cerebro")
        self.assertEqual(d.ref, "Cálculo/Tema 2/Límites.md")
        self.assertEqual(d.title, "Límite de una sucesión")
        self.assertEqual(d.subject, "Cálculo")
        self.assertEqual(d.changed_at, "2026-10-17T20:05:00+02:00")
        # El YAML del principio no es contenido de la nota.
        self.assertNotIn("tags", d.text)

    def test_titulo_por_nombre(self):
        escribir(self.root, "Idea suelta.md", "Sin encabezado.\n")
        (d,) = list(obsidian.scan(self.root, "business-lab"))
        self.assertEqual((d.title, d.subject, d.source), ("Idea suelta", "", "obsidian:business-lab"))

    def test_salta_carpetas(self):
        # Review Focus 4: conflictos y temporales de Syncthing, carpetas privadas y notas excluidas.
        escribir(self.root, "buena.md", "# Buena\nSí.\n")
        for rel in (".obsidian/workspace.md", ".trash/vieja.md", "private/diario.md", "x/private/y.md",
                    ".stversions/buena~20261017.md", "buena.sync-conflict-20261017-120000-ABCDEFG.md",
                    "~syncthing~buena.md.tmp", ".syncthing.buena.md.tmp", "no-indexar.md"):
            escribir(self.root, rel, "---\ncerebro: no\n---\n# X\n" if rel == "no-indexar.md" else "# X\nNo.\n")
        self.assertEqual([d.ref for d in obsidian.scan(self.root, "cerebro")], ["buena.md"])

    def test_cerebro_no_en_yaml_con_comillas(self):
        escribir(self.root, "a.md", "---\ntitulo: a\ncerebro: \"no\"\n---\nTexto.\n")
        escribir(self.root, "b.md", "---\ncerebro: sí\n---\nTexto.\n")
        self.assertEqual([d.ref for d in obsidian.scan(self.root, "cerebro")], ["b.md"])

    def test_enlaces(self):
        escribir(self.root, "a.md", "Ver [[Nota B|alias]] y [[Nota C#apartado]], otra vez [[Nota B]] y [[#local]].\n")
        (d,) = list(obsidian.scan(self.root, "cerebro"))
        self.assertEqual(d.links, ("Nota B", "Nota C"))

    def test_no_sigue_enlaces_simbolicos(self):
        fuera = tempfile.TemporaryDirectory()
        self.addCleanup(fuera.cleanup)
        escribir(Path(fuera.name), "secreto.md", "# Fuera\nNo.\n")
        os.symlink(fuera.name, self.root / "enlace")
        os.symlink(Path(fuera.name) / "secreto.md", self.root / "nota.md")
        self.assertEqual(list(obsidian.scan(self.root, "cerebro")), [])

    def test_bóveda_que_no_existe(self):
        self.assertEqual(list(obsidian.scan(self.root / "no-está", "cerebro")), [])


class FakeRun:
    def __init__(self, outputs: dict[str, str]):
        self.outputs, self.calls = outputs, []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        name = Path(args[-2]).name
        return subprocess.CompletedProcess(args, 0, stdout=self.outputs[name].encode("utf-8"), stderr=b"")


class PdfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pdf_con_texto(self):
        (self.root / "Física").mkdir()
        (self.root / "Física" / "Ondas.pdf").write_bytes(b"%PDF-1.7 falso")
        run = FakeRun({"Ondas.pdf": "Una onda es una perturbación que se propaga.\f" + "Más texto del PDF. " * 5})
        (d,) = list(pdf.scan(self.root, run=run))
        self.assertEqual((d.source, d.ref, d.title, d.subject), ("pdf", "Física/Ondas.pdf", "Ondas", "PDF"))
        self.assertNotIn("\f", d.text)
        args, kw = run.calls[0]
        self.assertEqual(args[:4], ["pdftotext", "-layout", "-enc", "UTF-8"])
        self.assertEqual(args[-1], "-")
        self.assertEqual(kw["timeout"], 60)

    def test_pdf_sin_texto(self):
        (self.root / "escaneo.pdf").write_bytes(b"%PDF")
        (self.root / "bueno.pdf").write_bytes(b"%PDF")
        run = FakeRun({"escaneo.pdf": "  \f \n 12 \f", "bueno.pdf": "Texto suficiente para contar como un PDF con texto."})
        self.assertEqual([d.ref for d in pdf.scan(self.root, run=run)], ["bueno.pdf"])
        self.assertEqual(pdf.skipped, ["escaneo.pdf"])

    def test_pdftotext_falla_o_tarda(self):
        (self.root / "roto.pdf").write_bytes(b"%PDF")
        (self.root / "lento.pdf").write_bytes(b"%PDF")

        def run(args, **kw):
            if Path(args[-2]).name == "lento.pdf":
                raise subprocess.TimeoutExpired(args, 60)
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"Syntax Error")

        self.assertEqual(list(pdf.scan(self.root, run=run)), [])
        self.assertEqual(sorted(pdf.skipped), ["lento.pdf", "roto.pdf"])

    def test_salta_conflictos(self):
        (self.root / "a.sync-conflict-20261017-120000-ABCDEFG.pdf").write_bytes(b"%PDF")
        (self.root / ".stversions").mkdir()
        (self.root / ".stversions" / "b.pdf").write_bytes(b"%PDF")
        self.assertEqual(list(pdf.scan(self.root, run=FakeRun({}))), [])


if __name__ == "__main__":
    unittest.main()
