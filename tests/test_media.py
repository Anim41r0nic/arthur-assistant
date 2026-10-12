"""Música: reproductor FALSO (nunca se llama al playerctl real)."""

import threading
import time
import unittest

from arthur.core.events import EventBus
from arthur.core.state import State, StateMachine
from arthur.integrations.media import MediaPlayer, MusicDucker
from arthur.tools.base import ToolError
from arthur.tools.media import build_media_tools


class FakePrograms:
    def __init__(self, status="Playing", players=True):
        self.status, self.players, self.calls = status, players, []

    def run(self, argv, **kw):
        assert argv[0] == "playerctl" and argv[1] == "--player=spotify,%any"
        args = argv[2:]
        if not self.players:
            raise ToolError("'playerctl' ha fallado: No players found")
        if args == ["status"]:
            return self.status + "\n"
        if args[0] == "metadata":
            return "spotify\tPlaying\tQueen\tBohemian Rhapsody\n"
        self.calls.append(args[0])
        self.status = {"pause": "Paused", "play": "Playing"}.get(args[0], self.status)
        return ""


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


class MediaToolTest(unittest.TestCase):
    def tool(self, programs):
        return build_media_tools(MediaPlayer(programs))[0]

    def test_actions_and_status(self):
        programs = FakePrograms()
        tool = self.tool(programs)
        self.assertEqual(tool.run({"action": "next"}), "Siguiente canción")
        self.assertEqual(tool.run({"action": "toggle"}), "Hecho")
        self.assertEqual(programs.calls, ["next", "play-pause"])
        self.assertEqual(tool.run({"action": "status"}), "spotify (sonando): Queen — Bohemian Rhapsody")

    def test_no_player_and_unknown_action(self):
        tool = self.tool(FakePrograms(players=False))
        with self.assertRaises(ToolError):
            tool.run({"action": "play"})
        from arthur.tools.base import ToolDenied
        with self.assertRaises(ToolDenied):
            tool.check({"action": "rm -rf"})


class AdjustAndSearchTest(unittest.TestCase):
    """Volumen del reproductor, saltos y búsqueda: órdenes FIJAS; del modelo solo viene el texto a buscar."""

    def setUp(self):
        self.programs = FakePrograms()
        self.launched = []
        self.programs.launch = lambda argv, **kw: self.launched.append(argv)
        self.media, self.search = build_media_tools(MediaPlayer(self.programs))

    def test_volume_and_seek_use_fixed_steps(self):
        calls = []
        self.programs.run = lambda argv, **kw: calls.append(argv[2:]) or ("Playing\n" if argv[2:] == ["status"] else "")
        for action in ("volume_up", "volume_down", "forward", "back"):
            self.media.run({"action": action})
        self.assertEqual([c for c in calls if c != ["status"]],
                         [["volume", "0.1+"], ["volume", "0.1-"], ["position", "10+"], ["position", "10-"]])

    def test_no_player_still_reports_error(self):
        media = build_media_tools(MediaPlayer(FakePrograms(players=False)))[0]
        with self.assertRaises(ToolError):
            media.run({"action": "volume_up"})

    def test_search_builds_encoded_urls(self):
        out = self.search.run({"query": "Queen & Bowie: Under Pressure/live", "service": "spotify"})
        self.assertIn("Spotify", out)
        self.search.run({"query": "lofi hip hop", "service": "youtube"})
        self.assertEqual(self.launched, [
            ["xdg-open", "spotify:search:Queen%20%26%20Bowie%3A%20Under%20Pressure%2Flive"],
            ["xdg-open", "https://www.youtube.com/results?search_query=lofi%20hip%20hop"],
        ])

    def test_search_rejects_bad_input(self):
        from arthur.tools.base import ToolDenied
        for args in ({"query": "x", "service": "file"}, {"query": "  ", "service": "spotify"},
                     {"query": "a\nb", "service": "spotify"}):
            with self.assertRaises(ToolDenied):
                self.search.check(args)
        self.assertEqual(self.launched, [])

    def test_search_failure_is_explained(self):
        def fail(argv, **kw):
            raise ToolError("código 3")
        self.programs.launch = fail
        with self.assertRaises(ToolError) as ctx:
            self.search.run({"query": "x", "service": "spotify"})
        self.assertIn("spotify-launcher", str(ctx.exception))


class DuckerTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.states = StateMachine(self.bus)
        self.programs = FakePrograms()
        self.player = MediaPlayer(self.programs)
        MusicDucker(self.player, self.bus)
        self.tool = build_media_tools(self.player)[0]

    def turn(self, during=None):
        self.states.transition(State.LISTENING)
        self.assertEqual(self.programs.status, "Paused")  # mientras te escucha, la música calla
        self.states.transition(State.THINKING)
        if during:
            self.tool.run({"action": during})
        self.states.transition(State.SPEAKING)
        self.states.transition(State.IDLE)

    def test_pauses_while_listening_and_resumes(self):
        self.turn()
        self.assertTrue(wait_for(lambda: self.programs.status == "Playing"))
        self.assertEqual(self.programs.calls, ["pause", "play"])

    def test_pause_request_keeps_it_paused(self):
        self.turn("pause")
        time.sleep(0.1)
        self.assertEqual(self.programs.status, "Paused")

    def test_next_is_applied_after_arthur_speaks(self):
        self.turn("next")
        self.assertTrue(wait_for(lambda: self.programs.calls[-2:] == ["next", "play"]))

    def test_outside_a_conversation_it_is_applied_at_once(self):
        """La palma de los gestos pausa con ARTHUR en reposo: no hay final de turno que esperar."""
        self.turn()
        self.assertTrue(wait_for(lambda: self.programs.status == "Playing"))
        self.programs.calls.clear()
        self.tool.run({"action": "pause"})
        self.assertEqual(self.programs.calls, ["pause"])
        self.assertEqual(self.programs.status, "Paused")

    def test_does_not_start_music_that_was_not_playing(self):
        self.programs.status = "Paused"
        self.states.transition(State.LISTENING)
        self.states.transition(State.IDLE)
        time.sleep(0.1)
        self.assertEqual(self.programs.calls, [])


if __name__ == "__main__":
    unittest.main()
