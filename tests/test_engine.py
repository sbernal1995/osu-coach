"""Regresiones de progresión reciente y separación de perfiles locales."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from osu_coach.settings import settings_context
from unittest.mock import patch

from osu_coach import app
from osu_coach.core import engine


NOW = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)


def play(index, *, stars=3.0, accuracy=99.0, key=None, **changes):
    result = {
        "id": f"attempt-{index}",
        "beatmap_key": key or f"map-{index}",
        "played_at": (NOW - timedelta(minutes=60 - index)).isoformat(),
        "mode": 0, "stars": stars, "accuracy": accuracy,
        "object_count": 400, "judged_objects": 400, "misses": 0,
        "max_combo": 490, "map_max_combo": 500,
        "passed": True, "completion": 1.0,
        "bpm": 150, "ar": 7.0, "length": 120,
        "player": "Jugador", "client": "lazer", "mods": [],
        "mod_key": app.DEFAULT_MOD_KEY,
    }
    result.update(changes)
    return result


def beatmap(index, **changes):
    result = {
        "key": f"candidate-{index}", "id": index + 1, "set_id": index + 10,
        "title": f"Canción {index}", "version": "Normal", "mode": 0,
        "stars": 3.0, "bpm": 150, "ar": 7.0, "length": 120,
        "path": str(Path(f"synthetic-{index}.osu").resolve()),
    }
    result.update(changes)
    return result


class ProgressionTests(unittest.TestCase):
    def test_historical_rank_and_pp_have_no_effect(self):
        recent = [play(i) for i in range(5)]
        with_history = copy.deepcopy(recent)
        for item in with_history:
            item.update(rank=1, global_rank=1, pp=99_999, historical_best_pp=50_000)
        a, b = engine.assess(recent, NOW), engine.assess(with_history, NOW)
        a.pop("window")
        b.pop("window")
        a.pop("session_window")
        b.pop("session_window")
        self.assertEqual(a, b)

    def test_reference_keeps_eight_day_old_plays_but_ignores_older_than_thirty_and_future(self):
        recent = play(1)
        eight_days = play(2, stars=3, played_at=(NOW - timedelta(days=8)).isoformat())
        old = play(4, stars=9, played_at=(NOW - timedelta(days=31)).isoformat())
        future = play(3, stars=9, played_at=(NOW + timedelta(minutes=1)).isoformat())
        self.assertEqual(engine.recent_window([old, recent, eight_days, future], NOW), [eight_days, recent])

    def test_reset_timestamp_excludes_earlier_results(self):
        items = [play(i) for i in range(5)]
        self.assertEqual(engine.recent_window(items, NOW, items[3]["played_at"]), items[3:])

    def test_window_uses_one_hundred_latest_results(self):
        items = [play(i, played_at=(NOW - timedelta(minutes=140 - i)).isoformat()) for i in range(140)]
        self.assertEqual(engine.recent_window(list(reversed(items)), NOW), items[-100:])

    def test_uncertain_duplicates_other_modes_and_invalid_stars_are_ignored(self):
        valid = play(0)
        items = [valid, dict(valid), play(1, needs_confirmation=True),
                 play(2, mode=3), play(3, stars=float("nan")),
                 play(4, excluded=True), play(5, played_at="invalid")]
        self.assertEqual(engine.recent_window(items, NOW), [valid])

    def test_two_failures_reduce_the_working_difficulty(self):
        recent = [play(i, accuracy=95) for i in range(5)]
        before = engine.assess(recent, NOW)
        recent.extend(play(i, passed=False, accuracy=78, misses=25,
                           judged_objects=200, completion=.5) for i in (5, 6))
        after = engine.assess(recent, NOW)
        self.assertLess(after["baseline"], before["baseline"])
        self.assertEqual(after["trend"], "down")
        self.assertFalse(after["challenge_unlocked"])

    def test_single_lucky_result_has_only_a_small_effect_without_changing_session_trend(self):
        recent = [play(0)] + [play(i, accuracy=95) for i in range(1, 5)]
        before = engine.assess(recent, NOW)
        after = engine.assess(recent + [play(5, stars=6)], NOW)
        self.assertLessEqual(after["baseline"] - before["baseline"], .1)
        self.assertNotEqual(after["trend"], "up")
        self.assertEqual(after["challenge_unlocked"], before["challenge_unlocked"])

    def test_expiring_oldest_result_cannot_promote_a_lucky_outlier(self):
        recent = [play(0, stars=2, accuracy=95), play(1, stars=6)]
        recent += [play(i, stars=2, accuracy=95) for i in range(2, 21)]
        before = engine.assess(recent[:20], NOW)
        after = engine.assess(recent, NOW)
        self.assertLessEqual(after["baseline"], before["baseline"] + .15)
        self.assertLessEqual(after["baseline"], 2.3)

    def test_repeating_one_map_cannot_complete_calibration_or_promote(self):
        profile = engine.assess([play(i, key="same-map") for i in range(10)], NOW)
        self.assertEqual(profile["phase"], "calibrating")
        self.assertEqual(profile["baseline"], 3.0)
        self.assertEqual(profile["distinct_maps"], 1)
        self.assertFalse(profile["challenge_unlocked"])

    def test_multiple_solid_maps_allow_only_a_small_step(self):
        profile = engine.assess([play(i) for i in range(5)], NOW)
        self.assertEqual(profile["phase"], "training")
        self.assertGreater(profile["baseline"], 3.0)
        self.assertLessEqual(profile["baseline"], 3.15)
        self.assertTrue(profile["challenge_unlocked"])

    def test_challenge_stays_locked_after_a_recent_struggling_play(self):
        recent = [play(i) for i in range(5)]
        recent += [play(5, accuracy=100), play(6, accuracy=100), play(7, accuracy=84)]
        profile = engine.assess(recent, NOW)
        self.assertFalse(profile["challenge_unlocked"])

    def test_repeating_one_map_after_calibration_does_not_unlock_a_challenge(self):
        recent = [play(i, accuracy=95) for i in range(5)]
        recent += [play(i, key="one-practised-map") for i in range(5, 8)]
        profile = engine.assess(recent, NOW)
        self.assertEqual(profile["phase"], "training")
        self.assertFalse(profile["challenge_unlocked"])

    def test_partial_attempts_use_judged_objects_for_miss_rate(self):
        partial = play(0, object_count=1000, judged_objects=100, misses=10)
        self.assertEqual(engine.rates(partial)[0], .1)


class RecommendationTests(unittest.TestCase):
    def test_map_on_decimal_difficulty_boundary_is_included(self):
        profile = engine.assess([play(i) for i in range(5)], NOW)
        profile.update(baseline=4.95, challenge_unlocked=True,
                       training_adjustments={"challenge_increment": .15})
        groups = engine.recommend([beatmap(1, stars=4.8)], profile,
                                  stages={"challenge"})
        self.assertEqual(len(groups), 1)
        self.assertEqual([item["key"] for item in groups[0]["maps"]], ["candidate-1"])

    @settings_context({"bpm_hard_limit": True})
    def test_recommendations_respect_difficulty_tempo_and_reading_limits(self):
        profile = engine.assess([play(i) for i in range(5)], NOW)
        maps = [beatmap(i, stars=2.5 + .05 * i) for i in range(24)]
        invalid = [beatmap(100, bpm=166), beatmap(101, ar=7.71),
                   beatmap(103, stars=9),
                   beatmap(104, mode=3), beatmap(105, stars=float("nan"))]
        groups = engine.recommend(maps + invalid, profile)
        chosen = []
        for group in groups:
            self.assertLessEqual(len(group["maps"]), 3)
            for item in group["maps"]:
                chosen.append(item["key"])
                self.assertLessEqual(item["stars"], group["target"] + .180001)
                self.assertGreaterEqual(item["stars"], max(.1, group["target"] - .300001))
                self.assertLessEqual(item["bpm"], 165)
                self.assertLessEqual(item["ar"], 7.7)
                self.assertEqual(item["mode"], 0)
                self.assertNotIn("path", item)
        self.assertTrue(chosen)
        self.assertEqual(len(chosen), len(set(chosen)))
        self.assertFalse(set(chosen) & {m["key"] for m in invalid})

    def test_calibration_does_not_add_a_challenge_step(self):
        profile = engine.assess([play(0)], NOW)
        groups = engine.recommend([], profile)
        self.assertEqual(groups[-1]["label"], "Consolidar")
        self.assertEqual(groups[-1]["target"], profile["baseline"])

    def test_maps_from_one_set_are_not_repeated_within_a_stage(self):
        profile = engine.assess([play(i) for i in range(5)], NOW)
        groups = engine.recommend([beatmap(i, set_id=42) for i in range(20)], profile)
        self.assertTrue(any(group["maps"] for group in groups))
        self.assertTrue(all(len(group["maps"]) <= 1 for group in groups))


class InlineThread:
    """Ejecuta el cálculo de mods de forma determinista durante estas pruebas."""

    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class LocalProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        args = SimpleNamespace(data_dir=self.tmp.name, demo=False, maps=None,
                               no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = app.Coach(args)
        self.addCleanup(self.coach.close)
        self.addCleanup(self.coach.db.close)
        self.coach.config["since"] = "2020-01-01T00:00:00+00:00"

    def test_profiles_separate_player_client_mods_and_speed(self):
        original = play(0)
        profile = app.profile_key(original)
        for field, value in (("player", "Otra persona"), ("client", "stable"),
                             ("mod_key", '{"mods":[],"rate":1.2}')):
            with self.subTest(field=field):
                self.assertNotEqual(profile, app.profile_key({**original, field: value}))
        self.assertEqual(profile, app.profile_key({**original, "player": "JUGADOR"}))

    def test_other_profiles_cannot_displace_this_profiles_training_memory(self):
        scores = [play(i) for i in range(25)]
        other = [play(i + 1000, player="Otra persona") for i in range(501)]
        with self.coach.db:
            self.coach.db.executemany("INSERT INTO plays VALUES (?, ?, 'accepted')",
                                     [(p["id"], json.dumps(p)) for p in scores + other])
        self.coach.active = app.profile_key(scores[0])
        self.assertEqual(len(self.coach.training_plays()), 25)
        self.assertEqual(self.coach.state()["profile"]["attempts"], 25)

    def test_pending_and_duplicate_plays_do_not_enter_training(self):
        accepted = play(0)
        pending = play(1, needs_confirmation=True)
        self.coach.add_play(accepted)
        self.coach.add_play(dict(accepted))
        self.coach.add_play(pending)
        self.assertEqual(len(self.coach.plays()), 1)
        self.assertEqual(len(self.coach.plays("pending")), 1)
        self.coach.confirm(pending["id"], True)
        self.assertEqual(len(self.coach.plays()), 2)
        self.assertFalse(self.coach.plays()[0]["needs_confirmation"])

    def test_accepted_plays_and_reset_survive_reopening(self):
        self.coach.add_play(play(0))
        self.coach.add_play(play(1, needs_confirmation=True))
        self.coach.reset()
        since = self.coach.config["since"]
        self.coach.db.commit()
        reopened = app.Coach(self.coach.args)
        self.addCleanup(reopened.close)
        self.addCleanup(reopened.db.close)
        self.assertEqual(len(reopened.plays()), 1)
        self.assertEqual(reopened.plays("pending"), [])
        self.assertEqual(reopened.config["since"], since)
        self.assertEqual(engine.assess(reopened.plays(), since=since)["attempts"], 0)

    def test_mod_calculation_uses_client_and_observed_clock_rate(self):
        sample = play(0, client="stable", mods=[{"acronym": "DT"}],
                      mod_key='{"mods":[{"acronym":"DT"}],"rate":1.2}')
        self.coach.catalog = [beatmap(0)]
        with patch("osu_coach.app.threading.Thread", InlineThread):
            with patch("osu_coach.beatmaps.catalog.difficulty_for", return_value={"stars": 3.5}) as calculate:
                self.coach.catalog_for(sample)
        calculate.assert_called_once()
        self.assertEqual(calculate.call_args.kwargs.get("clock_rate"), 1.2)
        self.assertIs(calculate.call_args.kwargs.get("lazer"), False)

    def test_stable_and_lazer_cannot_share_adjusted_difficulty_cache(self):
        sample = play(0, client="stable", mods=[{"acronym": "HR"}],
                      mod_key='{"mods":[{"acronym":"HR"}],"rate":1.0}')
        self.coach.catalog = [beatmap(0)]
        with patch("osu_coach.app.threading.Thread", InlineThread):
            with patch("osu_coach.beatmaps.catalog.difficulty_for", return_value={"stars": 3.5}) as calculate:
                self.coach.catalog_for(sample)
                self.coach.catalog_for({**sample, "client": "lazer"})
        self.assertEqual(calculate.call_count, 2)

    def test_custom_speed_without_mod_acronym_still_recalculates_maps(self):
        self.coach.catalog = [beatmap(0)]
        sample = play(0, mods=[], mod_key='{"mods":[],"rate":1.2}')
        with patch("osu_coach.app.threading.Thread", InlineThread):
            with patch("osu_coach.beatmaps.catalog.difficulty_for", return_value={"stars": 3.5}) as calculate:
                self.coach.catalog_for(sample)
        calculate.assert_called_once()
        self.assertEqual(calculate.call_args.kwargs.get("clock_rate"), 1.2)

    def test_rescan_cannot_publish_an_adjustment_from_the_previous_catalog(self):
        pending = []

        class QueuedThread(InlineThread):
            def start(self):
                pending.append(self.target)

        old_map, new_map = beatmap(0), beatmap(1)
        sample = play(0, mods=[{"acronym": "DT"}],
                      mod_key='{"mods":[{"acronym":"DT"}],"rate":1.5}')
        self.coach.catalog = [old_map]
        self.coach.config["maps_path"] = self.tmp.name
        with patch("osu_coach.app.threading.Thread", QueuedThread):
            with patch("osu_coach.beatmaps.catalog.difficulty_for", return_value={"stars": 3.5}):
                self.coach.catalog_for(sample)
                self.coach.rescan()
                with patch("osu_coach.beatmaps.catalog.scan_catalog", return_value=[new_map]):
                    pending[1]()  # El nuevo catálogo termina antes que el cálculo viejo.
                pending[0]()
                result, _ = self.coach.catalog_for(sample)
                self.assertNotIn(old_map["key"], [row["key"] for row in result])


if __name__ == "__main__":
    unittest.main()
