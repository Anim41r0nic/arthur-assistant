"""Creator Studio, paso 1: base de datos propia y cola de trabajos que sobrevive a los cortes.

Reloj FALSO: nada depende de la hora real ni hay esperas.
"""

import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from arthur.studio.jobs import JobRunner, Step, StepFailed
from arthur.studio.store import MIGRATIONS, StudioStore, StudioStoreError

NOW = datetime(2026, 10, 12, 21, 0)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def counting_step(name, calls, output=None):
    def run(ctx):
        calls.append(name)
        return output if output is not None else {"hecho": name, "antes": sorted(ctx.outputs)}
    return Step(name, run)


class StoreTest(unittest.TestCase):
    def test_file_is_private_and_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio" / "studio.sqlite3"
            store = StudioStore(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
            self.assertEqual(store.schema_version, len(MIGRATIONS))
            store.create_job("prueba", {"tema": "ejemplo"}, now=NOW)
            store.close()
            # Abrirlo otra vez no repite migraciones ni pierde datos.
            again = StudioStore(path)
            self.assertEqual(len(again.list_jobs()), 1)
            again.close()

    def test_refuses_file_from_a_newer_arthur(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.sqlite3"
            db = sqlite3.connect(path)
            db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
            db.close()
            with self.assertRaises(StudioStoreError):
                StudioStore(path)

    def test_job_validation(self):
        store = StudioStore.in_memory()
        for kind in ("", "Con Mayúsculas", "a;DROP", "x" * 41):
            with self.subTest(kind=kind), self.assertRaises(StudioStoreError):
                store.create_job(kind, {})
        for params in ([1, 2], {"x": float("nan")}, {"x": object()}, {"x": "a" * 70_000}):
            with self.subTest(params=str(params)[:30]), self.assertRaises(StudioStoreError):
                store.create_job("prueba", params)

    def test_claim_respects_order_and_time(self):
        store = StudioStore.in_memory()
        first = store.create_job("prueba", {"n": 1}, now=NOW)
        second = store.create_job("prueba", {"n": 2}, now=NOW)
        store.claim_next(NOW)  # se lleva el primero
        store.retry_later(first.id, "fallo", NOW + timedelta(minutes=5), now=NOW)
        self.assertEqual(store.claim_next(NOW).id, second.id)  # el primero aún no toca
        self.assertIsNone(store.claim_next(NOW))
        self.assertEqual(store.claim_next(NOW + timedelta(minutes=5)).id, first.id)

    def test_cancel(self):
        store = StudioStore.in_memory()
        queued = store.create_job("prueba", {}, now=NOW)
        self.assertTrue(store.request_cancel(queued.id, now=NOW))
        self.assertEqual(store.get_job(queued.id).status, "cancelled")
        self.assertFalse(store.request_cancel(queued.id, now=NOW))  # ya terminado
        self.assertFalse(store.request_cancel(999, now=NOW))


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.store = StudioStore.in_memory()
        self.clock = Clock()
        self.calls = []

    def runner(self, steps, **kw):
        return JobRunner(self.store, {"video": tuple(steps)}, now=self.clock, **kw)

    def test_runs_all_steps_in_order_passing_outputs(self):
        runner = self.runner([counting_step("guion", self.calls), counting_step("voz", self.calls)])
        job = runner.submit("video", {"idea": 7})
        self.assertTrue(runner.run_once())
        self.assertFalse(runner.run_once())  # no queda nada
        self.assertEqual(self.calls, ["guion", "voz"])
        self.assertEqual(self.store.get_job(job.id).status, "done")
        steps = {s.name: s for s in self.store.steps(job.id)}
        self.assertEqual(steps["voz"].output, {"hecho": "voz", "antes": ["guion"]})
        self.assertIsNone(steps["voz"].cost_eur)  # no se sabe ≠ 0 €

    def test_resumes_after_crash_without_repeating_done_steps(self):
        runner = self.runner([counting_step("guion", self.calls), counting_step("voz", self.calls)])
        job = runner.submit("video", {})
        # Simula un corte de luz: «guion» terminó, «voz» estaba a medias.
        self.store.claim_next(NOW)
        self.store.start_step(job.id, "guion", now=NOW)
        self.store.finish_step(job.id, "guion", {"texto": "hola"}, 1.0, now=NOW)
        self.store.start_step(job.id, "voz", now=NOW)
        self.assertEqual(self.store.recover_interrupted(now=NOW), [job.id])
        self.assertEqual(self.store.get_job(job.id).status, "queued")
        runner.run_once()
        self.assertEqual(self.calls, ["voz"])  # el guion NO se repite
        self.assertEqual(self.store.get_job(job.id).status, "done")

    def test_transient_error_retries_with_growing_wait(self):
        failures = [RuntimeError("Ollama ocupado"), RuntimeError("otra vez")]

        def flaky(ctx):
            if failures:
                raise failures.pop(0)
            return {"ok": True}

        runner = self.runner([counting_step("guion", self.calls), Step("voz", flaky)], max_attempts=3)
        job = runner.submit("video", {})
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).next_run_at, NOW + timedelta(seconds=30))
        self.assertFalse(runner.run_once())  # aún no toca
        self.clock.now = NOW + timedelta(seconds=30)
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).next_run_at, self.clock.now + timedelta(seconds=60))  # el doble
        self.clock.now += timedelta(seconds=60)
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "done")
        self.assertEqual(self.calls, ["guion"])  # el paso bueno solo una vez
        self.assertEqual({s.name: s.attempts for s in self.store.steps(job.id)}, {"guion": 1, "voz": 3})

    def test_gives_up_after_max_attempts(self):
        def broken(ctx):
            raise OSError("sin red")

        runner = self.runner([Step("buscar", broken)], max_attempts=2)
        job = runner.submit("video", {})
        runner.run_once()
        self.clock.now += timedelta(hours=1)
        runner.run_once()
        failed = self.store.get_job(job.id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("tras 2 intentos", failed.error)

    def test_permanent_error_fails_at_once(self):
        def no_license(ctx):
            raise StepFailed("La imagen no tiene licencia registrada")

        runner = self.runner([Step("montaje", no_license), counting_step("miniatura", self.calls)])
        job = runner.submit("video", {})
        runner.run_once()
        failed = self.store.get_job(job.id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("licencia", failed.error)
        self.assertEqual(self.calls, [])

    def test_output_too_big_is_permanent(self):
        runner = self.runner([Step("guion", lambda ctx: {"texto": "a" * 300_000})])
        job = runner.submit("video", {})
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "failed")

    def test_cancel_while_running_stops_after_current_step(self):
        holder = {}

        def first(ctx):
            holder["runner"].cancel(ctx.job.id)
            self.assertTrue(ctx.cancelled())
            return {}

        runner = self.runner([Step("guion", first), counting_step("voz", self.calls)])
        holder["runner"] = runner
        job = runner.submit("video", {})
        runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "cancelled")
        self.assertEqual(self.calls, [])

    def test_cancel_requested_then_crash_stays_cancelled(self):
        runner = self.runner([counting_step("guion", self.calls)])
        job = runner.submit("video", {})
        self.store.claim_next(NOW)
        self.store.request_cancel(job.id, now=NOW)
        self.store.recover_interrupted(now=NOW)
        self.assertEqual(self.store.get_job(job.id).status, "cancelled")

    def test_unknown_kind_and_bad_definitions(self):
        runner = self.runner([counting_step("guion", self.calls)])
        with self.assertRaises(StudioStoreError):
            runner.submit("otro", {})
        orphan = self.store.create_job("de_otra_version", {}, now=NOW)
        runner.run_once()
        self.assertEqual(self.store.get_job(orphan.id).status, "failed")
        with self.assertRaises(ValueError):
            JobRunner(self.store, {"video": (Step("a", dict), Step("a", dict))})
        with self.assertRaises(ValueError):
            JobRunner(self.store, {"video": ()})

    def test_broken_listener_does_not_stop_the_work(self):
        def listener(job):
            raise RuntimeError("la web falló")

        runner = self.runner([counting_step("guion", self.calls)], on_change=listener)
        job = runner.submit("video", {})
        with self.assertLogs("arthur.studio.jobs", "ERROR"):
            runner.run_once()
        self.assertEqual(self.store.get_job(job.id).status, "done")

    def test_listener_sees_progress(self):
        seen = []
        runner = self.runner([counting_step("guion", self.calls)], on_change=lambda j: seen.append(j.status))
        runner.submit("video", {})
        runner.run_once()
        self.assertEqual(seen[0], "queued")
        self.assertIn("running", seen)
        self.assertEqual(seen[-1], "done")

    def test_background_thread(self):
        finished = threading.Event()
        runner = JobRunner(self.store, {"video": (Step("guion", lambda ctx: {}),)}, poll=0.01,
                           on_change=lambda j: j.status == "done" and finished.set())
        runner.start()
        try:
            runner.submit("video", {})
            self.assertTrue(finished.wait(5))
        finally:
            runner.stop()


if __name__ == "__main__":
    unittest.main()
