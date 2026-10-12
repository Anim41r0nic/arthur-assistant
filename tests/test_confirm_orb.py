"""Tests de las confirmaciones desde el Orb: broker, elección de canal y la ruta /api/confirm."""

import http.client
import json
import threading
import time
import unittest
from unittest import mock

from arthur.api.server import OrbServer
from arthur.core.events import ALL, EventBus
from arthur.security import broker as broker_module
from arthur.security.broker import ConfirmationBroker, Pending, RoutingConfirmer
from arthur.security.confirm import ConfirmationRequest
from arthur.tools.base import Risk

REQUEST = ConfirmationRequest("write_file", Risk.WRITE, "Crear ~/Proyectos/notas.txt")


def ask_in_thread(broker):
    result = []
    thread = threading.Thread(target=lambda: result.append(broker.ask(REQUEST)))
    thread.start()
    return thread, result


def wait_pending(broker):
    deadline = time.monotonic() + 3
    while not broker.pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    return broker.pending()[0]


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe(ALL, self.events.append)
        self.broker = ConfirmationBroker(self.bus, timeout=2, has_clients=lambda: True)

    def test_answer_yes(self):
        thread, result = ask_in_thread(self.broker)
        item = wait_pending(self.broker)
        self.assertEqual((item["summary"], item["risk"]), ("Crear ~/Proyectos/notas.txt", "write"))
        self.assertGreaterEqual(len(item["id"]), 20)  # imposible de adivinar
        self.assertTrue(self.broker.answer(item["id"], True))
        thread.join()
        self.assertEqual(result, [True])
        self.assertEqual([e.type for e in self.events], ["confirmation_pending", "confirmation_resolved"])
        self.assertEqual(self.broker.pending(), [])

    def test_unknown_id_and_double_answer(self):
        thread, result = ask_in_thread(self.broker)
        item = wait_pending(self.broker)
        self.assertFalse(self.broker.answer("inventado", True))
        self.assertTrue(self.broker.answer(item["id"], False))
        self.assertFalse(self.broker.answer(item["id"], True))  # ya contestada: no se puede cambiar
        thread.join()
        self.assertEqual(result, [False])

    def test_two_simultaneous_answers_cannot_change_the_outcome(self):
        """Dos respuestas a la vez (dos peticiones HTTP del Orb): solo vale la primera. Antes se comprobaba
        «¿ya está contestada?» fuera del candado y las dos podían pasar; la segunda pisaba a la primera."""
        barrier = threading.Barrier(2, timeout=0.5)

        class MeetsTheOtherThread(threading.Event):
            def is_set(self):  # fuerza el cruce: las dos leen «no contestada» y solo entonces siguen
                value = super().is_set()
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                return value

        item = Pending("abc", REQUEST, done=MeetsTheOtherThread())
        self.broker._pending["abc"] = item
        outcomes = []
        threads = [threading.Thread(target=lambda v=v: outcomes.append((v, self.broker.answer("abc", v))))
                   for v in (False, True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        accepted = [value for value, ok in outcomes if ok]
        self.assertEqual(len(accepted), 1, outcomes)  # solo una respuesta se da por buena…
        self.assertEqual(item.answer, accepted[0])    # …y esa es la que cuenta

    def test_an_answer_that_arrives_as_the_wait_times_out_is_not_lost(self):
        """Si el Orb recibe «ok» (respuesta aceptada), ask() tiene que devolver esa respuesta: antes, si llegaba
        justo cuando caducaba la espera, el Orb creía que había dicho Sí y ARTHUR entendía un No."""
        accepted = []

        class AnswerArrivesAtTheDeadline(threading.Event):
            def wait(self, timeout=None):
                accepted.append(self_.broker.answer(self_.broker.pending()[0]["id"], True))
                return False  # la espera ya había terminado por tiempo…

        self_ = self
        real_pending = Pending
        with mock.patch.object(broker_module, "Pending",
                               lambda id, request: real_pending(id, request, done=AnswerArrivesAtTheDeadline())):
            result = self.broker.ask(REQUEST)
        self.assertEqual(accepted, [True])  # el Orb ha recibido «ok»…
        self.assertTrue(result)             # …y ARTHUR entiende un Sí

    def test_only_true_means_yes(self):
        thread, result = ask_in_thread(self.broker)
        self.broker.answer(wait_pending(self.broker)["id"], "true")
        thread.join()
        self.assertEqual(result, [False])

    def test_timeout_is_no(self):
        broker = ConfirmationBroker(self.bus, timeout=0.2, has_clients=lambda: True)
        self.assertFalse(broker.ask(REQUEST))
        self.assertEqual(self.events[-1].data["by"], "timeout")

    def test_without_orb_there_is_nobody_to_ask(self):
        broker = ConfirmationBroker(self.bus, has_clients=lambda: False)
        self.assertIsNone(broker.ask(REQUEST))
        self.assertEqual(self.events, [])


class RecordingTerminal:
    def __init__(self):
        self.asked = []

    def confirm(self, request):
        self.asked.append(request)
        return True


class RoutingTest(unittest.TestCase):
    def make(self, broker=None):
        self.terminal, self.said, self.notes = RecordingTerminal(), [], []
        return RoutingConfirmer(self.terminal, say=self.said.append, notify=self.notes.append, broker=broker)

    def from_other_thread(self, confirmer):
        result = []
        thread = threading.Thread(target=lambda: result.append(confirmer.confirm(REQUEST)))
        thread.start()
        return thread, result

    def test_terminal_requests_are_asked_in_the_terminal(self):
        broker = ConfirmationBroker(EventBus(), has_clients=lambda: True)
        self.assertTrue(self.make(broker).confirm(REQUEST))
        self.assertEqual(len(self.terminal.asked), 1)
        self.assertEqual(broker.pending(), [])

    def test_hotkey_requests_are_asked_in_the_orb(self):
        broker = ConfirmationBroker(EventBus(), timeout=2, has_clients=lambda: True)
        confirmer = self.make(broker)
        thread, result = self.from_other_thread(confirmer)
        broker.answer(wait_pending(broker)["id"], True)
        thread.join()
        self.assertEqual(result, [True])
        self.assertEqual(self.terminal.asked, [])
        self.assertIn("confirmación en el Orb", self.notes[0])

    def test_hotkey_without_orb_is_no(self):
        for broker in (None, ConfirmationBroker(EventBus(), has_clients=lambda: False)):
            with self.subTest(broker=broker):
                confirmer = self.make(broker)
                thread, result = self.from_other_thread(confirmer)
                thread.join()
                self.assertEqual(result, [False])
                self.assertIn("abre el Orb", self.said[-1])


class ConfirmEndpointTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.broker = ConfirmationBroker(self.bus, timeout=3, has_clients=lambda: True)
        self.server = OrbServer(self.bus, port=0, assistant_name="ARTHUR", get_state=lambda: "idle", broker=self.broker)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.origin = f"http://127.0.0.1:{self.server.port}"
        self.cookie = f"arthur_token={self.server.token}"

    def post(self, body, *, headers=None, path="/api/confirm"):
        default = {"Host": f"127.0.0.1:{self.server.port}", "Cookie": self.cookie, "Origin": self.origin,
                   "X-Arthur": "1", "Content-Type": "application/json"}
        default.update(headers or {})
        default = {k: v for k, v in default.items() if v is not None}
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn.request("POST", path, body=data, headers=default)
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def test_valid_answer(self):
        thread, result = ask_in_thread(self.broker)
        item = wait_pending(self.broker)
        self.assertEqual(self.post({"id": item["id"], "answer": True}), 200)
        thread.join()
        self.assertEqual(result, [True])

    def test_every_defense(self):
        thread, result = ask_in_thread(self.broker)
        item = wait_pending(self.broker)
        good = {"id": item["id"], "answer": True}
        cases = [
            ("sin cookie", good, {"Cookie": None}, 403),
            ("cookie falsa", good, {"Cookie": "arthur_token=falso"}, 403),
            ("cookie con texto no ASCII", good, {"Cookie": "arthur_token=é"}, 403),
            ("otra web (Origin)", good, {"Origin": "https://malvado.com"}, 403),
            ("sin Origin", good, {"Origin": None}, 403),
            ("sin X-Arthur", good, {"X-Arthur": None}, 403),
            ("formulario HTML", good, {"Content-Type": "application/x-www-form-urlencoded"}, 415),
            ("DNS rebinding", good, {"Host": f"malvado.com:{self.server.port}"}, 403),
            ("demasiado grande", b'{"id": "' + b"x" * 2000 + b'", "answer": true}', {}, 413),
            ("JSON roto", b"{no es json", {}, 400),
            ("answer no booleano", {"id": item["id"], "answer": "true"}, {}, 400),
            ("id inventado", {"id": "inventado", "answer": True}, {}, 404),
        ]
        for name, body, headers, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.post(body, headers=headers), expected)
        self.assertEqual(len(self.broker.pending()), 1)  # nada de lo anterior la ha contestado
        self.assertEqual(self.post(good), 200)
        thread.join()

    def test_token_in_url_is_not_enough_to_post(self):
        status = self.post({"id": "x", "answer": True}, headers={"Cookie": None},
                           path=f"/api/confirm?t={self.server.token}")
        self.assertEqual(status, 403)

    def test_pending_questions_are_sent_on_connect(self):
        thread, _ = ask_in_thread(self.broker)
        item = wait_pending(self.broker)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{self.server.port}", "Cookie": self.cookie})
        response = conn.getresponse()
        line = response.readline()
        while not line.startswith(b"data: "):
            line = response.readline()
        hello = json.loads(line[6:])
        self.assertEqual(hello["data"]["pending"][0]["id"], item["id"])
        conn.close()
        self.broker.answer(item["id"], False)
        thread.join()


if __name__ == "__main__":
    unittest.main()
