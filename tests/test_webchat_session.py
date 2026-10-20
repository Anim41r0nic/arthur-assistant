import base64
import io
import json
import unittest

from arthur.webchat.session import Passes, Turnstile


class PassesTest(unittest.TestCase):
    def setUp(self):
        self.t = 1_000_000.0
        self.p = Passes(b"secreto", lambda: self.t)

    def test_valid(self):
        token, exp = self.p.issue("1.2.3.4")
        self.assertEqual(exp, int(self.t) + 3600)
        self.assertTrue(self.p.check(token, "1.2.3.4"))

    def test_expired(self):
        token, _ = self.p.issue("1.2.3.4")
        self.t += 3601
        self.assertFalse(self.p.check(token, "1.2.3.4"))

    def test_other_ip(self):
        token, _ = self.p.issue("1.2.3.4")
        self.assertFalse(self.p.check(token, "5.6.7.8"))

    def test_tampered(self):
        token, _ = self.p.issue("1.2.3.4")
        datos, firma = token.split(".")
        self.assertFalse(self.p.check(datos + "." + firma[:-2] + "AA", "1.2.3.4"))
        # Cambiar la caducidad sin poder refirmar.
        falso = base64.urlsafe_b64encode(b"9999999999|" + b"0" * 16).rstrip(b"=").decode()
        self.assertFalse(self.p.check(falso + "." + firma, "1.2.3.4"))

    def test_other_secret(self):
        token, _ = self.p.issue("1.2.3.4")
        self.assertFalse(Passes(b"otro", lambda: self.t).check(token, "1.2.3.4"))

    def test_garbage(self):
        for basura in (None, 123, "sinpunto", "", "a.b", b"x.y", "!!.!!"):
            self.assertFalse(self.p.check(basura, "1.2.3.4"))


class Respuesta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class TurnstileTest(unittest.TestCase):
    def opener(self, cuerpo=None, error=None):
        self.llamadas = []

        def op(req, timeout=None):
            self.llamadas.append((req, timeout))
            if error:
                raise error
            return Respuesta(cuerpo)
        return op

    def test_true_false(self):
        t = Turnstile("s", self.opener(json.dumps({"success": True}).encode()))
        self.assertIs(t.verify("tok", "1.1.1.1"), True)
        req, timeout = self.llamadas[0]
        self.assertEqual(timeout, 10)
        self.assertEqual(req.full_url, "https://challenges.cloudflare.com/turnstile/v0/siteverify")
        self.assertIn(b"secret=s", req.data)
        self.assertIn(b"response=tok", req.data)
        t = Turnstile("s", self.opener(b'{"success": false}'))
        self.assertIs(t.verify("tok", "1.1.1.1"), False)

    def test_failures_are_none(self):
        self.assertIsNone(Turnstile("s", self.opener(error=OSError("caído"))).verify("t", "ip"))
        self.assertIsNone(Turnstile("s", self.opener(b"no json")).verify("t", "ip"))
        self.assertIsNone(Turnstile(None, self.opener(b'{"success": true}')).verify("t", "ip"))

    def test_truncated_answer_is_none(self):
        # Una respuesta cortada a medias (IncompleteRead, de la familia HTTPException, que NO es OSError) es
        # «Cloudflare no disponible» (503), no un fallo interno ni culpa de la visita.
        import http.client
        for error in (http.client.IncompleteRead(b"{"), http.client.RemoteDisconnected("cortado")):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(Turnstile("s", self.opener(error=error)).verify("t", "ip"))

    def test_bad_token_is_false_without_network(self):
        t = Turnstile("s", self.opener(b'{"success": true}'))
        self.assertIs(t.verify(None, "ip"), False)
        self.assertIs(t.verify("", "ip"), False)
        self.assertEqual(self.llamadas, [])


if __name__ == "__main__":
    unittest.main()
