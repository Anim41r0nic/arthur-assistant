"""Tablas de la revisión en panel.sqlite3 (esquema 2): foto del plan, bloques de estudio, días, revisiones, logros y
preferencias. La migración desde el esquema 1 no toca las tareas ni los resúmenes."""

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from arthur.panel.store import PanelStore

MADRID = timezone(timedelta(hours=2))
HOY = date(2026, 10, 24)
AYER = date(2026, 10, 23)


class RevisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.ahora = datetime(2026, 10, 24, 6, 0, tzinfo=MADRID)
        self.store = PanelStore.in_memory(now=lambda: self.ahora)
        self.addCleanup(self.store.close)

    def test_la_foto_no_se_rehace(self):
        primera = self.store.save_photo(HOY, {"tareas": [{"id": 1}], "bloques": [], "obsidian": []})
        segunda = self.store.save_photo(HOY, {"tareas": [], "bloques": [], "obsidian": []})
        self.assertEqual(primera, segunda)
        self.assertEqual(self.store.photo(HOY)["tareas"], [{"id": 1}])
        self.assertEqual(self.store.photo(HOY)["tomada_en"], "2026-10-24T06:00:00+02:00")
        self.assertIsNone(self.store.photo(AYER))

    def test_marcas_de_bloques(self):
        self.store.set_mark(HOY, "k1", "Estudio: MTP", "2026-10-24T10:00:00+02:00", "2026-10-24T10:50:00+02:00", "hecho")
        self.store.set_mark(HOY, "k2", "Estudio: ETC", "a", "b", "medio")
        self.store.set_mark(HOY, "k2", "Estudio: ETC", "a", "b", "no")
        marks = self.store.marks(HOY)
        self.assertEqual({k: v["marca"] for k, v in marks.items()}, {"k1": "hecho", "k2": "no"})
        self.store.set_mark(HOY, "k1", "Estudio: MTP", "a", "b", None)   # quitar la marca
        self.assertNotIn("k1", self.store.marks(HOY))
        self.assertEqual(self.store.marks(AYER), {})

    def test_dias_pausa_repaso_y_puntos_que_no_bajan_al_cerrar(self):
        self.assertIsNone(self.store.day(HOY))
        self.store.set_pause(HOY, True)
        self.assertTrue(self.store.day(HOY)["pausa"])
        self.store.set_repaso(HOY, {"hecha": True, "preguntas": 10, "aciertos": 8})
        self.assertEqual(self.store.day(HOY)["repaso"]["preguntas"], 10)
        self.store.save_day(HOY, {"activo": True}, 40, close=True)
        self.store.save_day(HOY, {"activo": True}, 20, close=True)   # recalcular un día cerrado nunca resta
        d = self.store.day(HOY)
        self.assertEqual((d["puntos"], d["cerrado"], d["datos"]), (40, True, {"activo": True}))
        self.store.save_day(HOY, {"activo": True}, 60, close=True)
        self.assertEqual(self.store.day(HOY)["puntos"], 60)
        self.assertEqual(list(self.store.days()), [HOY])

    def test_dia_abierto_se_puede_recalcular_hacia_abajo(self):
        self.store.save_day(HOY, {}, 30, close=False)
        self.store.save_day(HOY, {}, 10, close=False)
        self.assertEqual(self.store.day(HOY)["puntos"], 10)
        self.assertFalse(self.store.day(HOY)["cerrado"])

    def test_revisiones_y_semana_leida(self):
        self.store.save_revision(HOY, "noche", {"x": 1}, "Buen día.", "plantilla", True)
        r = self.store.revision(HOY, "noche")
        self.assertEqual((r["texto"], r["motor"], r["avisada"], r["datos"]), ("Buen día.", "plantilla", True, {"x": 1}))
        self.assertIsNone(self.store.revision(HOY, "semana"))
        self.assertIsNone(self.store.first_week_read())
        self.store.save_revision(date(2026, 10, 25), "semana", {}, "t", "plantilla", False)
        self.assertTrue(self.store.mark_week_read(date(2026, 10, 25)))
        self.assertEqual(self.store.first_week_read(), HOY)   # el día en que se leyó
        self.assertFalse(self.store.mark_week_read(date(2026, 11, 1)))   # no existe

    def test_logros_no_se_pierden(self):
        self.store.add_badges({"primer-paso": AYER})
        self.store.add_badges({"primer-paso": HOY, "tres-activos": HOY})
        self.assertEqual(self.store.badges(), {"primer-paso": AYER, "tres-activos": HOY})

    def test_preferencias(self):
        self.assertEqual(self.store.pref("ocultar_juego", "0"), "0")
        self.store.set_pref("ocultar_juego", "1")
        self.assertEqual(self.store.pref("ocultar_juego", "0"), "1")

    def test_tareas_para_la_foto(self):
        vieja = self.store.add_task("atrasada", AYER)
        hoy = self.store.add_task("de hoy", HOY)
        self.store.add_task("sin fecha", None)
        self.store.add_task("mañana", HOY + timedelta(1))
        hecha_ayer = self.store.add_task("hecha ayer", AYER)
        self.ahora = datetime(2026, 10, 23, 20, 0, tzinfo=MADRID)
        self.store.toggle(hecha_ayer.id)
        self.ahora = datetime(2026, 10, 24, 9, 0, tzinfo=MADRID)
        self.store.toggle(hoy.id)
        ids = [t.id for t in self.store.plan_candidates(HOY)]
        self.assertEqual(ids, [hoy.id])
        self.assertEqual([t.id for t in self.store.done_on(HOY)], [hoy.id])
        self.assertEqual(self.store.task(vieja.id).title, "atrasada")
        self.assertIsNone(self.store.task(999))


class MigracionTests(unittest.TestCase):
    def test_de_esquema_1_a_2_sin_perder_nada(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = sqlite3.connect(root / "panel.sqlite3")
            db.executescript("""
                CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, source TEXT NOT NULL,
                    due TEXT, created_at TEXT NOT NULL, done_at TEXT, studio_ref TEXT);
                CREATE TABLE briefings (date TEXT PRIMARY KEY, sent_at TEXT NOT NULL, text TEXT NOT NULL,
                    engine TEXT NOT NULL, delivered INTEGER NOT NULL);
                INSERT INTO tasks (title, source, due, created_at) VALUES ('vieja', 'arthur', NULL, '2026-10-01T08:00:00+02:00');
                PRAGMA user_version = 1;""")
            db.close()
            store = PanelStore(root)
            self.assertEqual(store.task(1).title, "vieja")
            store.set_pref("x", "y")
            store.close()
            store = PanelStore(root)   # abrir otra vez no repite la migración
            self.assertEqual(store.pref("x", ""), "y")
            store.close()
            self.assertEqual(sqlite3.connect(root / "panel.sqlite3").execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
