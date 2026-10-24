"""El chat entrega los contactos al registro de clientes (webchat/clientes_link.py). Sin red: un «opener» falso hace
de servicio `clientes`, y la cola vive en una carpeta temporal."""

import io
import json
import logging
import os
import sqlite3
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path

from arthur.webchat.clientes_link import ClientesLink, ClientesRejected, migrate_old_contacts, notify

TOKEN = "arthur_" + "LlaveSecretaDeWebchat" * 3
CONTACTO = {"name": "Nombre Raro Prueba", "email": "raro.prueba@example.org", "phone": "+34 600 11 22 33",
            "need": "Necesito algo muy concreto XYZ", "lang": "es"}


class FakeClientes:
    """Hace de `POST /api/interno/contacto`: apunta lo que recibe y contesta lo que le digan."""

    def __init__(self):
        self.requests = []
        self.answers = []   # lista de: dict (200), int (error HTTP), Exception (red caída)

    def open(self, req, timeout=None):
        self.requests.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout,
                              "auth": req.get_header("Authorization"), "ctype": req.get_header("Content-type"),
                              "body": json.loads(req.data)})
        answer = self.answers.pop(0) if self.answers else {"id": 1, "nuevo": True}
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "error", {},
                                         io.BytesIO(json.dumps({"error": "x", "field": "phone"}).encode()))
        return io.BytesIO(json.dumps(answer).encode())


class LinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.queue = Path(tmp.name) / "cola"
        self.fake = FakeClientes()
        self.link = ClientesLink("http://clientes:8777", TOKEN, self.queue, opener=self.fake)

    def queued(self):
        return sorted(self.queue.glob("*.json")) if self.queue.exists() else []

    def test_deliver_ok_returns_id(self):
        self.fake.answers = [{"id": 12, "nuevo": False}]
        self.assertEqual(self.link.deliver(CONTACTO), (12, False))
        r = self.fake.requests[0]
        self.assertEqual((r["url"], r["method"], r["timeout"]), ("http://clientes:8777/api/interno/contacto", "POST", 5))
        self.assertEqual(r["auth"], "Bearer " + TOKEN)
        self.assertEqual(r["ctype"], "application/json")
        self.assertEqual(r["body"], CONTACTO)
        self.assertEqual(self.queued(), [])

    def test_only_internal_urls(self):
        for url in ("https://clientes.example.com", "http://8.8.8.8:8777", "ftp://clientes"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ClientesLink(url, TOKEN, self.queue, opener=self.fake)
        with self.assertRaises(ValueError):
            ClientesLink("http://clientes:8777", "corta", self.queue, opener=self.fake)

    def test_deliver_fails_queues_file_600(self):
        for answer in (urllib.error.URLError("caído"), TimeoutError("lento"), 500, 503, 401, 403,
                       {"id": "12", "nuevo": True}, {"id": 3, "nuevo": "si"}, {"id": True, "nuevo": True}):
            with self.subTest(answer=answer):
                before = len(self.queued())
                self.fake.answers = [answer]
                with self.assertLogs("arthur.webchat", logging.WARNING):
                    self.assertIsNone(self.link.deliver(CONTACTO))
                files = self.queued()
                self.assertEqual(len(files), before + 1)
        for f in self.queued():
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
            self.assertEqual(json.loads(f.read_text(encoding="utf-8")), CONTACTO)
            self.assertRegex(f.name, r"^[0-9a-f]{32}\.json$")
        self.assertEqual(stat.S_IMODE(self.queue.stat().st_mode), 0o700)
        self.assertEqual([p for p in self.queue.iterdir() if not p.name.endswith(".json")], [])  # sin .tmp sueltos

    def test_rejected_400_is_not_queued(self):
        # Un dato que `clientes` rechaza no se arregla reintentando: se le dice a la visita qué campo está mal.
        self.fake.answers = [400]
        with self.assertRaises(ClientesRejected) as cm:
            self.link.deliver(CONTACTO)
        self.assertEqual(cm.exception.field, "phone")
        self.assertEqual(self.queued(), [])

    def test_flush_delivers_and_removes(self):
        self.fake.answers = [urllib.error.URLError("caído"), urllib.error.URLError("caído")]
        with self.assertLogs("arthur.webchat", logging.WARNING):
            self.link.deliver(CONTACTO)
            self.link.deliver(dict(CONTACTO, email="otra@example.org"))
        self.assertEqual(len(self.queued()), 2)
        # Sigue caído: no se pierde nada.
        self.fake.answers = [urllib.error.URLError("caído")]
        with self.assertLogs("arthur.webchat", logging.WARNING):
            self.assertEqual(self.link.flush(), 0)
        self.assertEqual(len(self.queued()), 2)
        # Vuelve: se entregan los dos, se borran y se avisa de cada uno.
        self.fake.answers = [{"id": 4, "nuevo": True}, {"id": 5, "nuevo": False}]
        delivered = []
        self.assertEqual(self.link.flush(lambda i, nuevo: delivered.append((i, nuevo))), 2)
        self.assertEqual(self.queued(), [])
        self.assertEqual(sorted(delivered), [(4, True), (5, False)])

    def test_corrupt_queue_file_skipped_not_blocking(self):
        self.queue.mkdir(mode=0o700)
        (self.queue / ("0" * 32 + ".json")).write_text("{no es json", encoding="utf-8")
        (self.queue / ("1" * 32 + ".json")).write_text('["lista"]', encoding="utf-8")
        self.fake.answers = [urllib.error.URLError("caído")]
        with self.assertLogs("arthur.webchat", logging.WARNING):
            self.link.deliver(CONTACTO)
        self.fake.answers = [{"id": 7, "nuevo": True}]
        with self.assertLogs("arthur.webchat", logging.WARNING):
            self.assertEqual(self.link.flush(), 1)
        self.assertEqual(self.queued(), [])
        # Los rotos se apartan (no se reintentan cada minuto) pero no se borran: Marc puede mirarlos.
        self.assertEqual(len(list(self.queue.glob("*.corrupto"))), 2)
        self.assertEqual(self.fake.requests[-1]["body"], CONTACTO)

    def test_queued_contact_rejected_later_is_set_aside(self):
        self.fake.answers = [urllib.error.URLError("caído")]
        with self.assertLogs("arthur.webchat", logging.WARNING):
            self.link.deliver(CONTACTO)
        self.fake.answers = [400]
        with self.assertLogs("arthur.webchat", logging.WARNING):
            self.assertEqual(self.link.flush(), 0)
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(list(self.queue.glob("*.rechazado"))), 1)

    def test_token_never_in_logs_or_errors(self):
        secret_bits = (TOKEN, CONTACTO["name"], CONTACTO["email"], CONTACTO["need"], "600 11 22 33")
        self.fake.answers = [urllib.error.URLError("fallo con " + TOKEN), 500, 400]
        with self.assertLogs("arthur.webchat", logging.DEBUG) as cm:
            logging.getLogger("arthur.webchat.prueba").info("arranque")
            self.link.deliver(CONTACTO)
            self.link.deliver(CONTACTO)
            try:
                self.link.deliver(CONTACTO)
            except ClientesRejected as e:
                error_text = str(e) + repr(e)
            self.fake.answers = [urllib.error.URLError(TOKEN)] * 2
            self.link.flush()
        todo = "\n".join(cm.output) + error_text + repr(self.link)
        for secret in secret_bits:
            self.assertNotIn(secret, todo)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "contactos.sqlite3"
        self.fake = FakeClientes()
        self.link = ClientesLink("http://clientes:8777", TOKEN, self.dir / "cola", opener=self.fake)

    def old_table(self, rows):
        db = sqlite3.connect(self.db)
        db.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                   "name TEXT NOT NULL, email TEXT NOT NULL, need TEXT NOT NULL, lang TEXT NOT NULL)")
        db.executemany("INSERT INTO contacts (created_at, name, email, need, lang) VALUES (?, ?, ?, ?, ?)", rows)
        db.commit()
        db.close()

    def test_migration_moves_old_rows_then_drops_table(self):
        self.old_table([("2026-10-16T12:00:00+02:00", "Ana", "ana@example.com", "Una web", "es"),
                        ("2026-10-16T13:00:00+02:00", "Joan", "joan@example.com", "Una app", "ca")])
        self.assertEqual(migrate_old_contacts(self.db, self.link), 2)
        # Pasan a la cola (que se entrega con el reintento) y el fichero viejo desaparece.
        self.assertFalse(self.db.exists())
        queued = [json.loads(f.read_text(encoding="utf-8")) for f in (self.dir / "cola").glob("*.json")]
        self.assertEqual(sorted(queued, key=lambda c: c["name"]), [
            {"name": "Ana", "email": "ana@example.com", "phone": "", "need": "Una web", "lang": "es"},
            {"name": "Joan", "email": "joan@example.com", "phone": "", "need": "Una app", "lang": "ca"}])
        self.assertEqual(self.link.flush(), 2)
        self.assertEqual(len(self.fake.requests), 2)

    def test_migration_without_old_file_or_table_does_nothing(self):
        self.assertEqual(migrate_old_contacts(self.db, self.link), 0)
        self.assertFalse(self.db.exists())   # no crea la base vieja de la nada
        sqlite3.connect(self.db).close()     # fichero sin la tabla
        self.assertEqual(migrate_old_contacts(self.db, self.link), 0)
        self.assertEqual(self.fake.requests, [])

    def test_migration_twice_is_harmless(self):
        self.old_table([("2026-10-16T12:00:00+02:00", "Ana", "ana@example.com", "Una web", "es")])
        self.assertEqual(migrate_old_contacts(self.db, self.link), 1)
        self.assertEqual(migrate_old_contacts(self.db, self.link), 0)
        self.assertEqual(len(list((self.dir / "cola").glob("*.json"))), 1)


class NotifyTest(unittest.TestCase):
    class Notifier:
        def __init__(self, answers):
            self.answers = list(answers)
            self.sent = []

        def send(self, text, **options):
            self.sent.append((text, options))
            return self.answers.pop(0)

    def test_notice_falls_back_to_plain_text_if_core_refuses_destination(self):
        # Un núcleo sin ARTHUR_CLIENTES_PUBLIC_URL rechaza el destino (503): Marc tiene que enterarse igual del contacto.
        n = self.Notifier([False, True])
        with self.assertLogs("arthur.webchat", logging.WARNING):
            notify(n, (12, True))
        self.assertEqual(n.sent, [("Cliente nuevo C-0012", {"destination": "clientes", "client": 12}),
                                  ("Cliente nuevo C-0012", {})])

    def test_notice_ok_sent_once(self):
        n = self.Notifier([True])
        notify(n, (3, False))
        self.assertEqual(n.sent, [("C-0003 ha vuelto a escribir", {"destination": "clientes", "client": 3})])
        notify(None, (3, False))   # sin avisos configurados, nada


if __name__ == "__main__":
    unittest.main()
