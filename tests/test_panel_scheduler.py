import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from arthur.panel.scheduler import BriefingClock, RevisionClock, next_run, should_catch_up
from arthur.panel.settings import DAYS, load_settings
from arthur.panel.store import PanelError, PanelStore

MAD = ZoneInfo("Europe/Madrid")


def at(d: int, h: int, m: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, d, h, m, tzinfo=MAD)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = load_settings(Path(tmp.name) / "ajustes.toml")


class SettingsTests(unittest.TestCase):
    def test_fichero_ausente_se_crea_con_las_siete_horas(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "panel" / "ajustes.toml"
            s = load_settings(path)
            self.assertTrue(path.exists())
            self.assertEqual(len(s.times), 7)
            self.assertEqual((s.times[0].hour, s.times[0].minute), (8, 30))
            self.assertEqual(s.times[3].hour, 10)
            self.assertEqual(s.channel, "aviso")
            self.assertEqual(str(s.zone), "Europe/Madrid")
            self.assertEqual(s.vault, Path("/vault"))
            self.assertEqual(s.files, ("00-Inicio/Tareas.md",))
            for d in DAYS:
                self.assertIn(d, path.read_text())

    def test_hora_mal_escrita_nombra_la_clave(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.toml"
            path.write_text('[resumen]\nmartes = "7h"\n')
            with self.assertRaises(PanelError) as ctx:
                load_settings(path)
            self.assertIn("martes", str(ctx.exception))

    def test_clave_desconocida_en_resumen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.toml"
            path.write_text('[resumen]\n"miércoles" = "07:00"\n')
            with self.assertRaises(PanelError) as ctx:
                load_settings(path)
            self.assertIn("miércoles", str(ctx.exception))

    def test_ficheros_de_obsidian_configurables_y_validados(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.toml"
            path.write_text('[obsidian]\nficheros = ["00-Inicio/Tareas.md", "Notas/Hoy.md"]\n')
            self.assertEqual(load_settings(path).files, ("00-Inicio/Tareas.md", "Notas/Hoy.md"))
            for bad in ('["../fuera.md"]', '[".obsidian/secreto.md"]', '["Tareas.txt"]',
                        '["00-Inicio/Tareas.md", "00-Inicio/Tareas.md"]', '"Tareas.md"'):
                path.write_text(f'[obsidian]\nficheros = {bad}\n')
                with self.assertRaises(PanelError, msg=bad):
                    load_settings(path)

    def test_zona_no_soportada_se_rechaza(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.toml"
            path.write_text('zona = "UTC"\n')
            with self.assertRaisesRegex(PanelError, "Europe/Madrid"):
                load_settings(path)


class NextRunTests(Base):
    def test_casos(self):
        s = self.settings
        self.assertEqual(next_run(s, at(16, 6)), at(16, 7))
        self.assertEqual(next_run(s, at(16, 7, 1)), at(17, 10))
        self.assertEqual(next_run(s, at(19, 8)), at(19, 8, 30))
        self.assertEqual(next_run(s, at(22, 9, 59)), at(22, 10))

    def test_cambio_de_hora(self):
        r = next_run(self.settings, at(24, 23))
        self.assertEqual(r, at(25, 10))
        self.assertEqual(r.isoformat(), "2026-10-25T10:00:00+01:00")


class CatchUpTests(Base):
    def test_casos(self):
        s = self.settings   # 20-10-2026 es martes (07:00)
        self.assertTrue(should_catch_up(s, at(20, 9), False))
        self.assertFalse(should_catch_up(s, at(20, 13, 5), False))
        self.assertFalse(should_catch_up(s, at(20, 9), True))
        self.assertFalse(should_catch_up(s, at(20, 6, 30), False))


class ClockTests(Base):
    def setUp(self):
        super().setUp()
        self.store = PanelStore.in_memory()
        self.calls = []
        self.t = at(20, 9)

    def clock(self):
        return BriefingClock(self.settings, self.store, lambda: self.calls.append(self.t), now=lambda: self.t)

    def test_ya_enviado_dos_arranques_no_llaman(self):
        self.store.save_briefing(date(2026, 10, 20), "hola", "x", True)
        self.clock().tick()
        self.clock().tick()
        self.assertEqual(self.calls, [])

    def test_recupera_una_vez_y_no_reintenta_cada_minuto(self):
        c = self.clock()
        c.tick()
        self.t = at(20, 9, 1)
        c.tick()
        self.assertEqual(len(self.calls), 1)

    def test_llega_la_hora_y_avisa_una_vez(self):
        self.t = at(21, 6, 59)   # miércoles 08:30
        c = self.clock()
        c.tick()
        self.t = at(21, 8, 30)
        c.tick()
        self.t = at(21, 8, 31)
        c.tick()
        self.assertEqual(self.calls, [at(21, 8, 30)])

    def test_run_que_falla_se_registra_y_el_dia_queda_intentado(self):
        def boom():
            raise RuntimeError("x")
        c = BriefingClock(self.settings, self.store, boom, now=lambda: self.t)
        with self.assertLogs("arthur.panel.scheduler", "ERROR"):
            c.tick()
        self.assertEqual(c._attempted, date(2026, 10, 20))

    def test_bucle_sobrevive_a_un_tick_que_falla(self):
        c = BriefingClock(self.settings, self.store, lambda: None, now=lambda: self.t,
                          sleep=lambda s: None)
        veces = []

        def tick():
            veces.append(1)
            if len(veces) == 1:
                raise RuntimeError("sqlite")
            c._stop.set()
            return 1.0
        c.tick = tick
        with self.assertLogs("arthur.panel.scheduler", "ERROR"):
            c._loop()
        self.assertEqual(len(veces), 2)

    def test_despertar_tarde_no_avisa_pasadas_las_13(self):
        c = self.clock()
        self.t = at(20, 5)
        c.tick()
        self.t = at(20, 15)
        c.tick()
        self.assertEqual(self.calls, [])

    def test_despertar_con_aviso_ya_entregado_no_llama(self):
        c = self.clock()
        self.t = at(20, 5)
        c.tick()
        self.store.save_briefing(date(2026, 10, 20), "hola", "x", True)
        self.t = at(20, 8)
        c.tick()
        self.assertEqual(self.calls, [])

    def test_start_stop_con_sleep_falso(self):
        c = BriefingClock(self.settings, self.store, lambda: self.calls.append(1),
                          now=lambda: self.t, sleep=lambda s: c._stop.set())
        c.start()
        c.stop()
        self.assertEqual(self.calls, [1])


if __name__ == "__main__":
    unittest.main()


class RevisionSettingsTests(unittest.TestCase):
    def test_por_defecto_y_en_el_fichero_nuevo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ajustes.toml"
            s = load_settings(path)
            self.assertEqual((s.revision_time, s.photo_time, s.study_prefix), (time(21, 30), time(8, 0), "Estudio"))
            self.assertIn("[revision]", path.read_text())

    def test_fichero_antiguo_sin_tabla_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.toml"
            path.write_text('[resumen]\nlunes = "08:00"\n')
            self.assertEqual(load_settings(path).revision_time, time(21, 30))

    def test_valores_propios_y_errores(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.toml"
            path.write_text('[revision]\nhora = "22:15"\nfoto = "05:30"\nprefijo_estudio = "Estudiar"\n')
            s = load_settings(path)
            self.assertEqual((s.revision_time, s.photo_time, s.study_prefix), (time(22, 15), time(5, 30), "Estudiar"))
            for mal in ('hora = "9"', 'prefijo_estudio = ""', 'otra = "x"', 'foto = "23:00"'):
                path.write_text(f"[revision]\n{mal}\n")
                with self.assertRaises(PanelError, msg=mal):
                    load_settings(path)


class FakeRevision:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.stored = set()

    def close_days(self):
        self.calls.append("cerrar")

    def ensure_photo(self):
        self.calls.append("foto")
        if self.fail:
            raise RuntimeError("calendario caído")

    def run(self, kind):
        self.calls.append(kind)
        if self.fail:
            raise RuntimeError("todo mal")

    def done(self, day, kind):
        return (day, kind) in self.stored


class RevisionClockTests(unittest.TestCase):
    def make(self, momento):
        self.now = momento
        self.rev = FakeRevision()
        return RevisionClock(self.rev, revision_time=time(21, 30), photo_time=time(8, 0), now=lambda: self.now)

    def test_de_madrugada_solo_cierra(self):
        clock = self.make(at(24, 3))
        wait = clock.tick()
        self.assertEqual(self.rev.calls, ["cerrar"])
        self.assertLessEqual(wait, 60)

    def test_foto_a_las_ocho_una_vez(self):
        clock = self.make(at(24, 7, 59))
        clock.tick()
        self.now = at(24, 8, 0)
        clock.tick()
        clock.tick()
        self.assertEqual(self.rev.calls.count("foto"), 1)

    def test_revision_nocturna_y_recuperacion_al_arrancar(self):
        clock = self.make(at(24, 21, 29))
        clock.tick()
        self.assertNotIn("noche", self.rev.calls)
        self.now = at(24, 21, 30)
        clock.tick()
        clock.tick()
        self.assertEqual(self.rev.calls.count("noche"), 1)
        # arrancar a las 23:00 sin revisión guardada: se hace; con ella guardada, no
        clock = self.make(at(24, 23, 0))
        clock.tick()
        self.assertIn("noche", self.rev.calls)
        clock = self.make(at(24, 23, 0))
        self.rev.stored.add((date(2026, 10, 24), "noche"))
        clock.tick()
        self.assertNotIn("noche", self.rev.calls)

    def test_domingo_semanal_en_vez_de_nocturna(self):
        clock = self.make(at(25, 21, 45))
        clock.tick()
        self.assertIn("semana", self.rev.calls)
        self.assertNotIn("noche", self.rev.calls)

    def test_un_fallo_no_se_reintenta_cada_minuto_ni_mata_el_hilo(self):
        clock = self.make(at(24, 21, 40))
        self.rev.fail = True
        with self.assertLogs("arthur.panel.scheduler", "ERROR"):
            clock.tick()
            clock.tick()
        self.assertEqual(self.rev.calls.count("noche"), 1)
        self.assertEqual(self.rev.calls.count("foto"), 1)
        # al día siguiente sí lo vuelve a intentar
        self.rev.fail = False
        self.now = at(25, 21, 40)
        clock.tick()
        self.assertIn("semana", self.rev.calls)
