import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from arthur.panel.store import PanelError, PanelStore

MADRID = timezone(timedelta(hours=2))  # octubre: horario de verano
HOY = date(2026, 10, 16)
AYER = date(2026, 10, 15)


class Reloj:
    def __init__(self, momento: datetime) -> None:
        self.momento = momento

    def __call__(self) -> datetime:
        return self.momento


class PanelStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reloj = Reloj(datetime(2026, 10, 16, 8, 30, 0, 123456, tzinfo=MADRID))
        self.store = PanelStore.in_memory(now=self.reloj)

    def test_titulo_vacio_o_largo_falla(self) -> None:
        for titulo in ("", "   ", "x" * 201):
            with self.assertRaises(PanelError):
                self.store.add_task(titulo, HOY)
        self.assertEqual(self.store.add_task("x" * 200, HOY).title, "x" * 200)

    def test_fuente_desconocida_falla(self) -> None:
        with self.assertRaises(PanelError):
            self.store.add_task("algo", HOY, source="obsidian")

    def test_toggle_dos_veces_deja_sin_hacer(self) -> None:
        t = self.store.add_task("llamar a Ana", HOY)
        self.assertIsNone(t.done_at)
        hecha = self.store.toggle(t.id)
        self.assertEqual(hecha.done_at, "2026-10-16T08:30:00+02:00")
        self.assertIsNone(self.store.toggle(t.id).done_at)

    def test_toggle_inexistente(self) -> None:
        with self.assertRaises(KeyError):
            self.store.toggle(99)

    def test_studio_ref_repetido_no_duplica(self) -> None:
        a = self.store.add_task("Revisar vídeo", HOY, source="studio", studio_ref="job-1")
        b = self.store.add_task("Otro título", HOY, source="studio", studio_ref="job-1")
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(self.store.tasks_for(HOY)), 1)

    def test_fechas_con_zona(self) -> None:
        t = self.store.add_task("a", HOY)
        self.assertEqual(t.created_at, "2026-10-16T08:30:00+02:00")
        self.assertEqual(t.due, "2026-10-16")
        self.assertIsNone(self.store.add_task("b", None).due)

    def test_tasks_for_atrasadas_y_hechas(self) -> None:
        ayer_sin = self.store.add_task("ayer sin hacer", AYER)
        ayer_hecha = self.store.add_task("ayer hecha", AYER)
        sin_fecha = self.store.add_task("sin fecha", None)
        manana = self.store.add_task("mañana", HOY + timedelta(days=1))
        hoy = self.store.add_task("hoy", HOY)
        self.reloj.momento = datetime(2026, 10, 15, 20, 0, tzinfo=MADRID)
        self.store.toggle(ayer_hecha.id)
        self.reloj.momento = datetime(2026, 10, 16, 9, 0, tzinfo=MADRID)
        self.store.toggle(hoy.id)
        ids = [t.id for t in self.store.tasks_for(HOY)]
        self.assertEqual(ids, [ayer_sin.id, sin_fecha.id, hoy.id])  # pendientes (por fecha) y luego hechas
        self.assertNotIn(manana.id, ids)
        self.assertNotIn(ayer_hecha.id, ids)
        self.assertIn(ayer_hecha.id, [t.id for t in self.store.tasks_for(AYER)])

    def test_resumen(self) -> None:
        self.assertFalse(self.store.briefing_sent(HOY))
        self.assertIsNone(self.store.last_briefing(HOY))
        self.store.save_briefing(HOY, "Buenos días", "gemini", True)
        self.assertTrue(self.store.briefing_sent(HOY))
        self.assertFalse(self.store.briefing_sent(AYER))
        self.assertEqual(self.store.last_briefing(HOY), {
            "date": "2026-10-16", "sent_at": "2026-10-16T08:30:00+02:00",
            "text": "Buenos días", "engine": "gemini", "delivered": True})

    def test_resumen_no_entregado_cuenta_como_no_enviado(self) -> None:
        self.store.save_briefing(HOY, "x", "local", False)
        self.assertFalse(self.store.briefing_sent(HOY))
        self.assertFalse(self.store.last_briefing(HOY)["delivered"])
        self.store.save_briefing(HOY, "y", "local", True)  # rehacerlo sustituye al anterior
        self.assertEqual(self.store.last_briefing(HOY)["text"], "y")


class PanelStoreDiscoTests(unittest.TestCase):
    def test_persistencia_y_permisos(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            raiz = Path(d) / "panel"
            reloj = Reloj(datetime(2026, 10, 16, 8, 0, tzinfo=MADRID))
            s = PanelStore(raiz, now=reloj)
            s.add_task("persiste", HOY)
            s.close()
            self.assertEqual(raiz.stat().st_mode & 0o777, 0o700)
            self.assertEqual([t.title for t in PanelStore(raiz, now=reloj).tasks_for(HOY)], ["persiste"])


if __name__ == "__main__":
    unittest.main()
