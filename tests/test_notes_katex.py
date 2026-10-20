"""KaTeX vendorizado en arthur/notes/web/katex/: que esté entero, que no haya cambiado y que no pida nada de fuera."""

import hashlib
import re
import unittest
from pathlib import Path

KATEX = Path(__file__).resolve().parent.parent / "arthur" / "notes" / "web" / "katex"
LINE = re.compile(r"^([0-9a-f]{64})  (\S+)$")


def listed() -> dict[str, str]:
    """Los ficheros apuntados en ORIGEN.txt con su sha256."""
    text = (KATEX / "ORIGEN.txt").read_text(encoding="utf-8")
    return {m.group(2): m.group(1) for m in map(LINE.match, text.splitlines()) if m}


class KatexVendorizado(unittest.TestCase):
    def test_ficheros_basicos(self):
        for name in ("katex.min.js", "katex.min.css", "LICENSE", "ORIGEN.txt"):
            self.assertTrue((KATEX / name).is_file(), name)
        self.assertIn("MIT", (KATEX / "LICENSE").read_text(encoding="utf-8"))
        self.assertTrue(list((KATEX / "fonts").glob("*.woff2")))

    def test_origen_dice_version_e_integridad(self):
        text = (KATEX / "ORIGEN.txt").read_text(encoding="utf-8")
        self.assertRegex(text, r"Versión: \d+\.\d+\.\d+")
        self.assertIn("https://registry.npmjs.org/katex/-/katex-", text)
        self.assertRegex(text, r"sha512-[A-Za-z0-9+/]+=*")

    def test_sha256_coinciden(self):
        files = listed()
        for name in ("katex.min.js", "katex.min.css", "LICENSE"):
            self.assertIn(name, files)
        for name, digest in files.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((KATEX / name).read_bytes()).hexdigest(), digest)

    def test_no_hay_ficheros_sin_apuntar(self):
        present = {p.relative_to(KATEX).as_posix() for p in KATEX.rglob("*") if p.is_file()}
        self.assertEqual(present - {"ORIGEN.txt"}, set(listed()))

    def test_fuentes_solo_woff2(self):
        self.assertEqual({p.suffix for p in (KATEX / "fonts").iterdir()}, {".woff2"})

    def test_css_solo_apunta_a_fonts(self):
        css = (KATEX / "katex.min.css").read_text(encoding="utf-8")
        self.assertNotIn("@import", css)
        urls = re.findall(r"url\(\s*([^)]*?)\s*\)", css)
        self.assertTrue(urls)
        for url in urls:
            with self.subTest(url=url):
                self.assertRegex(url.strip("'\""), r"^fonts/[A-Za-z0-9_-]+\.woff2$")
                self.assertTrue((KATEX / url.strip("'\"")).is_file())


if __name__ == "__main__":
    unittest.main()
