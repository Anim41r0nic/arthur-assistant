import unittest

from arthur.integrations.hyprland import HyprlandActionError
from arthur.integrations.orb_window import OrbWindow, is_orb_window

ORB = {"address": "0xa1", "class": "firefox", "title": "ARTHUR — Mozilla Firefox"}
WORK = {"address": "0xb2", "class": "Alacritty", "title": "fish"}


class FakeReader:
    def __init__(self, clients, active=WORK, workspace=3):
        self.clients = list(clients)
        self.active, self.workspace = active, workspace

    def windows(self):
        return self.clients

    def query(self, request):
        return {"j/activewindow": self.active, "j/activeworkspace": {"id": self.workspace}}[request]


class FakeController:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __getattr__(self, name):
        def action(*args, **kwargs):
            if self.fail:
                raise HyprlandActionError("falla")
            self.calls.append((name, *args, *kwargs.values()))
        return action


def make(clients, *, url="http://127.0.0.1:8765/?t=x", controller=None, appear=None):
    reader = FakeReader(clients)
    launched = []

    def launch(argv):
        launched.append(argv)
        if appear:
            reader.clients.append(appear)

    window = OrbWindow(controller or FakeController(), reader, url=lambda: url, launch=launch,
                       sleep=lambda s: None)
    return window, launched


class OrbWindowTest(unittest.TestCase):
    def test_recognises_only_the_orb(self):
        self.assertTrue(is_orb_window(ORB))
        self.assertFalse(is_orb_window(WORK))
        self.assertFalse(is_orb_window({"class": "firefox", "title": "ARTHUR wiki - Mozilla Firefox"}))
        self.assertFalse(is_orb_window({"class": "kitty", "title": "ARTHUR"}))

    def test_existing_orb_comes_to_your_workspace_in_fullscreen(self):
        window, launched = make([WORK, ORB])
        window.show()
        self.assertEqual(launched, [])
        self.assertEqual(window.controller.calls, [
            ("move_window", "0xa1", 3, True), ("focus_window", "0xa1"), ("set_fullscreen", "0xa1", True)])

    def test_opens_a_new_firefox_window_if_there_is_no_orb(self):
        window, launched = make([WORK], appear=ORB)
        window.show()
        self.assertEqual(launched, [["firefox", "--new-window", "http://127.0.0.1:8765/?t=x"]])
        self.assertIn(("set_fullscreen", "0xa1", True), window.controller.calls)

    def test_never_opens_a_foreign_url(self):
        window, launched = make([WORK], url="https://example.com/")
        window.show()
        self.assertEqual(launched, [])
        self.assertEqual(window.controller.calls, [])

    def test_gives_up_if_the_window_never_appears(self):
        window, launched = make([WORK])
        window.show()
        self.assertEqual(len(launched), 1)
        self.assertEqual(window.controller.calls, [])

    def test_hide_returns_focus_to_your_window(self):
        window, _ = make([WORK, ORB])
        window.show()
        window.controller.calls.clear()
        window.hide()
        self.assertEqual(window.controller.calls, [
            ("set_fullscreen", "0xa1", False), ("hide_window", "0xa1"), ("focus_window", "0xb2")])

    def test_hyprland_errors_do_not_break_the_conversation(self):
        window, _ = make([WORK, ORB], controller=FakeController(fail=True))
        window.show()  # no lanza nada: solo lo registra
        window.hide()


if __name__ == "__main__":
    unittest.main()
