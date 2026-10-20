import unittest
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.integrations.google import GoogleError
from arthur.panel.sources import SourceError
from arthur.panel.sources.obsidian import Checkbox, ObsidianVault
from arthur.panel.store import PanelStore
from arthur.panel.today import build_today
from arthur.tools.base import ToolError

MADRID = ZoneInfo("Europe/Madrid")
HOY = date(2026, 10, 16)
AHORA = datetime(2026, 10, 16, 10, 30, tzinfo=MADRID)


class FakeVault:
    def __init__(self, boxes=None, error=None):
        self.boxes, self.error = boxes or [], error

    def checkboxes(self):
        if self.error:
            raise self.error
        return self.boxes


class FakeCalendar:
    def __init__(self, events=None, error=None):
        self.crudos, self.error = events or [], error

    def events(self, start, end):
        if self.error:
            raise self.error
        return self.crudos


class FakeGmail:
    def __init__(self, mails=None, error=None):
        self.mails, self.error = mails or [], error

    def list(self, query, limit):
        if self.error:
            raise self.error
        return self.mails


class FakeLink:
    def __init__(self, reviews=None, error=None):
        self.reviews, self.error = reviews or [], error

    def call(self, path):
        if self.error:
            raise self.error
        return {"shorts": [{"id": r["id"], "title": r["title"], "published_at": r["created_at"]}
                           for r in self.reviews if r["status"] == "published" and r["kind"] == "corto"]}


def clase(titulo, ini, fin):
    return {"summary": titulo, "start": {"dateTime": f"2026-10-16T{ini}:00+02:00"},
            "end": {"dateTime": f"2026-10-16T{fin}:00+02:00"}}


CLASES = [clase("Contabilidad", "08:00", "10:00"), clase("Derecho", "10:00", "12:00"),
          clase("Estadística", "12:00", "14:00"),
          {"summary": "Festivo", "start": {"date": "2026-10-16"}, "end": {"date": "2026-10-17"}}]
CORREO = [{"id": "m1", "from": "Ana <ana@x.es>", "subject": "Hola"}]


