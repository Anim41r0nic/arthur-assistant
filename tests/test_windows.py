"""Tests de ventanas y escritorios. Hyprland FALSO: nunca se toca el escritorio real."""

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from arthur.brain.base import Message
from arthur.brain.rules import RuleBrain
from arthur.core.config import SecurityConfig
from arthur.core.events import EventBus
from arthur.core.router import ToolRouter
from arthur.integrations.hyprland import HyprlandActionError, HyprlandController, HyprlandReader, own_process_tree
from arthur.security.audit import AuditLog
from arthur.security.policy import Policy
from arthur.tools.base import ToolCall
from arthur.tools.registry import ToolRegistry
from arthur.tools.windows import build_window_tools

from .helpers import ScriptedConfirmer

WINDOWS = [
    {"address": "0xaaa1", "class": "firefox", "title": "Wikipedia", "pid": 100, "workspace": {"id": 1, "name": "1"}},
    {"address": "0xbbb2", "class": "kitty", "title": "~", "pid": 200, "workspace": {"id": 2, "name": "2"}},
    {"address": "0xccc3", "class": "Alacritty", "title": "arthur", "pid": 300, "workspace": {"id": 1, "name": "1"}},
    {"address": "0xddd4", "class": "Alacritty", "title": "htop", "pid": 400, "workspace": {"id": 3, "name": "3"}},
]


class FakeHyprland:
    """Imita el socket de Hyprland: responde consultas y apunta las órdenes "eval"."""

    def __init__(self, directory):
        self.path = Path(directory) / ".socket.sock"
        self.evals, self.eval_answer, self.windows = [], "ok", list(WINDOWS)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        self.server.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                request = conn.recv(65536).decode()
                if request.startswith("/eval "):
                    self.evals.append(request[6:])
                    answer = self.eval_answer
                elif request == "j/clients":
                    answer = json.dumps(self.windows)
                elif request == "j/workspaces":
                    answer = json.dumps([{"id": 1, "name": "1"}, {"id": 2, "name": "2"}, {"id": 3, "name": "3"}])
                elif request == "j/activeworkspace":
                    answer = json.dumps({"id": 1})
                else:
                    answer = "unknown request"
                conn.sendall(answer.encode())

    def close(self):
        self.server.close()


class WindowToolsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hypr = FakeHyprland(tmp.name)
        self.addCleanup(self.hypr.close)
        registry = ToolRegistry()
        # El PID 300 hace de "terminal donde se ejecuta ARTHUR".
        for tool in build_window_tools(HyprlandReader(self.hypr.path), HyprlandController(self.hypr.path),
                                       protected_pids=lambda: {300}):
            registry.register(tool)
        self.confirmer = ScriptedConfirmer()
        self.router = ToolRouter(registry, Policy(SecurityConfig()), self.confirmer, AuditLog(None), EventBus())

    def call(self, tool, **args):
        return self.router.execute(ToolCall(tool, args))

    def test_list_workspaces(self):
        out = self.call("list_workspaces").output
        self.assertIn("Escritorio 1: firefox «Wikipedia», Alacritty «arthur» ← activo", out)
        self.assertIn("Escritorio 2: kitty «~»", out)
        self.assertEqual(self.hypr.evals, [])

    def test_switch_workspace_uses_the_fixed_template(self):
        self.assertEqual(self.call("switch_workspace", number=3).status, "ok")
        self.assertEqual(self.hypr.evals, ['hl.dispatch(hl.dsp.focus({ workspace = "3" }))'])
        self.assertEqual(self.confirmer.requests, [])
        for bad in (0, 11, "3; hl.exec_cmd('rm -rf ~')"):
            with self.subTest(bad=bad):
                self.assertEqual(self.call("switch_workspace", number=bad).status, "error")
        self.assertEqual(len(self.hypr.evals), 1)

    def test_window_actions_check_the_window_inside_hyprland(self):
        self.assertEqual(self.call("focus_window", app="Firefox").status, "ok")
        self.assertEqual(
            self.hypr.evals[-1],
            'local w = hl.get_window("address:0xaaa1") if w == nil then error("ARTHUR:no-window", 0) end '
            "hl.dispatch(hl.dsp.focus({ window = w }))",
        )
        self.assertEqual(self.call("move_window", app="kitty", to_workspace=4).status, "ok")
        self.assertIn('hl.get_window("address:0xbbb2")', self.hypr.evals[-1])
        self.assertIn('hl.dsp.window.move({ window = w, workspace = "4", follow = false })', self.hypr.evals[-1])

    def test_model_text_never_reaches_hyprland(self):
        self.call("focus_window", app='firefox") hl.exec_cmd("rm -rf ~") -- ')
        self.assertEqual(self.hypr.evals, [])  # no hay ventana con ese nombre: ni se intenta

    def test_ambiguous_and_missing(self):
        result = self.call("focus_window", app="alacritty")
        self.assertEqual(result.status, "error")
        self.assertIn("varias ventanas", result.output)
        self.assertEqual(self.call("focus_window", app="alacritty", workspace=3).status, "ok")
        self.assertIn("0xddd4", self.hypr.evals[-1])
        self.assertIn("No hay ninguna ventana", self.call("focus_window", app="gimp").output)

    def test_close_needs_confirmation(self):
        self.assertEqual(self.call("close_window", app="kitty").status, "cancelled")
        self.assertEqual(self.hypr.evals, [])
        self.assertIn("Cerrar la ventana: kitty", self.confirmer.requests[0].summary)
        self.confirmer.answers = [True]
        self.assertEqual(self.call("close_window", app="kitty").status, "ok")
        self.assertIn("hl.dsp.window.close({ window = w })", self.hypr.evals[-1])
        self.assertNotIn("kill", self.hypr.evals[-1])

    def test_cannot_close_its_own_terminal(self):
        result = self.call("close_window", app="alacritty", workspace=1)
        self.assertEqual(result.status, "denied")
        self.assertIn("me estoy ejecutando", result.output)
        self.assertEqual(self.confirmer.requests, [])
        self.assertEqual(self.hypr.evals, [])

    def test_window_vanished_at_the_last_moment(self):
        self.hypr.eval_answer = "error: ARTHUR:no-window"
        result = self.call("focus_window", app="firefox")
        self.assertEqual(result.status, "error")
        self.assertIn("ya no existe: no he hecho nada", result.output)


