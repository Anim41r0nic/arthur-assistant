"""Tests de búsqueda e instalación de paquetes. pacman y terminal FALSOS: nada se instala."""

import shutil
import unittest
from unittest import mock

from arthur.core.config import Config
from arthur.core.events import EventBus
from arthur.core.router import ToolRouter
from arthur.security.audit import AuditLog
from arthur.security.policy import Policy
from arthur.tools.base import ToolCall, ToolError
from arthur.tools.packages import build_package_tools, parse_info, parse_search
from arthur.tools.registry import ToolRegistry

from .helpers import ScriptedConfirmer

SEARCH = """cachyos-extra-v3/vlc 3.0.23_2-16.1
    Free and open source cross-platform multimedia player and framework
extra/vlc 3.0.23_2-16
    Free and open source cross-platform multimedia player and framework
cachyos-extra-v3/libvlc 3.0.23_2-16.1 [installed]
    Free and open source cross-platform multimedia player and framework - library
"""
INFO = """Repository      : cachyos-extra-v3
Name            : vlc
Version         : 3.0.23_2-16.1
Description     : Free and open source cross-platform multimedia player and framework
URL             : https://www.videolan.org/vlc/
Licenses        : LGPL-2.1-or-later  GPL-2.0-or-later
Depends On      : libvlc  hicolor-icon-theme
                  qt6-base
Download Size   : 1,20 MiB
Installed Size  : 4,50 MiB

Repository      : extra
Name            : vlc
"""


class FakePacman:
    """Imita `env LANG=C pacman …` y apunta lo que se habría abierto en la terminal."""

    def __init__(self, installed=("libvlc",)):
        self.installed, self.launched, self.calls = set(installed), [], []

    def run(self, argv, *, input=None, timeout=10):
        self.calls.append(argv)
        args = argv[3:]  # quita "env LANG=C pacman"
        if args[0] == "-Ss":
            if "vlc" in args:
                return SEARCH
            raise ToolError("sin resultados")
        if args[0] == "-Si":
            if args[-1] in ("vlc", "libvlc"):
                return INFO
            raise ToolError("error: package not found")
        if args[0] == "-Q":
            if args[-1] in self.installed:
                return f"{args[-1]} 1.0\n"
            raise ToolError("not found")
        raise AssertionError(argv)

    def launch(self, argv, *, grace=2.0):
        self.launched.append(argv)


class PackagesTest(unittest.TestCase):
    def setUp(self):
        # install_package necesita una terminal (alacritty o kitty): los tests no pueden depender de que la
        # máquina que los ejecuta tenga una instalada (en un equipo de verdad sí; en la nube no).
        which = mock.patch.object(shutil, "which", lambda name: "/usr/bin/alacritty" if name == "alacritty" else None)
        which.start()
        self.addCleanup(which.stop)
        self.pacman = FakePacman()
        registry = ToolRegistry()
        for tool in build_package_tools(self.pacman):
            registry.register(tool)
        self.confirmer = ScriptedConfirmer()
        self.router = ToolRouter(registry, Policy(Config().security), self.confirmer, AuditLog(None), EventBus())

    def call(self, tool, **args):
        return self.router.execute(ToolCall(tool, args))

    def test_parsers(self):
        results = parse_search(SEARCH)
        self.assertEqual([(r["name"], r["repo"], r["installed"]) for r in results],
                         [("vlc", "cachyos-extra-v3", False), ("libvlc", "cachyos-extra-v3", True)])
        info = parse_info(INFO)
        self.assertEqual(info["Repository"], "cachyos-extra-v3")
        self.assertEqual(info["Depends On"], "libvlc  hicolor-icon-theme; qt6-base")

    def test_search(self):
        out = self.call("search_packages", query="vlc").output
        self.assertIn("vlc 3.0.23_2-16.1 — Free and open source", out)
        self.assertIn("libvlc 3.0.23_2-16.1 — ", out)
        self.assertNotIn("instalado)", out.splitlines()[1])  # la búsqueda no marca lo instalado
        self.assertIn("llama a install_package", out)
        self.assertTrue(out.startswith("Coincidencia exacta: vlc"))
        self.assertEqual(self.pacman.calls[0], ["env", "LANG=C", "pacman", "-Ss", "--", "vlc"])
        self.assertIn("No hay paquetes", self.call("search_packages", query="zzzz").output)
        self.assertEqual(self.call("search_packages", query="vlc; rm -rf ~").status, "error")

    def test_install_opens_a_terminal_and_does_not_install(self):
        result = self.call("install_package", name="vlc")
        self.assertEqual(result.status, "ok")
        self.assertIn("Pon tu contraseña", result.output)
        argv = self.pacman.launched[0]
        self.assertEqual(argv[:3], ["uwsm", "app", "--"])
        self.assertEqual(argv[-6:-3], ["-e", "bash", "-c"])
        self.assertIn('sudo pacman -S -- "$1"', argv[-3])
        self.assertEqual(argv[-1], "vlc")  # el nombre va aparte, nunca dentro del texto de la orden
        self.assertNotIn("vlc", argv[-3])
        self.assertFalse(any("-S" in c[3:4] for c in self.pacman.calls))  # ARTHUR nunca ejecuta pacman -S

    def test_a_name_can_never_be_taken_as_a_pacman_option(self):
        """Los nombres que escribe el modelo van tras «--» y no pueden empezar por «-» (no existe ningún paquete
        así): «--noconfirm», «--help» o «-r» no llegan a pacman como opciones."""
        for name in ("--noconfirm", "--help", "-r", "-y", ".oculto", "vlc\n"):
            with self.subTest(name=name):
                self.assertEqual(self.call("install_package", name=name).status, "error")
        self.assertEqual(self.pacman.calls, [])  # ni siquiera se ha preguntado a pacman
        self.call("install_package", name="vlc")
        for call in self.pacman.calls:
            self.assertEqual(call[call.index("--") + 1:], ["vlc"], call)  # justo después de «--», solo el nombre

    def test_search_words_are_never_taken_as_options(self):
        self.call("search_packages", query="--help")
        self.call("search_packages", query="-r vlc")
        self.assertEqual(self.pacman.calls, [["env", "LANG=C", "pacman", "-Ss", "--", "--help"],
                                             ["env", "LANG=C", "pacman", "-Ss", "--", "-r", "vlc"]])

    def test_install_rejections(self):
        self.assertIn("AUR", self.call("install_package", name="spotify").output)
        self.assertIn("ya está instalado", self.call("install_package", name="libvlc").output)
        self.assertEqual(self.call("install_package", name="vlc; reboot").status, "error")
        self.assertEqual(self.pacman.launched, [])


if __name__ == "__main__":
    unittest.main()
