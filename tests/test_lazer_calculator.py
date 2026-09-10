"""Current native calculator, explicit failures and mod consistency."""
import queue
import unittest
from unittest.mock import patch
from osu_coach.integrations import lazer_calculator as engine
from test_catalog import MAP


class LazerCalculatorTests(unittest.TestCase):
    def test_current_formula_regression(self):
        result = engine.calculator.calculate(MAP.encode())
        self.assertAlmostEqual(1.5445163686615362, result["stars"], places=6)
        self.assertEqual(engine.CALCULATOR_ID, result["calculator"])
        self.assertEqual(12, result["max_combo"])
        self.assertEqual(12, result["object_count"])
        self.assertIn("reading", result)

    def test_equivalent_speed_inputs_have_identical_difficulty(self):
        content = MAP.encode()
        direct = engine.calculator.calculate(content, clock_rate=1.25)
        custom = engine.calculator.calculate(content, [{"acronym": "DT", "settings": {"speed_change": 1.25}}])
        override = engine.calculator.calculate(content, "DT", clock_rate=1.25)
        self.assertEqual(direct, custom)
        self.assertEqual(direct, override)
        self.assertGreater(direct["stars"], engine.calculator.calculate(content)["stars"])
        self.assertEqual(engine.calculator.calculate(content), engine.calculator.calculate(content, "DT", clock_rate=1))

    def test_stable_uses_classic_rules(self):
        content = MAP.encode()
        self.assertEqual(engine.calculator.calculate(content, lazer=False),
                         engine.calculator.calculate(content, "CL"))
        self.assertEqual([{"acronym": "CL"}], engine.normalise_mods(None, lazer=False))

    def test_difficulty_adjust_settings_are_preserved(self):
        result = engine.calculator.calculate(MAP.encode(), [{"acronym": "DA", "settings": {"approach_rate": 9.5}}])
        self.assertEqual(9.5, result["ar"])

    def test_nightcore_bitflags_do_not_double_speed(self):
        self.assertEqual(engine.calculator.calculate(MAP.encode(), "NC"),
                         engine.calculator.calculate(MAP.encode(), 64 | 512))

    def test_unknown_or_variable_mods_fail_instead_of_silently_using_nomod(self):
        for mods in ("ZZ", "WU", "WD", "AS", True, -1, {"acronym": "DT", "settings": []},
                     [{"acronym": "DT", "settings": {"speed_change": 3}}], "DTHT"):
            with self.subTest(mods=mods), self.assertRaises(ValueError):
                engine.normalise_mods(mods)

    def test_rates_that_native_library_would_clamp_are_rejected(self):
        for rate in (.25, 3, float("nan"), float("inf")):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                engine.normalise_mods("DT", clock_rate=rate)

    def test_missing_runtime_never_falls_back_to_old_formula(self):
        calc = engine.Calculator()
        self.addCleanup(calc.close)
        with patch.object(engine, "installed", return_value=False), self.assertRaisesRegex(RuntimeError, "Falta preparar"):
            calc.calculate(MAP.encode())
        self.assertIsNone(calc.process)

    def test_invalid_worker_protocol_is_explicit(self):
        for message in ("not json", "[]", "null", None):
            with self.subTest(message=message):
                calc = engine.Calculator()
                calc.messages = queue.Queue()
                calc.messages.put(message)
                with self.assertRaises(RuntimeError):
                    calc._read()

    def test_timeout_closes_worker(self):
        calc = engine.Calculator()
        calc.messages = queue.Queue()
        with patch.object(calc.messages, "get", side_effect=queue.Empty), patch.object(calc, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "tardó demasiado"):
                calc._read()
            close.assert_called_once()

    def test_missing_native_binary_invalidates_installation(self):
        with patch.object(engine.Path, "is_file", return_value=False):
            self.assertFalse(engine.installed())

    def test_unsupported_platform_has_clear_message(self):
        with patch.object(engine.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(RuntimeError, "La demo sigue disponible"):
                engine.native_package()
