import os
import tempfile
import unittest
from pathlib import Path

from ledbar.config import Config, ConfigError, config_from_dict, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = load_config("/nonexistent/ledbar.toml")
        self.assertEqual(config.leds.count, 24)
        self.assertEqual(config.rgb("boot"), config.rgb("bar"))

    def test_load_file_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[leds]\ncount = 30\nreverse = true\ncolor_order = "grb"\n[thermal]\ncritical_c = 95\n')
            config = load_config(path)
            self.assertEqual(config.leds.count, 30)
            self.assertTrue(config.leds.reverse)
            self.assertEqual(config.leds.color_order, "GRB")
            self.assertEqual(config.thermal.critical_c, 95.0)
            os.environ["LEDBAR_CONFIG"] = str(path)
            try:
                self.assertEqual(load_config().leds.count, 30)
            finally:
                del os.environ["LEDBAR_CONFIG"]

    def test_unknown_keys_warn(self):
        config = config_from_dict({"leds": {"cont": 5}, "bogus": {"x": 1}})
        self.assertEqual(len(config.warnings), 2)
        self.assertEqual(config.leds.count, 24)

    def test_validation_errors(self):
        for data in (
            {"leds": {"count": 0}},
            {"leds": {"brightness": 101}},
            {"leds": {"color_order": "RGBW"}},
            {"idle": {"mode": "disco"}},
            {"colors": {"bar": "blue"}},
            {"power": {"on_exit": "explode"}},
            {"leds": {"count": "many"}},
            {"output": {"backend": "hue"}},
        ):
            with self.assertRaises(ConfigError, msg=str(data)):
                config_from_dict(data)

    def test_bad_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[leds\ncount = 1")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_example_config_matches_defaults(self):
        import tomllib

        example = Path(__file__).resolve().parent.parent / "ledbar" / "config.example.toml"
        config = config_from_dict(tomllib.load(open(example, "rb")))
        self.assertEqual(config.warnings, [])
        for section in ("leds", "openrgb", "colors", "boot", "idle", "game", "progress", "steam", "thermal", "faults", "power"):
            self.assertEqual(getattr(config, section), getattr(Config(), section), section)


if __name__ == "__main__":
    unittest.main()