class ControllerValidationTest(unittest.TestCase):
    def test_invalid_addresses_and_workspaces_are_never_sent(self):
        controller = HyprlandController(Path("/no/existe.sock"))
        for address in ("activewindow", "0xZZ", '0x1") hl.exec_cmd("x', "address:0x1", "", "0xaaa1\n"):
            with self.subTest(address=address), self.assertRaises(HyprlandActionError):
                controller.close_window(address)
        for workspace in (0, 11, "2", True):
            with self.subTest(workspace=workspace), self.assertRaises(HyprlandActionError):
                controller.switch_workspace(workspace)

    def test_orb_templates(self):
        controller = HyprlandController(Path("/no/existe.sock"))
        for action in (lambda a: controller.set_fullscreen(a, True), controller.hide_window):
            with self.assertRaises(HyprlandActionError):
                action('0x1") hl.exec_cmd("x')
        sent = []
        controller._eval = sent.append
        controller.set_fullscreen("0xa1", True)
        controller.set_fullscreen("0xa1", False)
        controller.hide_window("0xa1")
        guard = 'local w = hl.get_window("address:0xa1") if w == nil then error("ARTHUR:no-window", 0) end '
        self.assertEqual(sent, [
            guard + 'hl.dispatch(hl.dsp.window.fullscreen({ window = w, mode = "fullscreen", action = "set" }))',
            guard + 'hl.dispatch(hl.dsp.window.fullscreen({ window = w, mode = "fullscreen", action = "unset" }))',
            guard + 'hl.dispatch(hl.dsp.window.move({ window = w, workspace = "special:arthur", follow = false }))',
        ])

    def test_own_process_tree_includes_this_process(self):
        import os
        tree = own_process_tree()
        self.assertIn(os.getpid(), tree)
        self.assertIn(os.getppid(), tree)


class RulesTest(unittest.TestCase):
    def test_rules(self):
        brain = RuleBrain("ARTHUR")
        ask = lambda text: (lambda c: (c.name, dict(c.arguments)))(brain.reply([Message("user", text)]).tool_calls[0])  # noqa: E731
        self.assertEqual(ask("ve al escritorio 3"), ("switch_workspace", {"number": 3}))
        self.assertEqual(ask("manda firefox al escritorio 2"), ("move_window", {"app": "firefox", "to_workspace": 2}))
        self.assertEqual(ask("cierra kitty"), ("close_window", {"app": "kitty"}))


if __name__ == "__main__":
    unittest.main()
