import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arthur.core.config import Config, ConfigError, config_from_dict, load_config

EXAMPLE = Path(__file__).parent.parent / "config" / "config.example.toml"


class ConfigTest(unittest.TestCase):
    def test_empty_dict_gives_defaults(self):
        self.assertEqual(config_from_dict({}), Config())

    def test_values_are_applied(self):
        cfg = config_from_dict({"assistant": {"name": "Viernes"}, "logging": {"level": "DEBUG"}})
        self.assertEqual(cfg.assistant.name, "Viernes")
        self.assertEqual(cfg.logging.level, "DEBUG")
        self.assertEqual(cfg.assistant.history_limit, 20)
        self.assertEqual(cfg.brain.provider, "ollama")

    def test_rejects_unknown_section_and_key(self):
        with self.assertRaisesRegex(ConfigError, "Secciones desconocidas"):
            config_from_dict({"asistant": {}})
        with self.assertRaisesRegex(ConfigError, "Claves desconocidas"):
            config_from_dict({"assistant": {"nmae": "x"}})

    def test_rejects_wrong_types(self):
        with self.assertRaisesRegex(ConfigError, "int"):
            config_from_dict({"assistant": {"history_limit": "20"}})
        with self.assertRaisesRegex(ConfigError, "int"):
            config_from_dict({"assistant": {"history_limit": True}})
        with self.assertRaisesRegex(ConfigError, "sección"):
            config_from_dict({"assistant": "ARTHUR"})

    def test_rejects_invalid_values(self):
        for raw in (
            {"assistant": {"name": "  "}},
            {"assistant": {"history_limit": 0}},
            {"brain": {"provider": "skynet"}},
            {"brain": {"ollama_url": "http://10.0.0.1:11434"}},
            {"logging": {"level": "LOUD"}},
        ):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                config_from_dict(raw)

    def test_example_file_is_valid(self):
        self.assertEqual(load_config(EXAMPLE), Config())

    def test_missing_explicit_file_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, "No existe"):
            load_config(Path("/no/existe/config.toml"))

    def test_missing_default_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            self.assertEqual(load_config(), Config())

    def test_invalid_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[assistant\n")
            with self.assertRaisesRegex(ConfigError, "TOML no válido"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
