"""Tests del cerebro Ollama contra un servidor falso que imita su API."""

import json
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from arthur.brain.base import BrainError, Message
from arthur.brain.ollama import OllamaBrain, parse_tool_call, strip_thinking, to_ollama
from arthur.core.assistant import Assistant
from arthur.core.config import BrainConfig, ConfigError, SecurityConfig, config_from_dict
from arthur.core.events import EventBus
from arthur.core.state import StateMachine
from arthur.tools.base import Param, Risk, Tool, ToolCall

from .helpers import make_router


class FakeOllama:
    """Servidor HTTP local que responde con lo que le preparemos y guarda lo recibido."""

    def __init__(self):
        self.requests = []
        self.responses = []  # lista de (código, cuerpo)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append((self.command, self.path, body))
                code, payload = fake.responses.pop(0)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()  # bytes: a trozos
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, content="", tool_calls=None):
        self.responses.append((200, {"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


ECHO = Tool("echo", "Repite un texto", Risk.READ, run=lambda a: f"eco:{a['text']}",
            params=(Param("text", str, "Texto a repetir"),))


class OllamaBrainTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.close)
        self.brain = self.make_brain(self.fake.url)

    def make_brain(self, url, **cfg):
        return OllamaBrain(
            BrainConfig(provider="ollama", ollama_url=url, model="qwen3:4b", timeout=5, **cfg),
            SecurityConfig(read_roots=("~/Proyectos",), write_roots=("~/Proyectos",), command_allowlist=("df",)),
            "ARTHUR",
            [ECHO.json_schema()],
        )

    def test_overlapping_warm_ups_become_one_more(self):
        # Mientras precarga, llegan 3 aparatos: no se lanzan 3 precargas a la vez, sino UNA más al acabar.
        runs, release = [], threading.Event()

        def slow_warm():
            runs.append(1)
            release.wait(5)

        self.brain._warm_once = slow_warm
        self.brain.warm_up()
        for _ in range(3):
            self.brain.warm_up()
        release.set()
        deadline = time.monotonic() + 5
        while self.brain._warming and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(runs), 2)
        self.assertFalse(self.brain._warming)

    def test_plain_reply_and_request_format(self):
        self.fake.reply("  Hola, ¿qué tal?  ")
        reply = self.brain.reply([Message("user", "hola")])
        self.assertEqual((reply.text, reply.tool_calls), ("Hola, ¿qué tal?", ()))

        method, path, body = self.fake.requests[0]
        self.assertEqual((method, path), ("POST", "/api/chat"))
        self.assertEqual(body["model"], "qwen3:4b")
        self.assertFalse(body["stream"])
        self.assertFalse(body["think"])
        self.assertEqual(body["options"]["num_ctx"], 8192)
        system = body["messages"][0]
        self.assertEqual(system["role"], "system")
        for expected in ("ARTHUR", "~/Proyectos", "df", "/no_think"):
            self.assertIn(expected, system["content"])
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hola"})
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["function"]["name"], "echo")
        self.assertIn("properties", body["tools"][0]["function"]["parameters"])

    def test_streamed_reply_gives_the_text_as_it_comes(self):
        chunks = [{"message": {"role": "assistant", "content": "Hola, "}},
                  {"message": {"role": "assistant", "content": "¿qué tal?"}},
                  {"message": {"role": "assistant", "content": "", "tool_calls": [
                      {"function": {"name": "echo", "arguments": {"text": "x"}}}]}},
                  {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 5}]
        self.fake.responses.append((200, "\n".join(json.dumps(c) for c in chunks).encode() + b"\n"))
        pieces = []
        reply = self.brain.reply_stream([Message("user", "hola")], pieces.append)
        self.assertEqual(pieces, ["Hola, ", "¿qué tal?"])
        self.assertEqual(reply.text, "Hola, ¿qué tal?")
        self.assertEqual([c.name for c in reply.tool_calls], ["echo"])
        self.assertTrue(self.fake.requests[0][2]["stream"])

    def test_streamed_thinking_is_not_given_out(self):
        chunks = [{"message": {"content": "<think>mmm"}}, {"message": {"content": "</think>Hola"}}, {"done": True}]
        self.fake.responses.append((200, "\n".join(json.dumps(c) for c in chunks).encode()))
        pieces = []
        self.assertEqual(self.brain.reply_stream([Message("user", "hola")], pieces.append).text, "Hola")
        self.assertEqual(pieces, [])

    def test_streamed_error_is_explained(self):
        self.fake.responses.append((200, json.dumps({"error": "sin memoria"}).encode()))
        with self.assertRaisesRegex(BrainError, "sin memoria"):
            self.brain.reply_stream([Message("user", "hola")], lambda _: None)

    def test_timings_are_logged_without_any_text(self):
        self.fake.responses.append((200, {
            "message": {"role": "assistant", "content": "secreto"}, "prompt_eval_count": 4400,
            "prompt_eval_duration": 2_000_000_000, "eval_count": 30, "eval_duration": 3_000_000_000,
            "load_duration": 100_000_000, "total_duration": 5_200_000_000}))
        with self.assertLogs("arthur.brain.ollama", level="INFO") as logs:
            self.brain.reply([Message("user", "hola")])
        line = logs.output[0]
        self.assertIn("4400 tokens de entrada (2.0 s), 30 de salida (3.0 s), carga 0.1 s, total 5.2 s", line)
        self.assertNotIn("secreto", line)

    def test_system_prompt_is_stable_for_the_cache(self):
        self.fake.reply("a")
        self.fake.reply("b")
        self.brain.reply([Message("user", "uno")])
        self.brain.reply([Message("user", "dos")])
        first, second = (r[2]["messages"][0] for r in self.fake.requests)
        self.assertEqual(first, second)

    def test_thinking_is_removed(self):
        self.assertEqual(strip_thinking("<think>mmm</think>\n\nHola"), "Hola")
        self.assertEqual(strip_thinking("Okay, the user...\n</think>\n\nHola"), "Hola")
        self.assertEqual(strip_thinking("  Hola  "), "Hola")

    def test_user_messages_carry_their_timestamp(self):
        messages = to_ollama([Message("user", "¿qué hora es?", timestamp=datetime(2026, 10, 4, 9, 5))])
        self.assertEqual(messages[0]["content"], "[domingo 4 de octubre de 2026, 09:05] ¿qué hora es?")

    def test_tool_calls_are_parsed(self):
        self.fake.reply("", [
            {"function": {"name": "echo", "arguments": {"text": "a"}}},
            {"function": {"name": "echo", "arguments": '{"text": "b"}'}},
        ])
        calls = self.brain.reply([Message("user", "x")]).tool_calls
        self.assertEqual([(c.name, dict(c.arguments)) for c in calls], [("echo", {"text": "a"}), ("echo", {"text": "b"})])

    def test_broken_tool_arguments_are_kept_for_the_router_to_reject(self):
        call = parse_tool_call({"function": {"name": "echo", "arguments": "{roto"}})
        self.assertEqual(dict(call.arguments), {"_texto_no_valido": "{roto"})

    def test_conversation_conversion(self):
        call = ToolCall("echo", {"text": "a"})
        messages = to_ollama([
            Message("user", "hola"),
            Message("assistant", "voy", tool_calls=(call,)),
            Message("tool", "eco:a", tool_call_id=call.id),
            Message("assistant", "hecho"),
        ])
        self.assertEqual(messages[1]["tool_calls"], [{"function": {"name": "echo", "arguments": {"text": "a"}}}])
        self.assertEqual(messages[2], {"role": "tool", "content": "eco:a", "tool_name": "echo"})
        self.assertEqual(messages[3], {"role": "assistant", "content": "hecho"})

    def test_missing_model(self):
        self.fake.responses.append((404, {"error": "model 'qwen3:4b' not found"}))
        with self.assertRaisesRegex(BrainError, "ollama pull qwen3:4b"):
            self.brain.reply([Message("user", "x")])

    def test_server_error(self):
        self.fake.responses.append((500, {"error": "sin memoria"}))
        with self.assertRaisesRegex(BrainError, "500: sin memoria"):
            self.brain.reply([Message("user", "x")])

    def test_ollama_not_running(self):
        brain = self.make_brain("http://127.0.0.1:9")  # puerto 9: nadie escucha
        with self.assertRaisesRegex(BrainError, "systemctl start ollama"):
            brain.reply([Message("user", "x")])

    def test_check(self):
        self.fake.responses.append((200, {"models": [{"name": "qwen3:4b"}]}))
        self.assertIsNone(self.brain.check())
        self.fake.responses.append((200, {"models": [{"name": "otro:1b"}]}))
        self.assertIn("ollama pull qwen3:4b", self.brain.check())
        self.assertIn("No puedo conectar", self.make_brain("http://127.0.0.1:9").check())

    def test_full_round_trip_through_assistant_and_router(self):
        self.fake.reply("", [{"function": {"name": "echo", "arguments": {"text": "hola"}}}])
        self.fake.reply("El eco dice hola.")
        bus = EventBus()
        assistant = Assistant(self.brain, make_router([ECHO], bus=bus), bus, StateMachine(bus), 20)

        self.assertEqual(assistant.handle_text("repite hola"), "El eco dice hola.")
        second_request = self.fake.requests[1][2]["messages"]
        self.assertEqual(second_request[-1], {"role": "tool", "content": "eco:hola", "tool_name": "echo"})

    def test_unavailable_brain_gives_friendly_reply(self):
        brain = self.make_brain("http://127.0.0.1:9")
        bus = EventBus()
        assistant = Assistant(brain, make_router([]), bus, StateMachine(bus), 20)
        with self.assertLogs("arthur.core.assistant", "INFO"):
            reply = assistant.handle_text("hola")
        self.assertTrue(reply.startswith("No puedo pensar ahora mismo: No puedo conectar con Ollama"))


class OllamaConfigTest(unittest.TestCase):
    def test_only_this_computer_is_allowed(self):
        # «ollama» sin puntos: el servicio de la red interna de Docker (arthur-core en el servidor).
        for url in ("http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434/", "http://ollama:11434"):
            with self.subTest(url=url):
                config_from_dict({"brain": {"ollama_url": url}})
        for url in ("http://10.0.0.20:11434", "https://127.0.0.1:11434", "http://ollama.com", "http://127.0.0.1:11434/x",
                    "http://ollama.lan:11434", "http://100.64.1.2:11434", "http://ollama:11434/api"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                config_from_dict({"brain": {"ollama_url": url}})

    def test_other_limits(self):
        for raw in ({"provider": "skynet"}, {"model": " "}, {"context_tokens": 100}, {"timeout": 1}, {"think": "no"}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                config_from_dict({"brain": raw})


if __name__ == "__main__":
    unittest.main()
