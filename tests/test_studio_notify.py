"""Creator Studio: avisos por ARTHUR y la confirmación «PUBLICAR». Núcleo FALSO: nada sale a la red."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace

from arthur.studio import autopilot, notify

TOKEN = "arthur_" + "s" * 40


class FakeCore:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def open(self, request, timeout=None):
        if self.fail == "down":
            raise urllib.error.URLError("connection refused")
        if self.fail:
            raise urllib.error.HTTPError(request.full_url, self.fail, "x", {}, io.BytesIO(b"{}"))
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        self.sent.append((request.full_url, json.loads(request.data)["text"]))
        return io.BytesIO(b'{"ok": true}')


class NotifierTest(unittest.TestCase):
    def test_only_inside_the_private_network(self):
        for url in ("https://core:8770", "http://evil.example.com", "http://8.8.8.8:8770", "http://core:8770/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                notify.Notifier(url, TOKEN)
        with self.assertRaises(ValueError):
            notify.Notifier("http://core:8770", "corta")

    def test_sends_one_short_line_and_never_raises(self):
        core = FakeCore()
        n = notify.Notifier("http://core:8770", TOKEN, opener=core)
        self.assertTrue(n.send("Vídeo\nlisto  " + "x" * 400))
        url, text = core.sent[0]
        self.assertEqual(url, "http://core:8770/api/v1/notice")
        self.assertEqual(len(text), notify.MAX_TEXT)
        self.assertTrue(text.startswith("Vídeo listo x"))
        for fail in ("down", 403):
            with self.subTest(fail=fail), self.assertLogs("arthur.studio.notify", "WARNING"):
                self.assertFalse(notify.Notifier("http://core:8770", TOKEN, opener=FakeCore(fail)).send("hola"))

    def test_from_env_and_silent_without_config(self):
        self.assertIsNone(notify.Notifier.from_env({}))
        notify.send(None, "nada")       # sin avisos configurados: no pasa nada
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "llave"
            key.write_text(TOKEN + "\n")
            n = notify.Notifier.from_env({"ARTHUR_STUDIO_AVISOS_URL": "http://core:8770",
                                          "ARTHUR_STUDIO_AVISOS_TOKEN_FILE": str(key)})
            self.assertEqual(n.url, "http://core:8770/api/v1/notice")
            with self.assertLogs("arthur.studio.notify", "WARNING"):
                self.assertIsNone(notify.Notifier.from_env({"ARTHUR_STUDIO_AVISOS_URL": "http://core:8770",
                                                            "ARTHUR_STUDIO_AVISOS_TOKEN_FILE": str(key) + "x"}))
        core = FakeCore()
        notify.send(notify.Notifier("http://core:8770", TOKEN, opener=core), "")
        self.assertEqual(core.sent, [])  # nada que decir: no molesta


class JobNoticeTest(unittest.TestCase):
    def store(self, avisos):
        steps = [SimpleNamespace(name="guion", output={"titulo": "Séneca frente a la muerte", "avisos": avisos})]
        return SimpleNamespace(steps=lambda job_id: steps)

    def job(self, status, error=None, kind=autopilot.KIND):
        return SimpleNamespace(id=1, kind=kind, status=status, error=error)

    def test_what_arthur_says_when_the_autopilot_ends(self):
        doubts = autopilot.job_notice(self.store(5), self.job("done"))
        self.assertIn("5 dudas", doubts)
        self.assertIn("no se publica solo", doubts)
        clean = autopilot.job_notice(self.store(0), self.job("done"), publish_hour=18)
        self.assertIn("Se publica solo a las 18:00", clean)
        self.assertIn("ha fallado (Gemini saturado)", autopilot.job_notice(self.store(0), self.job("failed", "Gemini saturado")))
        for job in (self.job("running"), self.job("queued"), self.job("done", kind="investigar")):
            self.assertIsNone(autopilot.job_notice(self.store(0), job))


if __name__ == "__main__":
    unittest.main()