class TodayTests(unittest.TestCase):
    def setUp(self):
        self.store = PanelStore.in_memory(now=lambda: AHORA)
        self.addCleanup(self.store.close)

    def build(self, **kw):
        args = dict(store=self.store, vault=None, calendar=None, gmail=None, studio_link=None, now=AHORA)
        args.update(kw)
        return build_today(**args)

    def test_mezcla_las_fuentes_pendientes_primero(self):
        hecha = self.store.add_task("Hecha", None)
        self.store.toggle(hecha.id)
        propia = self.store.add_task("Llamar a Ana", HOY)
        vault = FakeVault([Checkbox("00-Inicio/Tareas.md", 7, "Yo: revisar apuntes", False)])
        link = FakeLink([{"id": 3, "kind": "corto", "title": "Séneca", "status": "published",
                          "created_at": "2026-10-16T09:00:00+02:00"}])
        d = self.build(vault=vault, studio_link=link, calendar=FakeCalendar(CLASES), gmail=FakeGmail(CORREO))
        ids = [t["id"] for t in d["tasks"]]
        self.assertEqual(ids[-1], f"arthur-{hecha.id}")
        self.assertEqual([t["done"] for t in d["tasks"]], [False] * 4 + [True])
        self.assertIn(f"arthur-{propia.id}", ids)
        self.assertIn("obsidian-00-Inicio/Tareas.md:7", ids)
        self.assertEqual(sum(1 for t in d["tasks"] if t["source"] == "studio"), 2)
        obs = next(t for t in d["tasks"] if t["source"] == "obsidian")
        self.assertEqual((obs["file"], obs["line"], obs["title"], obs["due"]),
                         ("00-Inicio/Tareas.md", 7, "Yo: revisar apuntes", None))
        self.assertEqual(d["tasks"][ids.index(f"arthur-{propia.id}")]["due"], "2026-10-16")
        self.assertEqual(d["date"], "2026-10-16")
        self.assertEqual(d["now"], "2026-10-16T10:30:00+02:00")
        self.assertEqual(d["mail"], [{"id": "m1", "sender": "Ana", "subject": "Hola",
                                  "link": "https://mail.google.com/mail/u/0/#inbox/m1", "snippet": ""}])
        self.assertEqual(len(d["events"]), 4)
        self.assertEqual(d["events"][2], {"title": "Derecho", "start": "2026-10-16T10:00:00+02:00",
                                          "end": "2026-10-16T12:00:00+02:00", "all_day": False})
        self.assertEqual(d["problems"], [])
        self.assertEqual(d["progress"], {"done": 1, "total": 5, "percent": 20})

    def test_actual_y_siguiente_a_mitad_de_clase(self):
        d = self.build(calendar=FakeCalendar(CLASES), now=datetime(2026, 10, 16, 10, 30, tzinfo=MADRID))
        self.assertEqual(d["next"]["current"]["title"], "Derecho")
        self.assertEqual(d["next"]["following"]["title"], "Estadística")

    def test_sin_clase_en_curso_ni_siguiente(self):
        d = self.build(calendar=FakeCalendar(CLASES), now=datetime(2026, 10, 16, 15, 0, tzinfo=MADRID))
        self.assertEqual(d["next"], {"current": None, "following": None})
        # Un evento de día entero nunca es «el actual».
        d = self.build(calendar=FakeCalendar(CLASES[3:]))
        self.assertEqual(d["next"], {"current": None, "following": None})

    def test_progreso_tres_de_siete(self):
        for i in range(7):
            t = self.store.add_task(f"T{i}", None)
            if i < 3:
                self.store.toggle(t.id)
        self.assertEqual(self.build()["progress"], {"done": 3, "total": 7, "percent": 43})

    def test_casilla_de_obsidian_sigue_contando_tras_marcar_y_se_puede_desmarcar(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "Tareas.md"
            note.write_text("- [ ] Yo: repasar\n")
            vault = ObsidianVault(Path(tmp), ("Tareas.md",))
            self.assertEqual(self.build(vault=vault)["progress"], {"done": 0, "total": 1, "percent": 0})
            vault.toggle("Tareas.md", 1, "Yo: repasar")
            self.assertEqual(self.build(vault=vault)["progress"], {"done": 1, "total": 1, "percent": 100})
            vault.toggle("Tareas.md", 1, "Yo: repasar")
            self.assertEqual(self.build(vault=vault)["progress"], {"done": 0, "total": 1, "percent": 0})

    def test_sin_nada_el_porcentaje_es_cero(self):
        d = self.build()
        self.assertEqual(d["progress"], {"done": 0, "total": 0, "percent": 0})
        self.assertEqual((d["tasks"], d["events"], d["mail"], d["problems"]), ([], [], [], []))

    def test_los_eventos_no_cuentan_en_el_progreso(self):
        d = self.build(calendar=FakeCalendar(CLASES))
        self.assertEqual(d["progress"]["total"], 0)

    def test_cada_fuente_que_falla_deja_las_otras(self):
        fallos = {
            "vault": (FakeVault(error=SourceError("La carpeta de Obsidian no está")),
                      "La carpeta de Obsidian no está"),
            "calendar": (FakeCalendar(error=GoogleError("x")), "Calendario no disponible ahora"),
            "gmail": (FakeGmail(error=OSError("x")), "Correo no disponible ahora"),
            "studio_link": (FakeLink(error=ToolError("x")), "Studio no disponible ahora"),
        }
        buenas = {"vault": FakeVault([Checkbox("a.md", 1, "Yo: x", False)]),
                  "calendar": FakeCalendar(CLASES), "gmail": FakeGmail(CORREO), "studio_link": FakeLink()}
        for nombre, (roto, texto) in fallos.items():
            with self.subTest(nombre):
                fuentes = dict(buenas)
                fuentes[nombre] = roto
                d = self.build(**fuentes)
                self.assertEqual(d["problems"], [texto])
                self.assertEqual(bool(d["tasks"]), nombre != "vault")
                self.assertEqual(bool(d["events"]), nombre != "calendar")
                self.assertEqual(bool(d["mail"]), nombre != "gmail")


if __name__ == "__main__":
    unittest.main()
