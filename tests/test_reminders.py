"""Recordatorios: almacén, herramientas y avisos. Reloj FALSO: nada depende de la hora real."""

import unittest
from datetime import datetime, timedelta

from arthur.core.reminders import ReminderScheduler
from arthur.memory.store import MAX_REMINDERS, MemoryStore
from arthur.tools.base import ToolError
from arthur.tools.reminders import build_reminder_tools, due_time, when_text

NOW = datetime(2026, 10, 5, 20, 0)


def args(**kw):
    return {"text": "sacar la ropa", "minutes": 0, "at": "", "date": "", **kw}


class DueTimeTest(unittest.TestCase):
    def test_in_minutes(self):
        self.assertEqual(due_time(args(minutes=20), NOW), NOW + timedelta(minutes=20))

    def test_at_an_hour_today_or_tomorrow(self):
        self.assertEqual(due_time(args(at="21:30"), NOW), datetime(2026, 10, 5, 21, 30))
        self.assertEqual(due_time(args(at="8:00"), NOW), datetime(2026, 10, 6, 8, 0))  # ya pasó hoy → mañana
        self.assertEqual(due_time(args(at="9.15", date="2026-12-24"), NOW), datetime(2026, 12, 24, 9, 15))

    def test_invalid(self):
        for bad in (args(), args(minutes=5, at="10:00"), args(at="25:00"), args(at="10:00", date="ayer"),
                    args(at="10:00", date="2026-01-01"), args(minutes=5, date="2026-12-24"),
                    args(at="10:00", date="2028-01-01")):
            with self.subTest(bad=bad), self.assertRaises(ToolError):
                due_time(bad, NOW)

    def test_when_text(self):
        self.assertEqual(when_text(NOW + timedelta(minutes=20), NOW), "hoy a las 20:20 (en 20 min)")
        self.assertEqual(when_text(NOW + timedelta(minutes=125), NOW), "hoy a las 22:05 (en 2 h 5 min)")
        self.assertEqual(when_text(datetime(2026, 10, 6, 8, 0), NOW), "mañana a las 08:00")
        self.assertEqual(when_text(datetime(2026, 10, 12, 9, 0), NOW), "el 12/10 a las 09:00")


class ReminderToolsTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore.in_memory()
        self.addCleanup(self.store.close)
        self.tools = {t.name: t for t in build_reminder_tools(self.store, now=lambda: NOW)}

    def run_tool(self, name, **kw):
        return self.tools[name].run(kw)

    def test_set_list_cancel(self):
        self.assertEqual(self.run_tool("set_reminder", **args(minutes=10, text="temporizador de 10 minutos")),
                         "Recordatorio #1 para hoy a las 20:10 (en 10 min): temporizador de 10 minutos")
        self.run_tool("set_reminder", **args(at="08:00"))
        self.assertEqual(self.run_tool("list_reminders").splitlines(), [
            "#1 hoy a las 20:10 (en 10 min): temporizador de 10 minutos",
            "#2 mañana a las 08:00: sacar la ropa"])
        self.assertEqual(self.run_tool("cancel_reminder", id=1), "He quitado el recordatorio #1")
        with self.assertRaises(ToolError):
            self.run_tool("cancel_reminder", id=1)

    def test_rejects_secrets_and_too_many(self):
        with self.assertRaises(ToolError):
            self.run_tool("set_reminder", **args(minutes=5, text="mi contraseña es hunter2"))
        for _ in range(MAX_REMINDERS):
            self.store.add_reminder("x", NOW + timedelta(hours=1))
        with self.assertRaises(ToolError):
            self.run_tool("set_reminder", **args(minutes=5))

    def test_risk_needs_no_confirmation(self):
        self.assertEqual({t.risk.value for t in self.tools.values()} - {"read", "low"}, set())


class SchedulerTest(unittest.TestCase):
    def test_fires_once_when_due_and_says_if_late(self):
        store = MemoryStore.in_memory()
        self.addCleanup(store.close)
        store.add_reminder("sacar la ropa", NOW + timedelta(minutes=10))
        store.add_reminder("llamar a mamá", NOW - timedelta(hours=3))  # el portátil estaba apagado
        store.add_reminder("mañana", NOW + timedelta(days=1))
        clock = [NOW]
        said = []
        scheduler = ReminderScheduler(store, said.append, now=lambda: clock[0])
        scheduler.check()
        self.assertEqual(said, ["Recordatorio atrasado, era para las 17:00: llamar a mamá."])
        clock[0] = NOW + timedelta(minutes=10)
        scheduler.check()
        scheduler.check()  # ya avisado: no se repite
        self.assertEqual(said[1:], ["Recordatorio: sacar la ropa."])
        self.assertEqual([r.text for r in store.list_reminders()], ["mañana"])

    def test_survives_restarts(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.sqlite3"
            store = MemoryStore(path)
            store.add_reminder("x", NOW)
            store.close()
            store = MemoryStore(path)
            self.assertEqual(len(store.list_reminders()), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
