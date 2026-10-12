"""La voz a trozos (0.56.0): la respuesta sale a frases según se escribe, sin saltarse la regla de honestidad.

Cerebro con guion que «escribe» a trozos: nada de Ollama.
"""

import unittest

from arthur.brain.base import BrainReply
from arthur.core.honesty import HONEST_REPLY, claims_action
from arthur.core.spoken import SpokenStream
from arthur.tools.base import ToolCall

from .test_honesty import OPEN_APP, LIST_APPS, assistant_with, opened


def stream_of(text, size=7):
    return [text[i:i + size] for i in range(0, len(text), size)]


class StreamBrain:
    """Como ScriptBrain, pero da el texto en trozos pequeños, como Ollama con stream."""
    name = "guion-a-trozos"

    def __init__(self, *replies):
        self.replies = list(replies)

    def reply(self, conversation):
        return self.replies.pop(0)

    def reply_stream(self, conversation, on_text):
        reply = self.replies.pop(0)
        for piece in stream_of(reply.text):
            on_text(piece)
        return reply


class SpokenStreamTest(unittest.TestCase):
    def make(self, may_say=lambda said: True):
        said = []
        return SpokenStream(said.append, may_say, clean=lambda t: t.replace(" #3", "")), said

    def test_sentences_come_out_as_soon_as_they_end(self):
        stream, said = self.make()
        for piece in stream_of("Mañana tienes dentista a las 10 #3. Luego, la clase de inglés. Y nada más"):
            stream.feed(piece)
        self.assertEqual(said, ["Mañana tienes dentista a las 10.", "Luego, la clase de inglés."])
        stream.finish("Mañana tienes dentista a las 10 #3. Luego, la clase de inglés. Y nada más")
        self.assertEqual(said[-1], "Y nada más")  # lo que faltaba, al final

    def test_the_first_piece_can_end_at_a_comma(self):
        stream, said = self.make()
        text = "En otoño Barcelona suele ser bastante suave, con días soleados y algo de lluvia, sobre todo en octubre. Fin"
        for piece in stream_of(text):
            stream.feed(piece)
        # el primero corta en la primera coma que deja 30 letras; luego, solo frases enteras
        self.assertEqual(said, ["En otoño Barcelona suele ser bastante suave,",
                                "con días soleados y algo de lluvia, sobre todo en octubre."])

    def test_short_bits_and_numbers_do_not_cut(self):
        stream, said = self.make()
        for piece in stream_of("Vale. Son 3.5 km hasta allí, más o menos. Fin"):
            stream.feed(piece)
        self.assertEqual(said, ["Vale. Son 3.5 km hasta allí, más o menos."])

    def test_a_claim_without_action_is_never_said(self):
        stream, said = self.make(may_say=lambda text: not claims_action(text))
        for piece in stream_of("Claro que sí. Abro Firefox ahora mismo. Ya está."):
            stream.feed(piece)
        self.assertEqual(said, ["Claro que sí."])  # se para en la frase que miente, y no sigue
        stream.finish(HONEST_REPLY)  # la respuesta definitiva es otra: entera
        self.assertEqual(said[-1], HONEST_REPLY)


class AssistantSpeechTest(unittest.TestCase):
    def setUp(self):
        opened.clear()

    def test_reply_is_spoken_in_sentences_and_returned_whole(self):
        text = "Hoy hace sol en Barcelona. Por la tarde puede llover un poco."
        said = []
        reply = assistant_with(StreamBrain(BrainReply(text))).handle_text("¿qué tiempo hace?", speak=False,
                                                                            on_speech=said.append)
        self.assertEqual(reply, text)
        self.assertEqual(said, ["Hoy hace sol en Barcelona.", "Por la tarde puede llover un poco."])

    def test_after_a_real_action_the_claim_is_said(self):
        said = []
        brain = StreamBrain(BrainReply("", (ToolCall("open_application", {"name": "firefox"}),)),
                            BrainReply("He abierto Firefox. Ya puedes usarlo."))
        assistant_with(brain, OPEN_APP).handle_text("abre firefox", speak=False, on_speech=said.append)
        self.assertEqual(opened, [{"name": "firefox"}])
        self.assertEqual(said, ["He abierto Firefox.", "Ya puedes usarlo."])

    def test_a_lie_is_not_spoken_and_the_honest_reply_is(self):
        said = []
        lie = BrainReply("Vale, te ayudo con eso. Abro Code::Blocks ahora.")
        brain = StreamBrain(BrainReply("", (ToolCall("list_applications", {}),)), lie, lie)
        reply = assistant_with(brain, LIST_APPS, OPEN_APP).handle_text("abre codeblocks", speak=False,
                                                                       on_speech=said.append)
        self.assertEqual(reply, HONEST_REPLY)
        self.assertFalse(any("Abro" in s for s in said))
        self.assertEqual(said[-1], HONEST_REPLY)
        self.assertEqual(opened, [])

    def test_without_streaming_brain_the_whole_reply_is_spoken_once(self):
        from .test_honesty import ScriptBrain

        said = []
        assistant_with(ScriptBrain(BrainReply("Son las cinco. Y diez."))).handle_text("hora", speak=False,
                                                                                    on_speech=said.append)
        self.assertEqual(said, ["Son las cinco. Y diez."])


if __name__ == "__main__":
    unittest.main()
