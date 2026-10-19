"""Avisos al móvil (fase S6): suscripciones, el core, arthur-push y el cifrado. Sin red de verdad: los servicios
de avisos (Apple, Google…) son funciones falsas y los servidores escuchan en 127.0.0.1.

Las pruebas de criptografía necesitan `cryptography` (solo está en el contenedor arthur-push): sin ella se saltan.
"""

import base64
import json
import os
import stat
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from arthur.api.core_api import CoreServer
from arthur.core.events import EventBus
from arthur.integrations.push import PushError, PushLink, PushNotifier, PushSubscriptions
from arthur.push.push_server import PushServer
from arthur.push.subscription import SubscriptionError, allowed_endpoint, b64url_encode, check_subscription
from arthur.security.devices import DeviceRegistry

try:
    import cryptography  # noqa: F401
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

P256DH = b64url_encode(b"\x04" + b"\x11" * 64)
AUTH = b64url_encode(b"\x22" * 16)
APPLE = "https://web.push.apple.com/QGuQyavXutnMH6"


def sub(endpoint=APPLE, p256dh=P256DH, auth=AUTH):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}, "expirationTime": None}


def b64(text):
    text = text.replace(" ", "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def http(url, path, body=None, token=None):
    req = urllib.request.Request(url + path, json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


class SubscriptionTest(unittest.TestCase):
    def test_the_real_push_services_pass(self):
        for url in (APPLE, "https://fcm.googleapis.com/fcm/send/abc:xyz",
                    "https://updates.push.services.mozilla.com/wpush/v2/gAAA",
                    "https://wns2-db5p.notify.windows.com/w/?token=BQYAAA"):
            with self.subTest(url=url):
                self.assertEqual(check_subscription(sub(url))["endpoint"], url)

    def test_anything_else_is_refused(self):
        # Lo importante: el servidor nunca debe llamar a la red de casa ni a sitios cualquiera (SSRF).
        for url in ("http://web.push.apple.com/x", "https://10.0.0.1/x", "https://localhost/x",
                    "https://web.push.apple.com.evil.com/x", "https://evil.com/web.push.apple.com",
                    "https://web.push.apple.com:8443/x", "https://user:pw@web.push.apple.com/x",
                    "https://notify.windows.com/x", "https://[::1]/x", "file:///etc/passwd", 42, None,
                    "https://web.push.apple.com/" + "a" * 2000):
            with self.subTest(url=url):
                self.assertFalse(allowed_endpoint(url))
                with self.assertRaises(SubscriptionError):
                    check_subscription(sub(url))

    def test_keys_must_have_the_right_size(self):
        for p256dh, auth in ((b64url_encode(b"\x04" * 10), AUTH), (P256DH, b64url_encode(b"x" * 8)),
                             (b64url_encode(b"\x05" + b"\x11" * 64), AUTH), ("¡no base64!", AUTH), (None, AUTH)):
            with self.subTest(p256dh=p256dh, auth=auth), self.assertRaises(SubscriptionError):
                check_subscription(sub(p256dh=p256dh, auth=auth))
        with self.assertRaises(SubscriptionError):
            check_subscription("no es un objeto")

    def test_only_the_needed_fields_are_kept(self):
        self.assertEqual(check_subscription({**sub(), "extra": "<script>"}),
                         {"endpoint": APPLE, "keys": {"p256dh": P256DH, "auth": AUTH}})


class FakeLink:
    def __init__(self, status=201, error=None):
        self.status, self.error, self.sent = status, error, []

    def public_key(self):
        return "BPUBLICA"

    def send(self, subscription, title, body, **_):
        if self.error:
            raise PushError(self.error)
        self.sent.append((subscription["endpoint"], title, body))
        return self.status


class StoreAndNotifierTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "push.json"
        self.subs = PushSubscriptions(self.path)
        self.devices = DeviceRegistry(Path(self.dir.name) / "devices.json")
        self.devices.add("iphone")
        self.devices.add("cachyos")

    def test_store_saves_privately_and_forgets(self):
        self.subs.set("iphone", sub())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(self.subs.all()["iphone"]["endpoint"], APPLE)
        with self.assertRaises(SubscriptionError):
            self.subs.set("cachyos", sub("https://10.0.0.1/x"))
        self.assertTrue(self.subs.remove("iphone"))
        self.assertFalse(self.subs.remove("iphone"))
        self.assertEqual(self.subs.all(), {})

    def test_a_broken_entry_does_not_hide_the_rest(self):
        self.path.write_text(json.dumps({"subs": {"iphone": sub(), "raro": {"endpoint": "https://evil.com"}}}))
        self.assertEqual(list(self.subs.all()), ["iphone"])

    def test_notifies_every_live_device(self):
        link = FakeLink()
        self.subs.set("iphone", sub())
        self.subs.set("cachyos", sub("https://fcm.googleapis.com/fcm/send/x"))
        self.assertEqual(PushNotifier(link, self.subs, self.devices).notify("ARTHUR", "Recordatorio: pan."), 2)
        self.assertEqual(sorted(e for e, _, _ in link.sent), ["https://fcm.googleapis.com/fcm/send/x", APPLE])
        link.sent.clear()
        PushNotifier(link, self.subs, self.devices).notify("ARTHUR", "solo a uno", only="iphone")
        self.assertEqual(link.sent, [(APPLE, "ARTHUR", "solo a uno")])

    def test_revoked_or_gone_subscriptions_are_removed(self):
        self.subs.set("iphone", sub())
        self.subs.set("viejo", sub())  # aparato ya revocado
        link = FakeLink(status=410)
        self.assertEqual(PushNotifier(link, self.subs, self.devices).notify("ARTHUR", "x"), 0)
        self.assertEqual([e for e, _, _ in link.sent], [APPLE])  # al revocado ni se le intenta
        self.assertEqual(self.subs.all(), {})

    def test_a_failure_never_raises(self):
        self.subs.set("iphone", sub())
        with self.assertLogs("arthur.integrations.push", "WARNING"):
            self.assertEqual(PushNotifier(FakeLink(error="sin red"), self.subs, self.devices).notify("A", "x"), 0)
        self.assertIn("iphone", self.subs.all())  # un fallo de red no borra la suscripción


class CoreApiPushTest(unittest.TestCase):
    def start(self, with_push=True):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        devices = DeviceRegistry(Path(self.dir.name) / "devices.json")
        self.key = devices.add("iphone")
        self.link = FakeLink()
        self.subs = PushSubscriptions(Path(self.dir.name) / "push.json")
        push = PushNotifier(self.link, self.subs, devices) if with_push else None
        server = CoreServer(bus=EventBus(), devices=devices, handle_text=lambda t: t, port=0, push=push)
        server.start()
        self.addCleanup(server.stop)
        self.url = f"http://127.0.0.1:{server.port}"

    def call(self, path, body=None):
        return http(self.url, path, body, self.key)

    def test_subscribe_sends_a_test_notice_to_that_device(self):
        self.start()
        who = self.call("/api/v1/whoami")[1]
        self.assertEqual((who["push"], who["push_on"]), (True, False))
        self.assertEqual(self.call("/api/v1/push/key"), (200, {"key": "BPUBLICA"}))
        self.assertEqual(self.call("/api/v1/push/subscribe", {"subscription": sub()}), (200, {"ok": True}))
        self.assertEqual(self.subs.all()["iphone"]["endpoint"], APPLE)
        self.assertTrue(self.call("/api/v1/whoami")[1]["push_on"])
        deadline = time.monotonic() + 3
        while not self.link.sent and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.link.sent, [(APPLE, "ARTHUR", "Avisos activados en este aparato ✔")])
        self.assertEqual(self.call("/api/v1/push/unsubscribe", {}), (200, {"ok": True, "removed": True}))
        self.assertEqual(self.subs.all(), {})

    def test_bad_subscription_is_refused(self):
        self.start()
        with self.assertLogs("arthur.api.core_api", "WARNING"):
            code, body = self.call("/api/v1/push/subscribe", {"subscription": sub("https://10.0.0.1/x")})
        self.assertEqual(code, 400)
        self.assertIn("servicio de avisos", body["error"])
        self.assertEqual(self.subs.all(), {})

    def test_without_arthur_push(self):
        self.start(with_push=False)
        self.assertEqual(self.call("/api/v1/whoami")[1]["push"], False)
        self.assertEqual(self.call("/api/v1/push/key")[0], 404)
        self.assertEqual(self.call("/api/v1/push/subscribe", {"subscription": sub()})[0], 503)

    def test_service_worker_is_served(self):
        self.start()
        with urllib.request.urlopen(self.url + "/sw.js", timeout=5) as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/javascript"))
            self.assertIn(b"showNotification", r.read())


class FakeVapid:
    public_key = "BVAPID"

    def authorization(self, endpoint, subject):
        return f"vapid t=firma-de-{endpoint.split('/')[2]}, k={self.public_key}"


class PushServerTest(unittest.TestCase):
    TOKEN = "t" * 40

    def start(self, send=None):
        self.delivered = []

        def fake_send(endpoint, body, headers):
            self.delivered.append((endpoint, body, headers))
            return 201

        server = PushServer(token=self.TOKEN, vapid=FakeVapid(), subject="mailto:a@example.com", port=0,
                            encrypt=lambda text, p256dh, auth: b"CIFRADO:" + text, send=send or fake_send)
        server.start()
        self.addCleanup(server.stop)
        self.url = f"http://127.0.0.1:{server.port}"

    def call(self, path, body=None, token=TOKEN):
        return http(self.url, path, body, token)

    def test_sends_encrypted_and_signed(self):
        self.start()
        self.assertEqual(self.call("/key"), (200, {"key": "BVAPID"}))
        code, body = self.call("/send", {"subscription": sub(), "title": "ARTHUR", "body": "Recordatorio: pan.",
                                         "ttl": 3600, "urgency": "high"})
        self.assertEqual((code, body), (200, {"status": 201}))
        endpoint, payload, headers = self.delivered[0]
        self.assertEqual(endpoint, APPLE)
        self.assertEqual(json.loads(payload.removeprefix(b"CIFRADO:")),
                         {"title": "ARTHUR", "body": "Recordatorio: pan."})
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertEqual((headers["TTL"], headers["Urgency"]), ("3600", "high"))
        self.assertEqual(headers["Authorization"], "vapid t=firma-de-web.push.apple.com, k=BVAPID")

    def test_needs_the_key(self):
        self.start()
        with self.assertLogs("arthur.push.push_server", "WARNING"):
            self.assertEqual(self.call("/key", token="otra" * 10)[0], 401)
        self.assertEqual(self.delivered, [])

    def test_refuses_bad_requests_before_sending(self):
        self.start()
        bad = [
            {"subscription": sub("https://10.0.0.1/x"), "title": "A", "body": "x"},
            {"subscription": sub(), "title": "", "body": "x"},
            {"subscription": sub(), "title": "A", "body": "x" * 2000},
            {"subscription": sub(), "title": "A", "body": "x", "ttl": -1},
            {"subscription": sub(), "title": "A", "body": "x", "ttl": True},
            {"subscription": sub(), "title": "A", "body": "x", "urgency": "ya"},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.call("/send", body)[0], 400)
        self.assertEqual(self.delivered, [])

    def test_network_failure_is_a_502(self):
        def broken(*_):
            raise OSError("sin red")

        self.start(send=broken)
        with self.assertLogs("arthur.push.push_server", "WARNING"):
            self.assertEqual(self.call("/send", {"subscription": sub(), "title": "A", "body": "x"})[0], 502)

    def test_needs_a_proper_subject_and_key(self):
        enc = lambda *a: b""  # noqa: E731
        with self.assertRaises(Exception):
            PushServer(token="corta", vapid=FakeVapid(), subject="mailto:a@b.c", port=0, encrypt=enc)
        with self.assertRaises(Exception):
            PushServer(token=self.TOKEN, vapid=FakeVapid(), subject="arthur", port=0, encrypt=enc)


class PushLinkTest(unittest.TestCase):
    def test_link_talks_to_arthur_push(self):
        delivered = []
        server = PushServer(token="k" * 40, vapid=FakeVapid(), subject="mailto:a@b.c", port=0,
                            encrypt=lambda t, p, a: t, send=lambda e, b, h: delivered.append(e) or 201)
        server.start()
        self.addCleanup(server.stop)
        link = PushLink(f"http://127.0.0.1:{server.port}", "k" * 40)
        self.assertEqual(link.public_key(), "BVAPID")
        self.assertEqual(link.send(sub(), "ARTHUR", "hola"), 201)
        self.assertEqual(delivered, [APPLE])
        with self.assertLogs("arthur.push.push_server", "WARNING"), self.assertRaises(PushError):
            PushLink(f"http://127.0.0.1:{server.port}", "x" * 40).public_key()

    def test_only_internal_addresses(self):
        for url in ("https://push.example.com", "http://8.8.8.8:8772"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                PushLink(url, "k" * 40)
        self.assertIsNone(PushLink.from_env({}))


@unittest.skipUnless(HAS_CRYPTO, "sin la biblioteca cryptography (solo está en el contenedor arthur-push)")
class CryptoTest(unittest.TestCase):
    def test_rfc8291_example(self):
        # El ejemplo oficial del RFC 8291 (sección 5 y apéndice A), byte a byte.
        from cryptography.hazmat.primitives.asymmetric import ec

        from arthur.push.webpush import encrypt

        as_private = ec.derive_private_key(int.from_bytes(b64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                           ec.SECP256R1())
        result = encrypt(b"When I grow up, I want to be a watermelon",
                         b64("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcx aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
                         b64("BTBZMqHH6r4Tts7J_aSIgg"), as_private=as_private, salt=b64("DGv6ra1nlYgDCS1FRnbzlw"))
        header = b64("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z 9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
                     "mlMoZIIgDll6e3vCYLocInmYWAmS6Tlz AC8wEqKK6PBru3jl7A8")
        ciphertext = b64("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrd irN6GcG7sFz1y1sqLgVi1VhjVkHsUoEs bI_0LpXMuGvnzQ")
        self.assertEqual(result, header + ciphertext)

    def test_fresh_keys_each_time(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from arthur.push.webpush import encrypt

        ua = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        a, b = encrypt(b"hola", ua, b"\x01" * 16), encrypt(b"hola", ua, b"\x01" * 16)
        self.assertNotEqual(a[:16], b[:16])   # sal distinta en cada aviso
        self.assertNotEqual(a, b)

    def test_vapid_signature_verifies_and_key_is_private(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        from arthur.push.webpush import Vapid

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "vapid.pem"
            vapid = Vapid.load_or_create(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(Vapid.load_or_create(path).public_key, vapid.public_key)  # la misma al reiniciar
        header = vapid.authorization(APPLE, "mailto:a@example.com", now=1_000_000)
        token, key = header.removeprefix("vapid t=").split(", k=")
        self.assertEqual(key, vapid.public_key)
        head, claims, signature = token.split(".")
        self.assertEqual(json.loads(b64(head)), {"typ": "JWT", "alg": "ES256"})
        self.assertEqual(json.loads(b64(claims)), {"aud": "https://web.push.apple.com",
                                                   "exp": 1_000_000 + 12 * 3600, "sub": "mailto:a@example.com"})
        raw = b64(signature)
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64(key))
        public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                      f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # lanza si la firma no vale


if __name__ == "__main__":
    unittest.main()
