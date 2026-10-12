import unittest

from arthur.brain.base import BrainReply
from arthur.core.assistant import ERROR_REPLY, MAX_TOOL_ROUNDS, TOO_MANY_ROUNDS_REPLY, Assistant
from arthur.core.events import ALL, Event, EventBus
from arthur.core.state import InvalidTransition, State, StateMachine
from arthur.tools.base import Risk, Tool, ToolCall

from .helpers import make_router


class EventBusTest(unittest.TestCase):
    def test_delivers_to_type_and_wildcard_subscribers(self):
        bus, got = EventBus(), []
        bus.subscribe("a", lambda e: got.append(("a", e.type)))
        bus.subscribe(ALL, lambda e: got.append(("*", e.type)))
        bus.publish(Event("a"))
        bus.publish(Event("b"))
        self.assertEqual(got, [("a", "a"), ("*", "a"), ("*", "b")])

    def test_unsubscribe(self):
        bus, got = EventBus(), []
        unsubscribe = bus.subscribe("a", got.append)
        unsubscribe()
        bus.publish(Event("a"))
        self.assertEqual(got, [])

    def test_broken_subscriber_does_not_break_others(self):
        bus, got = EventBus(), []

        def broken(_):
            raise RuntimeError("boom")

        bus.subscribe("a", broken)
        bus.subscribe("a", got.append)
        with self.assertLogs("arthur.core.events", "ERROR"):
            bus.publish(Event("a"))
        self.assertEqual(len(got), 1)


class StateMachineTest(unittest.TestCase):
    def test_valid_transition_publishes_event(self):
        bus, got = EventBus(), []
        bus.subscribe("state_changed", got.append)
        sm = StateMachine(bus)
        sm.transition(State.THINKING)
        self.assertEqual(sm.state, State.THINKING)
        self.assertEqual(got[0].data, {"from": State.IDLE, "to": State.THINKING})

    def test_invalid_transition_is_rejected(self):
        sm = StateMachine(EventBus())
        sm.transition(State.ERROR)
        with self.assertRaises(InvalidTransition):
            sm.transition(State.SPEAKING)
        self.assertEqual(sm.state, State.ERROR)

    def test_same_state_is_a_no_op(self):
        bus, got = EventBus(), []
        bus.subscribe("state_changed", got.append)
        StateMachine(bus).transition(State.IDLE)
        self.assertEqual(got, [])


class EchoBrain:
    name = "echo"

    def __init__(self):
        self.conversations = []

    def reply(self, conversation):
        self.conversations.append(conversation)
        return BrainReply(f"eco: {conversation[-1].content}")


class FailingBrain:
    name = "failing"

    def reply(self, conversation):
        raise RuntimeError("cerebro roto")


class PingBrain:
    """Pide la herramienta `ping` y después informa del resultado."""

    name = "ping"

    def reply(self, conversation):
        last = conversation[-1]
        if last.role == "tool":
            return BrainReply(f"resultado: {last.content}")
        return BrainReply("voy", (ToolCall("ping", {}),))


class LoopingBrain:
    name = "looping"

    def reply(self, conversation):
        return BrainReply("otra vez", (ToolCall("ping", {}),))


PING = Tool("ping", "Responde pong", Risk.READ, run=lambda a: "pong")


def make_assistant(brain, history_limit=20):
    bus = EventBus()
    events = []
    bus.subscribe(ALL, events.append)
    router = make_router([PING], bus=bus)
    return Assistant(brain, router, bus, StateMachine(bus), history_limit), events


def states_of(events):
    return [e.data["to"] for e in events if e.type == "state_changed"]


class AssistantTest(unittest.TestCase):
    def test_reply_and_state_sequence(self):
        assistant, events = make_assistant(EchoBrain())
        self.assertEqual(assistant.handle_text("  hola  "), "eco: hola")
        self.assertEqual(states_of(events), [State.THINKING, State.SPEAKING, State.IDLE])

    def test_empty_input_is_ignored(self):
        assistant, events = make_assistant(EchoBrain())
        self.assertEqual(assistant.handle_text("   "), "")
        self.assertEqual(events, [])

    def test_history_is_passed_and_limited_by_turns(self):
        brain = EchoBrain()
        assistant, _ = make_assistant(brain, history_limit=1)
        for text in ("uno", "dos", "tres"):
            assistant.handle_text(text)
        self.assertEqual([m.content for m in brain.conversations[-1]], ["dos", "eco: dos", "tres"])

    def test_brain_failure_goes_through_error_and_back_to_idle(self):
        assistant, events = make_assistant(FailingBrain())
        with self.assertLogs("arthur.core.assistant", "ERROR"):
            self.assertEqual(assistant.handle_text("hola"), ERROR_REPLY)
        self.assertEqual(states_of(events), [State.THINKING, State.ERROR, State.IDLE])
        self.assertIn("error", [e.type for e in events])

    def test_tool_round_trip(self):
        assistant, events = make_assistant(PingBrain())
        self.assertEqual(assistant.handle_text("haz ping"), "resultado: pong")
        self.assertEqual(
            states_of(events),
            [State.THINKING, State.EXECUTING, State.THINKING, State.SPEAKING, State.IDLE],
        )
        self.assertIn("tool_finished", [e.type for e in events])

    def test_direct_reply_tool_skips_the_second_model_pass(self):
        class CountingBrain(PingBrain):
            calls = 0

            def reply(self, conversation):
                CountingBrain.calls += 1
                return super().reply(conversation)

        bus = EventBus()
        agenda = Tool("ping", "Agenda", Risk.READ, run=lambda a: "Viernes:\n  20:00 Cena #1", direct_reply=True)
        assistant = Assistant(CountingBrain(), make_router([agenda], bus=bus), bus, StateMachine(bus), 20)
        self.assertEqual(assistant.handle_text("agenda"), "Viernes:\n  20:00 Cena #1")
        self.assertEqual(CountingBrain.calls, 1)  # solo la que elige la herramienta
        self.assertEqual(assistant.state, State.IDLE)

    def test_direct_reply_is_spoken_without_the_number_tags(self):
        spoken = []

        class Speaker:
            def speak(self, text):
                spoken.append(text)

            def stop(self):
                pass

        bus = EventBus()
        agenda = Tool("ping", "Agenda", Risk.READ, run=lambda a: "20:00 Cena #1\n21:00 Cine #12", direct_reply=True)
        assistant = Assistant(PingBrain(), make_router([agenda], bus=bus), bus, StateMachine(bus), 20, speaker=Speaker())
        assistant.handle_text("agenda")
        self.assertEqual(spoken, ["20:00 Cena\n21:00 Cine"])

    def test_direct_reply_is_not_used_when_the_tool_fails(self):
        def boom(a):
            from arthur.tools.base import ToolError
            raise ToolError("sin conexión")

        bus = EventBus()
        agenda = Tool("ping", "Agenda", Risk.READ, run=boom, direct_reply=True)
        assistant = Assistant(PingBrain(), make_router([agenda], bus=bus), bus, StateMachine(bus), 20)
        self.assertEqual(assistant.handle_text("agenda"), "resultado: [ERROR] sin conexión")  # lo explica el modelo

    def test_tool_rounds_are_limited(self):
        assistant, events = make_assistant(LoopingBrain())
        self.assertEqual(assistant.handle_text("sin fin"), TOO_MANY_ROUNDS_REPLY)
        self.assertEqual([e.type for e in events].count("tool_finished"), MAX_TOOL_ROUNDS)
        self.assertEqual(assistant.state, State.IDLE)


if __name__ == "__main__":
    unittest.main()
