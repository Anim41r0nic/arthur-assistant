import unittest
from datetime import date, datetime, timedelta, timezone

from arthur.integrations.google import GoogleError
from arthur.panel.sources import SourceError
from arthur.panel.sources.google import GMAIL_QUERY, Event, Mail, important_mail, today_events


class CalendarFalso:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.llamadas = items or [], error, []

    def events(self, start, end):
        self.llamadas.append((start, end))
        if self.error:
            raise self.error
        return self.items


class GmailFalso:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.llamadas = items or [], error, []

    def list(self, query, max_results):
        self.llamadas.append((query, max_results))
        if self.error:
            raise self.error
        return self.items


class EventosTests(unittest.TestCase):
    def test_dia_entero_y_con_hora_ordenados(self):
        cal = CalendarFalso([
            {"summary": "Dentista", "start": {"dateTime": "2026-10-16T17:00:00+02:00"},
             "end": {"dateTime": "2026-10-16T18:00:00+02:00"}},
            {"summary": "Festivo", "start": {"date": "2026-10-16"}, "end": {"date": "2026-10-17"}},
            {"start": {"dateTime": "2026-10-16T09:30:00+02:00"}, "end": {"dateTime": "2026-10-16T10:00:00+02:00"}},
        ])
        eventos = today_events(cal, date(2026, 10, 16))
        self.assertEqual([e.title for e in eventos], ["Festivo", "(sin título)", "Dentista"])
        festivo = eventos[0]
        self.assertTrue(festivo.all_day)
        self.assertEqual(festivo.start.isoformat(), "2026-10-16T00:00:00+02:00")
        self.assertEqual(festivo.end.isoformat(), "2026-10-17T00:00:00+02:00")
        self.assertFalse(eventos[2].all_day)
        self.assertEqual(eventos[2].start.utcoffset(), timedelta(hours=2))
        self.assertEqual(len(cal.llamadas), 1)

    def test_error_de_google(self):
        for error in (GoogleError("x"), OSError("red")):
            with self.assertRaises(SourceError) as cm:
                today_events(CalendarFalso(error=error), date(2026, 10, 16))
            self.assertEqual(str(cm.exception), "Calendario no disponible ahora")

        with self.assertRaisesRegex(SourceError, "Calendario no disponible ahora"):
            today_events(CalendarFalso([{"start": {}, "end": {"date": "2026-10-17"}}]), date(2026, 10, 16))


class CorreoTests(unittest.TestCase):
    def test_consulta_y_remitente(self):
        gmail = GmailFalso([
            {"id": "a1", "from": '"Ana" <ana@x.es>', "subject": "Hola", "snippet": "Hola, ¿cómo estás?",
             "date": "", "unread": True},
            {"id": "b2", "from": "Ana Pérez <ana@x.es>", "subject": "", "date": "", "unread": True},
            {"id": "c3", "from": "pepe@x.es", "subject": "Y", "date": "", "unread": True},
        ])
        correos = important_mail(gmail)
        self.assertEqual(gmail.llamadas, [("is:important is:unread newer_than:2d", 10)])
        self.assertEqual(GMAIL_QUERY, "is:important is:unread newer_than:2d")
        self.assertEqual(correos[0], Mail("a1", "Ana", "Hola", "https://mail.google.com/mail/u/0/#inbox/a1",
                                          "Hola, ¿cómo estás?"))
        self.assertEqual(correos[1].sender, "Ana Pérez")
        self.assertEqual(correos[1].subject, "(sin asunto)")
        self.assertEqual(correos[2].sender, "pepe@x.es")

    def test_error_de_google(self):
        for error in (GoogleError("x"), OSError("red")):
            with self.assertRaises(SourceError) as cm:
                important_mail(GmailFalso(error=error))
            self.assertEqual(str(cm.exception), "Correo no disponible ahora")

        with self.assertRaisesRegex(SourceError, "Correo no disponible ahora"):
            important_mail(GmailFalso([{"subject": "sin ID"}]))


if __name__ == "__main__":
    unittest.main()
