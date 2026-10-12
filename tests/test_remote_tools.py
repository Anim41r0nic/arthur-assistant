"""Fase S3: las herramientas de cada aparato, usadas desde el ARTHUR del servidor.

Todo en local y con herramientas FALSAS (nada toca el escritorio): un servidor HTTP en 127.0.0.1 y un «aparato»
que es este mismo proceso con su propio catálogo.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from arthur.api.core_api import CoreServer
from arthur.core.config import SecurityConfig
from arthur.core.events import EventBus
from arthur.core.remote_tools import RemoteToolHub, SpecError, build_remote_tool
from arthur.interfaces.core import LocalExecutor, RemoteCore, capability_specs
from arthur.security.audit import AuditLog
from arthur.security.policy import Policy
from arthur.tools.base import Param, Risk, Tool, ToolCall, ToolDenied, ToolError
from arthur.tools.registry import ToolRegistry
from tests.helpers import ScriptedConfirmer, make_router


def spec(name="move_window", risk="low", **extra):
    return {"name": name, "description": "Mueve una ventana.", "risk": risk,
            "input_schema": {"type": "object", "properties": {"app": {"type": "string"},
                                                              "workspace": {"type": "integer"}},
                             "required": ["app", "workspace"]}, **extra}


class RemoteSpecTest(unittest.TestCase):
    def test_dangerous_and_malformed_tools_are_rejected(self):
        bad = [spec(risk="dangerous"), spec(risk="root"), spec(name="Mal Nombre"), {**spec(), "description": ""},
               {**spec(), "input_schema": {"type": "array"}},
               {**spec(), "input_schema": {"type": "object", "properties": {"x": {"type": "object"}}}},
               spec(low_if={"param": "nope", "values": ["firefox"]}), "texto"]
        for s in bad:
            with self.subTest(s=str(s)[:50]), self.assertRaises(SpecError):
                build_remote_tool(s, run=lambda a: "")

    def test_risk_that_depends_on_the_arguments_travels_with_the_tool(self):
        """«Quién me ha escrito» es de solo mirar; con el TEXTO de los mensajes es privado y pide confirmación,
        igual en el servidor que en el portátil (antes ese «privado» se perdía al anunciarla)."""
        from arthur.integrations.notifications import NotificationWatcher
        from arthur.interfaces.core import capability_specs
        from arthur.core.config import SecurityConfig
        from arthur.tools import build_registry

        registry = build_registry(SecurityConfig(), profile="desktop", notifications=NotificationWatcher(lambda n: None))
        announced = next(s for s in capability_specs(registry) if s["name"] == "recent_messages")
        tool = build_remote_tool(announced, run=lambda a: "")
        self.assertEqual(tool.effective_risk(tool.validate({"include_text": True})), Risk.PRIVATE)
        self.assertEqual(tool.effective_risk(tool.validate({})), Risk.READ)

    def test_lower_if_can_only_lower(self):
        schema = {"type": "object", "properties": {"x": {"type": "boolean"}}}
        base = {"name": "algo", "description": "Algo.", "risk": "read", "input_schema": schema}
        for bad in ({"param": "x", "values": [False], "risk": "private"},   # subir: no
                    {"param": "x", "values": [False], "risk": "read"},      # igual: no tiene sentido
                    {"param": "y", "values": [False], "risk": "read"}, {"param": "x", "values": [], "risk": "read"},
                    {"param": "x", "values": [{"a": 1}], "risk": "read"}, "texto"):
            with self.subTest(bad=bad), self.assertRaises(SpecError):
                build_remote_tool({**base, "risk": "low" if bad != bad else base["risk"], "lower_if": bad},
                                  run=lambda a: "")
        tool = build_remote_tool({**base, "risk": "private", "lower_if": {"param": "x", "values": [False],
                                                                          "risk": "read"}}, run=lambda a: "")
        self.assertEqual(tool.effective_risk({"x": 0}), Risk.PRIVATE)  # 0 no es False: sin trampas de tipos
        self.assertEqual(tool.effective_risk({"x": False}), Risk.READ)

    def test_trusted_app_is_low_risk_like_on_the_laptop(self):
        s = {"name": "open_application", "description": "Abre una app.", "risk": "write",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             "low_if": {"param": "name", "values": ["firefox"]}}
        tool = build_remote_tool(s, run=lambda a: "")
        self.assertIs(tool.effective_risk({"name": "firefox"}), Risk.LOW)
        self.assertIs(tool.effective_risk({"name": "gparted"}), Risk.WRITE)


class HubTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(Tool("remember", "Recuerda algo.", Risk.WRITE, run=lambda a: "ok"))
        self.hub = RemoteToolHub(self.registry, timeout=2)
        self.sent = []

        def send(device, payload):
            self.sent.append((device, payload))
            data = payload["data"]
            threading.Thread(target=lambda: self.hub.resolve(device, data["id"], "ok",
                                                             f"{device}: {data['arguments']['app']}")).start()
            return True

        self.hub.send = send

    def test_brain_is_told_once_per_change(self):
        changes = []
        self.hub.on_change = lambda: changes.append(1)
        self.hub.register("cachyos", [spec()])
        self.assertEqual(len(changes), 1)
        self.hub.register("cachyos", [spec()])  # se reconecta con lo mismo: nada que precargar otra vez
        self.assertEqual(len(changes), 1)
        self.hub.register("cachyos", [spec(name="close_window")])  # con otras: sustituye lo suyo, un aviso
        self.assertEqual(len(changes), 2)
        self.hub.unregister_device("cachyos")
        self.assertEqual(len(changes), 3)
        self.hub.unregister_device("cachyos")  # ya no tenía nada: no cambia la lista, no avisa
        self.assertEqual(len(changes), 3)

    def test_a_short_disconnection_does_not_touch_the_brain(self):
        """El portátil se corta y vuelve (reinicio, suspensión, Tailscale): ni una precarga de más."""
        timers, changes = [], []
        self.hub._timer = lambda seconds, action: timers.append((seconds, action))
        self.hub.on_change = lambda: changes.append(1)
        self.hub.register("cachyos", [spec()])
        self.hub.disconnect("cachyos")
        self.assertIsNotNone(self.registry.get("move_window"))  # sigue en la lista del cerebro
        with self.assertRaisesRegex(ToolError, "«cachyos» no está conectado ahora mismo"):
            self.registry.get("move_window").run({"app": "x", "workspace": 1})
        self.assertEqual(self.sent, [])  # ni se intenta
        self.hub.register("cachyos", [spec()])  # vuelve con lo mismo
        timers[0][1]()  # la cuenta atrás vieja acaba: ya no hace nada
        self.assertEqual(changes, [1])
        self.assertEqual(self.registry.get("move_window").run({"app": "firefox", "workspace": 2}),
                         "cachyos: firefox")

    def test_a_device_that_does_not_come_back_loses_its_tools(self):
        timers, changes = [], []
        self.hub._timer = lambda seconds, action: timers.append((seconds, action))
        self.hub.on_change = lambda: changes.append(1)
        self.hub.register("cachyos", [spec()])
        self.hub.disconnect("cachyos")
        self.assertEqual(timers[0][0], 600)
        timers[0][1]()
        self.assertIsNone(self.registry.get("move_window"))
        self.assertEqual(changes, [1, 1])  # ahora sí: la lista cambió y el cerebro precarga
        self.hub.register("cachyos", [spec()])  # vuelve más tarde: es nuevo otra vez
        self.assertIsNotNone(self.registry.get("move_window"))
        self.assertEqual(len(changes), 3)

    def test_device_cannot_replace_a_server_tool(self):
        added, rejected = self.hub.register("cachyos", [spec(name="remember"), spec()])
        self.assertEqual(added, ["move_window"])
        self.assertIn("remember: ya es una herramienta del servidor", rejected)
        self.assertEqual(self.registry.get("remember").description, "Recuerda algo.")

    def test_call_goes_to_the_active_device_and_tools_leave_with_it(self):
        self.hub.register("cachyos", [spec()])
        self.hub.register("windows", [spec()])
        tool = self.registry.get("move_window")
        self.assertEqual(tool.run({"app": "firefox", "workspace": 2}), "windows: firefox")  # el último en llegar
        self.hub.set_active("cachyos")
        self.assertEqual(tool.run({"app": "firefox", "workspace": 2}), "cachyos: firefox")
        self.hub.unregister_device("cachyos")
        self.assertIsNotNone(self.registry.get("move_window"))  # windows aún la tiene
        self.hub.unregister_device("windows")
        self.assertIsNone(self.registry.get("move_window"))

    def test_only_the_asked_device_can_answer(self):
        self.hub.register("cachyos", [spec()])
        self.hub.send = lambda device, payload: self.sent.append(payload) or True
        result = []
        t = threading.Thread(target=lambda: result.append(self._run_quiet({"app": "x", "workspace": 1})))
        t.start()
        deadline = time.monotonic() + 2
        while not self.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        call_id = self.sent[0]["data"]["id"]
        self.assertFalse(self.hub.resolve("intruso", call_id, "ok", "hecho"))
        self.assertFalse(self.hub.resolve("cachyos", "otro-id", "ok", "hecho"))
        self.assertFalse(self.hub.resolve("cachyos", call_id, "quizá", "hecho"))
        self.assertTrue(self.hub.resolve("cachyos", call_id, "denied", "no"))
        t.join(2)
        self.assertEqual(result, ["DENIED"])

    def _run_quiet(self, args):
        try:
            return self.registry.get("move_window").run(args)
        except ToolDenied:
            return "DENIED"

    def test_unreachable_or_silent_device_is_an_error(self):
        self.hub.register("cachyos", [spec()])
        self.hub.send = lambda device, payload: False
        with self.assertRaises(ToolError):
            self.registry.get("move_window").run({"app": "x", "workspace": 1})
        self.hub.timeout = 0.1
        self.hub.send = lambda device, payload: True
        with self.assertRaises(ToolError):
            self.registry.get("move_window").run({"app": "x", "workspace": 1})


class LocalExecutorTest(unittest.TestCase):
    def setUp(self):
        self.ran = []
        registry = ToolRegistry()
        registry.register(Tool("volume", "Volumen.", Risk.LOW, params=(Param("level", int, "", minimum=0, maximum=100),),
                               run=lambda a: self.ran.append(a) or f"volumen {a['level']}"))
        registry.register(Tool("run_command", "Orden.", Risk.DANGEROUS, run=lambda a: self.ran.append(a) or "x"))

        def deny(a):
            raise ToolDenied("fuera de las carpetas permitidas")

        registry.register(Tool("write_file", "Escribe.", Risk.WRITE, run=lambda a: "x", check=deny))
        self.registry = registry
        self.executor = LocalExecutor(registry, Policy(SecurityConfig()), AuditLog(None))

    def test_device_validates_with_its_own_rules(self):
        self.assertEqual(self.executor.run("volume", {"level": 40}), ("ok", "volumen 40"))
        self.assertEqual(self.executor.run("volume", {"level": 400})[0], "error")
        self.assertEqual(self.executor.run("volume", {"level": "40; rm -rf"})[0], "error")
        self.assertEqual(self.executor.run("run_command", {})[0], "denied")  # lo peligroso, nunca a distancia
        self.assertEqual(self.executor.run("write_file", {}), ("denied", "fuera de las carpetas permitidas"))
        self.assertEqual(self.executor.run("no_existe", {})[0], "denied")
        self.assertEqual(self.ran, [{"level": 40}])

    def test_local_safe_mode_wins(self):
        executor = LocalExecutor(self.registry, Policy(SecurityConfig(safe_mode=True)), AuditLog(None))
        self.assertEqual(executor.run("volume", {"level": 40})[0], "denied")

    def test_dangerous_tools_are_never_announced(self):
        names = [s["name"] for s in capability_specs(self.registry)]
        self.assertIn("volume", names)
        self.assertNotIn("run_command", names)


class EndToEndTest(unittest.TestCase):
    """Servidor real en 127.0.0.1 + «aparato» con herramientas falsas: la orden va y vuelve por la red."""

    def test_server_uses_the_device_hands_and_forgets_them_when_it_leaves(self):
        with tempfile.TemporaryDirectory() as d:
            from arthur.security.devices import DeviceRegistry

            devices = DeviceRegistry(Path(d) / "devices.json")
            token = devices.add("cachyos")
            router = make_router([], confirmer=ScriptedConfirmer(True))
            hub = RemoteToolHub(router.registry, timeout=5, grace=0.2)  # «el rato» de espera, corto en la prueba
            server = CoreServer(bus=EventBus(), devices=devices, handle_text=lambda t: t, hub=hub,
                                host="127.0.0.1", port=0)
            server.start()
            moved = []
            local = ToolRegistry()
            local.register(Tool("move_window", "Mueve una ventana.", Risk.LOW,
                                params=(Param("app", str, ""), Param("workspace", int, "", minimum=1, maximum=10)),
                                run=lambda a: moved.append(a) or f"{a['app']} al escritorio {a['workspace']}"))
            executor = LocalExecutor(local, Policy(SecurityConfig()), AuditLog(None))
            core = RemoteCore(f"http://127.0.0.1:{server.port}", token)
            stop = threading.Event()

            def device():
                for event in core.events():
                    if event["type"] == "hello":
                        core.register(capability_specs(local))
                    elif event["type"] == "tool_call":
                        data = event["data"]
                        core.tool_result(data["id"], *executor.run(data["name"], data["arguments"]))
                    if stop.is_set():
                        return

            threading.Thread(target=device, daemon=True).start()
            try:
                deadline = time.monotonic() + 5
                while router.registry.get("move_window") is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                result = router.execute(ToolCall("move_window", {"app": "firefox", "workspace": 2}))
                self.assertEqual((result.status, result.output), ("ok", "firefox al escritorio 2"))
                self.assertEqual(moved, [{"app": "firefox", "workspace": 2}])
                bad = router.execute(ToolCall("move_window", {"app": "firefox", "workspace": 99}))
                self.assertEqual(bad.status, "error")  # lo frena el aparato con SUS límites (1-10)
                self.assertEqual(len(moved), 1)
            finally:
                stop.set()
                server.stop()  # corta la conexión de eventos: el aparato «se va»
            deadline = time.monotonic() + 5
            while router.registry.get("move_window") is not None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNone(router.registry.get("move_window"))


if __name__ == "__main__":
    unittest.main()
