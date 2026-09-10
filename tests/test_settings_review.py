"""Focused settings integration regressions using only synthetic data and fetchers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from osu_coach.app import Coach
from osu_coach.storage.discovery_store import DiscoveryStore
from osu_coach.core.engine import apply_player_profile, recommend
from osu_coach.settings import DEFAULTS, get_setting, settings_context, validate_settings
from tests.test_discovery_store import beatmap


class SettingsDiscoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DiscoveryStore(self.temp.name)

    def tearDown(self):
        self.store.stop.set()
        if self.store.thread:
            self.store.thread.join(3)
        self.temp.cleanup()

    def wait_for_search(self):
        self.store.thread.join(3)
        self.assertFalse(self.store.thread.is_alive())

    def test_deep_recovery_fetch_can_find_a_map_inside_the_engine_stage(self):
        values = validate_settings({"recovery_drop": 1.0})
        self.store.set_settings(values)
        base = {"baseline": 4.5, "phase": "training", "window": [],
                "challenge_unlocked": False,
                "session": {"adjustment": -1.0, "focus": "Recuperar control",
                            "message": "Practicar un mapa mas comodo"}}
        with settings_context(values):
            profile = apply_player_profile(base, {"progression": {"mode": "recover"}})
            practice = next(group for group in recommend([], profile)
                            if group["stage"] == "practice")
            need = {"min_stars": practice["target"] - get_setting("star_tolerance_below"),
                    "max_stars": practice["target"] + get_setting("star_tolerance_above")}
        candidate = beatmap()
        candidate["stars"] = practice["target"]

        def fetch(lower, upper, *, requirements, **_):
            # Simulate the two independent filters applied by the public source.
            inside = lower <= candidate["stars"] <= upper and any(
                rule["min_stars"] <= candidate["stars"] <= rule["max_stars"]
                for rule in requirements)
            return {"maps": [candidate] if inside else [], "next_cursor": None, "exhausted": True}

        self.store.batch_fetcher = Mock(side_effect=fetch)
        self.assertTrue(self.store.sync(4.5, needs=[need]))
        self.wait_for_search()
        self.assertEqual([candidate], self.store.maps)
        lower, upper = self.store.batch_fetcher.call_args.args
        self.assertLessEqual(lower, need["min_stars"])
        self.assertGreaterEqual(upper, need["max_stars"])

    def test_daily_discovery_envelope_also_covers_configured_recovery(self):
        values = validate_settings({"recovery_drop": 1.0})
        self.store.set_settings(values)
        fetch = Mock(return_value=[])
        self.store.fetcher = fetch
        self.assertTrue(self.store.sync(4.5))
        self.wait_for_search()
        lower, upper = fetch.call_args.args
        self.assertLessEqual(lower, 4.5 - values["recovery_drop"] - values["star_tolerance_below"])
        self.assertGreaterEqual(upper, 4.5 + values["challenge_increment"] + values["star_tolerance_above"])

    def test_stale_failure_does_not_poison_retry_or_new_worker_settings(self):
        entered, release = threading.Event(), threading.Event()
        observed = []

        def old_fetch(*_):
            observed.append(get_setting("quality_min_rating"))
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test worker was not released")
            observed.append(get_setting("quality_min_rating"))
            raise OSError("Synthetic old request failure")

        self.store.fetcher = old_fetch
        try:
            self.assertTrue(self.store.sync(4.5))
            self.assertTrue(entered.wait(2))
            self.store.set_settings(validate_settings({"quality_min_rating": 9.5}))
            release.set()
            self.wait_for_search()
            self.assertEqual([8, 8], observed)
            self.assertEqual("ready", self.store.status["state"])
            self.assertEqual(0, self.store.next_retry_at)
            self.assertFalse(self.store.retry_path.exists())
            self.assertFalse(self.store.path.exists())

            candidate = beatmap()
            candidate["popularity"]["rating"] = 9.75

            def new_fetch(*_):
                observed.append(get_setting("quality_min_rating"))
                return [candidate]

            self.store.fetcher = new_fetch
            self.assertTrue(self.store.sync(4.5, force=True))
            self.wait_for_search()
            self.assertEqual(9.5, observed[-1])
            self.assertEqual([candidate], self.store.maps)
            saved = json.loads(self.store.path.read_text(encoding="utf-8"))
            self.assertEqual(9.5, saved["quality_policy"]["min_rating"])
        finally:
            release.set()


class SettingsCoachIsolationReviewTests(unittest.TestCase):
    def test_two_coaches_keep_state_and_background_filters_separate(self):
        coaches = []
        with tempfile.TemporaryDirectory() as directory, patch("osu_coach.app.detect_maps", return_value=None):
            try:
                for index, settings in enumerate((
                    {"initial_stars": 2.0, "reference_plays": 60, "quality_min_rating": 8.0},
                    {"initial_stars": 5.0, "reference_plays": 120, "quality_min_rating": 9.5},
                )):
                    args = argparse.Namespace(data_dir=str(Path(directory, str(index))), demo=False,
                        maps=None, no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
                    coach = Coach(args)
                    coaches.append(coach)
                    coach.update_settings(settings)
                barrier = threading.Barrier(2)
                observed = []

                def fetch(*_):
                    before = get_setting("quality_min_rating")
                    barrier.wait(timeout=3)
                    after = get_setting("quality_min_rating")
                    observed.append((before, after))
                    return [beatmap()]

                for coach in coaches:
                    coach.discovery_store.fetcher = fetch
                    self.assertTrue(coach.discovery_store.sync(coach.settings["initial_stars"]))
                for coach in coaches:
                    coach.discovery_store.thread.join(4)
                    self.assertFalse(coach.discovery_store.thread.is_alive())
                self.assertCountEqual([(8.0, 8.0), (9.5, 9.5)], observed)

                with settings_context({"initial_stars": 10.0, "quality_min_rating": 10.0}):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        states = list(pool.map(lambda coach: coach.state(), coaches))
                    self.assertEqual(10.0, get_setting("initial_stars"))
                self.assertEqual(DEFAULTS["initial_stars"], get_setting("initial_stars"))
                self.assertEqual([2.0, 5.0], [state["profile"]["baseline"] for state in states])
                self.assertEqual([60, 120], [state["settings"]["values"]["reference_plays"] for state in states])
                self.assertEqual([1, 0], [state["discovery"]["candidate_count"] for state in states])
                self.assertEqual([8.0, 9.5], [state["discovery"]["quality_policy"]["min_rating"] for state in states])
            finally:
                for coach in coaches:
                    coach.close()
                    if coach.discovery_store.thread:
                        coach.discovery_store.thread.join(4)
                    coach.db.close()


if __name__ == "__main__":
    unittest.main()
