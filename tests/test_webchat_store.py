import unittest

from arthur.webchat.store import QUEUED_NOTICE, ContactError, notice_text, validate_contact


def valido(**extra):
    body = {"name": "Ana Ruiz", "email": "ana@example.com", "need": "Una web", "lang": "es", "consent": True}
    body.update(extra)
    return body


class ValidateTests(unittest.TestCase):
    def test_valido_y_limpio(self):
        c = validate_contact(valido(name="  Ana  ", need=" Hola\r\nmundo\x00 ", lang="fr"))
        self.assertEqual(c, {"name": "Ana", "email": "ana@example.com", "phone": "", "need": "Hola\nmundo",
                             "lang": "en"})

    def test_idiomas(self):
        for lang in ("es", "en", "ca"):
            self.assertEqual(validate_contact(valido(lang=lang))["lang"], lang)

    def test_errores_con_campo(self):
        casos = [
            ({"consent": False}, "consent"),
            ({"consent": "true"}, "consent"),
            ({"consent": 1}, "consent"),
            ({"email": "mal"}, "email"),
            ({"email": "a b@x.com"}, "email"),
            ({"email": "a@" + "x" * 200 + ".com"}, "email"),
            ({"name": ""}, "name"),
            ({"name": "   "}, "name"),
            ({"name": "a" * 101}, "name"),
            ({"name": 5}, "name"),
            ({"need": ""}, "need"),
            ({"need": "a" * 2001}, "need"),
        ]
        for extra, campo in casos:
            with self.subTest(extra=campo):
                with self.assertRaises(ContactError) as cm:
                    validate_contact(valido(**extra))
                self.assertEqual(cm.exception.field, campo)

    def test_sin_consentimiento_ausente(self):
        body = valido()
        del body["consent"]
        with self.assertRaises(ContactError) as cm:
            validate_contact(body)
        self.assertEqual(cm.exception.field, "consent")


class PhoneTests(unittest.TestCase):
    def test_contact_with_phone_validated(self):
        # El teléfono es opcional; si viene, se deja con su formato (lo normaliza `clientes`) pero limpio.
        for phone in ("+34 600 11 22 33", "600-112-233", "0034600112233", "(600)112233", "600.11.22.33",
                      " 600112233 ", "+44 20 7946 0958"):
            with self.subTest(phone=phone):
                self.assertEqual(validate_contact(valido(phone=phone))["phone"], phone.strip())
        for body in (valido(), valido(phone=""), valido(phone="   "), valido(phone=None)):
            self.assertEqual(validate_contact(body)["phone"], "")

    def test_bad_phone_400_field_phone(self):
        for phone in ("abc", "1234", "600 11 22 3a", "+34 600+112233", "6" * 16, "1" * 31, 600112233,
                      ["600112233"], "600\n112233", "00", "+"):
            with self.subTest(phone=phone):
                with self.assertRaises(ContactError) as cm:
                    validate_contact(valido(phone=phone))
                self.assertEqual(cm.exception.field, "phone")

    def test_lone_surrogate_is_a_field_error(self):
        # JSON lo admite, pero no se puede guardar ni reenviar como UTF-8: mejor un 400 claro que un fallo luego.
        with self.assertRaises(ContactError) as cm:
            validate_contact(valido(need="Hola \ud800 adiós"))
        self.assertEqual(cm.exception.field, "need")


class NoticeTests(unittest.TestCase):
    def test_notice_texts_new_and_repeat(self):
        self.assertEqual(notice_text(12, True), "Cliente nuevo C-0012")
        self.assertEqual(notice_text(12, False), "C-0012 ha vuelto a escribir")
        self.assertEqual(notice_text(12345, True), "Cliente nuevo C-12345")
        self.assertEqual(QUEUED_NOTICE, "Contacto nuevo en cola (clientes no responde)")


if __name__ == "__main__":
    unittest.main()
