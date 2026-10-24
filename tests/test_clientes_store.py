"""Almacén de clientes (arthur/clientes/store.py): duplicados por correo o teléfono, estados, purga. Sin red."""

import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arthur.clientes.store import ClientError, ClientStore, normalize_email, normalize_phone, numero

AHORA = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)


class Reloj:
    def __init__(self):
        self.t = AHORA

    def __call__(self):
        return self.t


class NormalizeTests(unittest.TestCase):
    def test_email(self):
        self.assertEqual(normalize_email("  Ana@Example.COM "), "ana@example.com")
        self.assertEqual(normalize_email("   "), "")
        for malo in ("ana", "ana@", "a b@c.es", "a\x00b@c.es", "x" * 190 + "@example.com"):
            with self.assertRaises(ClientError) as cm:
                normalize_email(malo)
            self.assertEqual(cm.exception.field, "email")

    def test_phone(self):
        for variante in ("+34 600 11 22 33", "600-112-233", "0034600112233", "(600)112233", "600.11.22.33",
                         "600112233"):
            self.assertEqual(normalize_phone(variante), "34600112233", variante)
        self.assertEqual(normalize_phone("+44 7911 123456"), "447911123456")
        # El 34 escrito sin «+» delante de un número español de 9 cifras es el mismo número
        self.assertEqual(normalize_phone("34600112233"), "34600112233")
        self.assertEqual(normalize_phone("34 600 11 22 33"), "34600112233")
        self.assertEqual(normalize_phone(""), "")
        self.assertEqual(normalize_phone("  "), "")

    def test_bad_phone_raises(self):
        for malo in ("abc", "1234", "600 11 22", "6001122334455667", "60011223a", "+", "34+600112233",
                     "1" * 31):
            with self.assertRaises(ClientError, msg=malo) as cm:
                normalize_phone(malo)
            self.assertEqual(cm.exception.field, "phone")

    def test_numero_format(self):
        self.assertEqual(numero(1), "C-0001")
        self.assertEqual(numero(12345), "C-12345")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.reloj = Reloj()
        self.store = ClientStore(":memory:", self.reloj)

    def tearDown(self):
        self.store.close()

    def contacto(self, name="Ana Ruiz", email="ana@example.com", phone="", text="Quiero una web", lang="es"):
        return self.store.record_contact(name, email, phone, text, lang)

    def test_new_contact(self):
        cid, nuevo = self.contacto()
        self.assertTrue(nuevo)
        ficha = self.store.get(cid)
        self.assertEqual(ficha["numero"], "C-0001")
        self.assertEqual(ficha["estado"], "nuevo")
        self.assertEqual(ficha["origen"], "web")
        self.assertEqual(ficha["sin_leer"], 1)
        self.assertEqual(ficha["email"], "ana@example.com")
        self.assertEqual([m["texto"] for m in ficha["mensajes"]], ["Quiero una web"])
        self.assertEqual(ficha["mensajes"][0]["idioma"], "es")

    def test_numero_format(self):
        cid, _ = self.contacto()
        self.assertEqual(self.store.list()[0]["numero"], "C-0001")
        self.assertEqual(cid, 1)

    def test_same_email_case_and_spaces_same_client(self):
        a, _ = self.contacto(email="ana@example.com")
        b, nuevo = self.contacto(email="  ANA@Example.com ", text="Otra vez")
        self.assertEqual(a, b)
        self.assertFalse(nuevo)
        ficha = self.store.get(a)
        self.assertEqual(len(ficha["mensajes"]), 2)
        self.assertEqual(ficha["sin_leer"], 2)

    def test_phone_variants_same_client(self):
        a, _ = self.contacto(email="", phone="+34 600 11 22 33")
        for variante in ("600-112-233", "0034600112233", "(600)112233"):
            cid, nuevo = self.contacto(name="Otra", email="", phone=variante)
            self.assertEqual(cid, a, variante)
            self.assertFalse(nuevo)
        self.assertEqual(len(self.store.list()), 1)

    def test_bad_phone_raises_and_no_client(self):
        for malo in ("abc", "1234"):
            with self.assertRaises(ClientError) as cm:
                self.contacto(phone=malo)
            self.assertEqual(cm.exception.field, "phone")
        self.assertEqual(self.store.list(), [])

    def test_validation_fields(self):
        casos = [
            (dict(name="  "), "name"), (dict(name="x" * 101), "name"),
            (dict(email="", phone=""), "contact"), (dict(email="no-es-correo"), "email"),
            (dict(text=""), "text"), (dict(text="x" * 2001), "text"),
        ]
        for extra, campo in casos:
            with self.assertRaises(ClientError, msg=campo) as cm:
                self.contacto(**extra)
            self.assertEqual(cm.exception.field, campo)
            self.assertNotIn("x" * 50, str(cm.exception))  # el error no repite lo escrito
        with self.assertRaises(ClientError) as cm:
            self.store.record_contact("Ana", "ana@example.com", "", "hola", "es", origin="fax")
        self.assertEqual(cm.exception.field, "origin")

    def test_lang_unknown_is_empty_and_control_chars_removed(self):
        cid, _ = self.contacto(name=" Ana\x00  Ruiz ", text=" Hola\r\nmundo\x07 ", lang="fr")
        ficha = self.store.get(cid)
        self.assertEqual(ficha["nombre"], "Ana Ruiz")
        self.assertEqual(ficha["mensajes"][0]["texto"], "Hola\nmundo")
        self.assertEqual(ficha["mensajes"][0]["idioma"], "")

    def test_phone_fills_missing(self):
        a, _ = self.contacto(email="ana@example.com", phone="")
        b, _ = self.contacto(email="ana@example.com", phone="600 11 22 33")
        self.assertEqual(a, b)
        self.assertEqual(self.store.get(a)["telefono"], "34600112233")
        # y ahora el teléfono solo también la encuentra
        c, _ = self.contacto(email="", phone="+34600112233")
        self.assertEqual(c, a)

    def test_email_fills_missing_when_found_by_phone(self):
        a, _ = self.contacto(email="", phone="600112233")
        b, _ = self.contacto(email="ana@example.com", phone="600112233")
        self.assertEqual(a, b)
        self.assertEqual(self.store.get(a)["email"], "ana@example.com")

    def test_conflict_marks_possible_duplicate(self):
        a, _ = self.contacto(name="Ana", email="ana@example.com")
        b, _ = self.contacto(name="Bea", email="bea@example.com", phone="600112233")
        cid, nuevo = self.contacto(name="Ana", email="ana@example.com", phone="600 112 233")
        self.assertEqual(cid, a)
        self.assertFalse(nuevo)
        ficha_a, ficha_b = self.store.get(a), self.store.get(b)
        self.assertEqual(ficha_a["posible_duplicado"], b)
        self.assertEqual(ficha_a["telefono"], "")           # no se fusiona: el teléfono sigue siendo de B
        self.assertEqual(len(ficha_b["mensajes"]), 1)
        self.assertEqual({c["id"]: c["posible_duplicado"] for c in self.store.list()}, {a: b, b: None})

    def test_discarded_returns_to_talking(self):
        a, _ = self.contacto()
        self.store.update(a, estado="descartado")
        self.contacto(text="Me lo he pensado")
        self.assertEqual(self.store.get(a)["estado"], "hablando")
        # los demás estados no cambian al volver a escribir
        self.store.update(a, estado="presupuesto")
        self.contacto(text="¿Y el precio?")
        self.assertEqual(self.store.get(a)["estado"], "presupuesto")

    def test_message_keeps_name_used(self):
        a, _ = self.contacto(name="Ana Ruiz")
        self.contacto(name="Anita", text="Soy yo otra vez")
        ficha = self.store.get(a)
        self.assertEqual(ficha["nombre"], "Ana Ruiz")
        self.assertEqual([m["nombre"] for m in ficha["mensajes"]], ["Ana Ruiz", "Anita"])

    def test_messages_in_order_and_last_contact(self):
        a, _ = self.contacto(text="primero")
        self.reloj.t = AHORA + timedelta(days=3)
        self.contacto(text="segundo")
        ficha = self.store.get(a)
        self.assertEqual([m["texto"] for m in ficha["mensajes"]], ["primero", "segundo"])
        self.assertEqual(datetime.fromisoformat(ficha["ultimo_contacto"]), AHORA + timedelta(days=3))
        self.assertEqual(datetime.fromisoformat(ficha["alta"]), AHORA)

    def test_mark_read(self):
        a, _ = self.contacto()
        self.contacto()
        self.store.mark_read(a)
        self.assertEqual(self.store.get(a)["sin_leer"], 0)

    def test_get_missing_is_none(self):
        self.assertIsNone(self.store.get(99))

    def test_list_search_and_filter(self):
        a, _ = self.contacto(name="Ana Ruiz", email="ana@example.com")
        self.reloj.t = AHORA + timedelta(hours=1)
        b, _ = self.contacto(name="Jordi Pérez", email="jordi@correu.cat", phone="611 22 33 44")
        self.store.update(b, estado="presupuesto")
        self.assertEqual([c["id"] for c in self.store.list()], [b, a])  # el último contacto primero
        self.assertEqual([c["id"] for c in self.store.list("ana")], [a])
        self.assertEqual([c["id"] for c in self.store.list("PÉREZ")], [b])
        self.assertEqual([c["id"] for c in self.store.list("correu")], [b])
        self.assertEqual([c["id"] for c in self.store.list("611-22")], [b])
        self.assertEqual([c["id"] for c in self.store.list("C-0001")], [a])
        self.assertEqual([c["id"] for c in self.store.list("c2")], [b])
        self.assertEqual([c["id"] for c in self.store.list(estado="presupuesto")], [b])
        self.assertEqual(self.store.list("ana", estado="presupuesto"), [])
        with self.assertRaises(ClientError):
            self.store.list(estado="inventado")
        fila = self.store.list("ana")[0]
        self.assertEqual(set(fila), {"id", "numero", "nombre", "estado", "origen", "ultimo_contacto", "sin_leer",
                                     "posible_duplicado"})

    def test_update_fields_and_validation(self):
        a, _ = self.contacto()
        self.store.update(a, nombre="Ana R.", email="ANA2@example.com", telefono="600112233", estado="hablando",
                          notas="Llamar el lunes <b>")
        ficha = self.store.get(a)
        self.assertEqual((ficha["nombre"], ficha["email"], ficha["telefono"], ficha["estado"], ficha["notas"]),
                         ("Ana R.", "ana2@example.com", "34600112233", "hablando", "Llamar el lunes <b>"))
        for campos, campo in ((dict(estado="raro"), "status"), (dict(notas="x" * 4001), "notes"),
                              (dict(nombre=""), "name"), (dict(telefono="12"), "phone"),
                              (dict(email="", telefono=""), "contact"), (dict(color="rojo"), "color")):
            with self.assertRaises(ClientError, msg=campo) as cm:
                self.store.update(a, **campos)
            self.assertEqual(cm.exception.field, campo)
        self.assertEqual(self.store.get(a)["estado"], "hablando")  # un error no deja cambios a medias
        with self.assertRaises(KeyError):
            self.store.update(99, nombre="Nadie")

    def test_update_to_other_clients_email_rejected(self):
        a, _ = self.contacto(email="ana@example.com")
        b, _ = self.contacto(name="Bea", email="bea@example.com", phone="611223344")
        for campos in (dict(email=" BEA@example.com"), dict(telefono="+34 611 22 33 44")):
            with self.assertRaises(ClientError) as cm:
                self.store.update(a, **campos)
            self.assertEqual(cm.exception.field, "duplicado")
        self.assertEqual(self.store.get(a)["email"], "ana@example.com")
        # el suyo propio sí se puede volver a guardar
        self.store.update(b, email="bea@example.com", telefono="611223344")

    def test_manual_create_and_message(self):
        a = self.store.create("Pep", "", "600 11 22 33", "telefono", "Me llamó por la tarde")
        ficha = self.store.get(a)
        self.assertEqual((ficha["origen"], ficha["sin_leer"]), ("telefono", 0))  # lo apunta Marc: ya leído
        self.assertEqual(ficha["mensajes"][0]["origen"], "telefono")
        # mismas reglas: el mismo teléfono es el mismo cliente
        self.assertEqual(self.store.create("Pep G.", "", "600112233", "whatsapp"), a)
        self.assertEqual(len(self.store.get(a)["mensajes"]), 1)  # sin texto no se añade mensaje
        self.reloj.t = AHORA + timedelta(days=1)
        self.store.add_message(a, "Le mando presupuesto", "whatsapp")
        ficha = self.store.get(a)
        self.assertEqual(ficha["mensajes"][-1]["texto"], "Le mando presupuesto")
        self.assertEqual(ficha["mensajes"][-1]["nombre"], "Pep")
        self.assertEqual(datetime.fromisoformat(ficha["ultimo_contacto"]), AHORA + timedelta(days=1))
        with self.assertRaises(ClientError):
            self.store.add_message(a, "", "whatsapp")
        with self.assertRaises(ClientError):
            self.store.add_message(a, "hola", "paloma")
        with self.assertRaises(KeyError):
            self.store.add_message(99, "hola", "telefono")
        with self.assertRaises(ClientError) as cm:
            self.store.create("Sin datos", "", "", "persona")
        self.assertEqual(cm.exception.field, "contact")

    def test_delete_removes_messages(self):
        a, _ = self.contacto()
        b, _ = self.contacto(name="Bea", email="bea@example.com")
        self.assertTrue(self.store.delete(a))
        self.assertFalse(self.store.delete(a))
        self.assertIsNone(self.store.get(a))
        n = self.store._db.execute("SELECT COUNT(*) FROM mensajes WHERE cliente = ?", (a,)).fetchone()[0]
        self.assertEqual(n, 0)
        self.assertIsNotNone(self.store.get(b))
        # el mismo correo vuelve a ser un cliente nuevo (con otro número)
        c, nuevo = self.contacto()
        self.assertTrue(nuevo)
        self.assertNotEqual(c, a)

    def test_delete_clears_possible_duplicate_pointer(self):
        a, _ = self.contacto(email="ana@example.com")
        b, _ = self.contacto(name="Bea", email="bea@example.com", phone="600112233")
        self.contacto(email="ana@example.com", phone="600112233")
        self.store.delete(b)
        self.assertIsNone(self.store.get(a)["posible_duplicado"])

    def test_purge_365_from_last_contact_except_cliente(self):
        viejo, _ = self.contacto(name="Viejo", email="viejo@example.com")
        fiel, _ = self.contacto(name="Fiel", email="fiel@example.com")
        self.store.update(fiel, estado="cliente")
        vuelve, _ = self.contacto(name="Vuelve", email="vuelve@example.com")
        self.reloj.t = AHORA + timedelta(days=200)
        self.contacto(name="Vuelve", email="vuelve@example.com", text="sigo aquí")
        self.reloj.t = AHORA + timedelta(days=365, seconds=-1)
        self.assertEqual(self.store.purge(), 0)                   # aún no se cumple el año
        self.reloj.t = AHORA + timedelta(days=366)
        self.assertEqual(self.store.purge(), 1)
        self.assertIsNone(self.store.get(viejo))
        self.assertIsNotNone(self.store.get(fiel))              # los clientes no se borran solos
        self.assertIsNotNone(self.store.get(vuelve))            # cuenta desde el ÚLTIMO contacto
        n = self.store._db.execute("SELECT COUNT(*) FROM mensajes WHERE cliente = ?", (viejo,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_naive_now_rejected(self):
        store = ClientStore(":memory:", lambda: datetime(2026, 10, 17, 10, 0))
        try:
            with self.assertRaises(ValueError):
                store.record_contact("Ana", "ana@example.com", "", "hola", "es")
        finally:
            store.close()


class FileTests(unittest.TestCase):
    def test_file_is_600_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "clientes.sqlite3"
            store = ClientStore(path, Reloj())
            cid, _ = store.record_contact("Ana", "ana@example.com", "", "hola", "es")
            store.close()
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            again = ClientStore(path, Reloj())
            try:
                self.assertEqual(again.record_contact("Ana", "ana@example.com", "", "otra", "es"), (cid, False))
            finally:
                again.close()

    def test_concurrent_same_email_one_client(self):
        # Dos conexiones distintas al mismo fichero (como dos procesos): BEGIN IMMEDIATE las pone en fila.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clientes.sqlite3"
            stores = [ClientStore(path, Reloj()) for _ in range(2)]
            results, errors = [], []
            barrier = threading.Barrier(8)

            def write(i):
                try:
                    barrier.wait()
                    results.append(stores[i % 2].record_contact("Ana", "ANA@example.com ", "", f"hola {i}", "es"))
                except Exception as e:  # noqa: BLE001 - se comprueba abajo que no hubo ninguno
                    errors.append(e)

            threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            try:
                self.assertEqual(errors, [])
                self.assertEqual({cid for cid, _ in results}, {1})
                self.assertEqual(sum(nuevo for _, nuevo in results), 1)
                self.assertEqual(len(stores[0].get(1)["mensajes"]), 8)
            finally:
                for s in stores:
                    s.close()


if __name__ == "__main__":
    unittest.main()
