"""Persistent per-player difficulty feel ("¿Cómo lo sentiste?")."""
import math
import sqlite3
import tempfile
import unittest

from osu_coach.core import feel as feel_core
from osu_coach.storage.feel_store import FeelStore


class FeelStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(self.temp.name + "/coach.sqlite3")
        self.store = FeelStore(self.db)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_set_map_snapshot_and_clear(self):
        self.store.set("jugador", "map-1", 0.5)
        self.store.set("jugador", "map-1", 0.5)
        self.store.set("jugador", "map-2", -0.25)
        self.store.set("otra", "map-1", 0.25)
        self.assertEqual({"map-1": 0.5, "map-2": -0.25}, self.store.map("jugador"))
        self.assertEqual(2, self.store.snapshot("jugador")["total"])
        self.store.set("jugador", "map-1", 0.0)
        self.assertEqual({"map-2": -0.25}, self.store.map("jugador"))

    def test_clear_is_idempotent(self):
        self.store.clear("jugador", "map-1")
        self.store.clear("jugador", "map-1")
        self.assertEqual({}, self.store.map("jugador"))

    def test_set_validates_owner_key_and_offset(self):
        with self.assertRaises(ValueError):
            self.store.set("", "map-1", 0.5)
        with self.assertRaises(ValueError):
            self.store.set("jugador", "   ", 0.5)
        with self.assertRaises(ValueError):
            self.store.set("jugador", "x" * 101, 0.5)
        with self.assertRaises(ValueError):
            self.store.set("jugador", "map-1", math.nan)
        with self.assertRaises(ValueError):
            self.store.set("jugador", "map-1", True)


class FeelCoreTests(unittest.TestCase):
    def test_offsets_are_symmetric_around_zero(self):
        offsets = feel_core.offsets_for_step(0.25)
        self.assertEqual(0.0, offsets["justo"])
        self.assertEqual(-0.25, offsets["más fácil"])
        self.assertEqual(0.25, offsets["más difícil"])
        self.assertEqual(-0.5, offsets["mucho más fácil"])
        self.assertEqual(0.5, offsets["mucho más difícil"])

    def test_step_is_clamped_and_guarded(self):
        self.assertEqual(0.25, feel_core.offsets_for_step("raro")["más difícil"])
        self.assertEqual(0.5, feel_core.offsets_for_step(0.5)["más difícil"])
        self.assertEqual(0.05, feel_core.offsets_for_step(0.001)["más difícil"])
        self.assertEqual(1.0, feel_core.offsets_for_step(99)["más difícil"])

    def test_label_for_offset_roundtrips_configured_labels(self):
        for label, offset in feel_core.offsets_for_step(0.5).items():
            self.assertEqual(label, feel_core.label_for_offset(offset, 0.5))
        self.assertIsNone(feel_core.label_for_offset(0.31, 0.25))
        self.assertIsNone(feel_core.label_for_offset("nope", 0.25))

    def test_apply_feel_clamps_to_difficulty_range(self):
        self.assertEqual(feel_core.FEEL_MIN, feel_core.apply_feel(0.0, 0.0))
        self.assertEqual(feel_core.FEEL_MAX, feel_core.apply_feel(12.0, 0.0))
        self.assertEqual(3.75, feel_core.apply_feel(3.5, 0.25))
        self.assertEqual(3.25, feel_core.apply_feel(3.5, -0.25))


if __name__ == "__main__":
    unittest.main()