"""Tests del registro (arthur.log): quién puede leerlo."""

import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arthur.core.config import LoggingConfig
from arthur.core.logging_setup import setup_logging


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class LogFilePermissionsTest(unittest.TestCase):
    """El registro cuenta qué herramientas se usan, qué falla y (con DEBUG) lo que dices: solo para tu usuario,
    como audit.jsonl y la memoria. Antes nacía con permisos 644 (los de la máscara normal)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.log_dir = self.state / "arthur"
        self.addCleanup(self.close_handlers)
        # La máscara normal: el test no debe depender de la del equipo (y se restaura al terminar).
        self.addCleanup(os.umask, os.umask(0o022))

    def close_handlers(self):
        logger = logging.getLogger("arthur")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_new_log_file_is_private(self):
        path = setup_logging(LoggingConfig())
        logging.getLogger("arthur").info("hola")
        self.assertEqual((path, mode(path), mode(path.parent)), (self.log_dir / "arthur.log", 0o600, 0o700))
        self.assertIn("hola", path.read_text())

    def test_rotated_files_are_private_too(self):
        path = setup_logging(LoggingConfig())
        logging.getLogger("arthur").info("antes")
        file_handler = next(h for h in logging.getLogger("arthur").handlers if hasattr(h, "doRollover"))
        file_handler.doRollover()
        logging.getLogger("arthur").info("después")
        self.assertEqual(mode(path), 0o600)  # el fichero nuevo, creado tras la rotación
        self.assertEqual(mode(path.with_name("arthur.log.1")), 0o600)

    def test_logs_created_by_older_versions_are_tightened(self):
        self.log_dir.mkdir(parents=True)
        for name in ("arthur.log", "arthur.log.1"):
            (self.log_dir / name).write_text("antiguo\n")
            (self.log_dir / name).chmod(0o644)
        path = setup_logging(LoggingConfig())
        self.assertEqual((mode(path), mode(path.with_name("arthur.log.1"))), (0o600, 0o600))
        self.assertEqual(path.read_text(), "antiguo\n")  # y no se pierde lo que había


if __name__ == "__main__":
    unittest.main()
