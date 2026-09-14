"""Recommendation cards receive consistent goals from the filtered profile."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from osu_coach.core import engine
from osu_coach.core.tag_analysis import analyze_tags


NOW = datetime(2026, 9, 8, 20, tzinfo=timezone.utc)


def play(index, **changes):
    result = {"id": f"play-{index}", "beatmap_key": f"played-{index}",
              "played_at": (NOW - timedelta(minutes=50 - index)).isoformat(),
              "mode": 0, "client": "lazer", "mods": [], "stars": 4.5,
              "accuracy": 97, "misses": 1, "object_count": 400,
              "judged_objects": 400, "map_max_combo": 500, "max_combo": 450,
              "passed": True, "completion": 1, "bpm": 170, "ar": 8.5, "length": 100}
    result.update(changes)
    return result


def catalog(baseline):
    maps = []
    for index in range(24):
        maps.append({"key": f"candidate-{index}", "id": index + 100, "set_id": index + 200,
                     "title": f"Song {index}", "artist": "Artist", "version": "Insane",
                     "mode": 0, "stars": baseline - .45 + (index % 12) * .06,
                     "object_count": (10, 250, 750)[index % 3],
                     "max_combo": (15, 300, 950)[index % 3],
                     "bpm": 170, "ar": 8.5, "length": 100,
                     "tags": [{"name": "skillset/jumps", "source": "manual"}],
                     "path": f"private/map-{index}.osu"})
    return maps


def all_maps(groups):
    return [beatmap for group in groups for beatmap in group["maps"]]


class RecommendationExpectationIntegrationTests(unittest.TestCase):
    def test_every_recommended_map_has_reviewable_consistent_target(self):
        profile = engine.assess([play(i) for i in range(6)], now=NOW)
        groups = engine.recommend(catalog(profile["baseline"]), profile)
        self.assertEqual([len(group["maps"]) for group in groups], [3, 3, 3])
        grades_seen = set()
        for beatmap in all_maps(groups):
            with self.subTest(map=beatmap["key"]):
                expected = beatmap["expectation"]
                self.assertTrue(expected["complete_required"])
                self.assertEqual(expected["kind"], "training_target")
                self.assertGreaterEqual(expected["accuracy_min"], 0)
                self.assertLessEqual(expected["accuracy_min"], 100)
                self.assertIsInstance(expected["misses_max"], int)
                self.assertGreaterEqual(expected["misses_max"], 0)
                self.assertLessEqual(expected["misses_max"], beatmap["object_count"])
                self.assertLessEqual(expected["combo_min"], beatmap["max_combo"])
                self.assertNotIn("grade", expected["required_keys"])
                self.assertIn("Completar", beatmap["goal"])
                self.assertIn(f"{expected['accuracy_min']:g}", beatmap["goal"])
                self.assertNotIn("path", beatmap)
                self.assertTrue(expected["basis"])
                self.assertTrue(expected["note"])
                grades_seen.add(expected["grade_min"])
                if expected["grade_min"] == "S":
                    self.assertEqual(expected["misses_max"], 0)
                    self.assertGreaterEqual(expected["accuracy_min"], 95)
        self.assertIn("S", grades_seen)
        self.assertIn("A", grades_seen)

    def test_locked_challenge_card_uses_consolidation_targets(self):
        profile = engine.assess([play(0)], now=NOW)
        self.assertFalse(profile["challenge_unlocked"])
        groups = engine.recommend(catalog(profile["baseline"]), profile)
        final_group = groups[-1]
        self.assertEqual(final_group["label"], "Consolidar")
        self.assertEqual(final_group["target"], profile["baseline"])
        self.assertTrue(final_group["maps"])
        self.assertTrue(all(m["expectation"]["stage"] == "consolidate" for m in final_group["maps"]))
        self.assertIn("calibración", final_group["description"])

    def test_unlocked_challenge_uses_actual_challenge_targets(self):
        profile = engine.assess([play(i) for i in range(6)], now=NOW)
        self.assertTrue(profile["challenge_unlocked"])
        groups = engine.recommend(catalog(profile["baseline"]), profile)
        self.assertEqual(groups[-1]["label"], "Consolidar")
        self.assertEqual(1, sum(m["expectation"]["stage"] == "challenge" for m in groups[1]["maps"]))
        self.assertTrue(all(m["expectation"]["stage"] == "consolidate" for m in groups[-1]["maps"]))

    def test_old_scores_cannot_change_expectations_through_assess(self):
        recent = [play(i, accuracy=93) for i in range(6)]
        old = [play(i + 100, stars=8, accuracy=100,
                    played_at=(NOW - timedelta(days=31, minutes=i)).isoformat()) for i in range(20)]
        expected_profile = engine.assess(recent, now=NOW)
        actual_profile = engine.assess(old + recent, now=NOW)
        self.assertEqual(expected_profile, actual_profile)
        candidates = catalog(expected_profile["baseline"])
        self.assertEqual(engine.recommend(candidates, expected_profile),
                         engine.recommend(candidates, actual_profile))

    def test_hundred_play_window_controls_expectation_evidence(self):
        current = [play(i + 10, accuracy=94, played_at=(NOW - timedelta(minutes=100-i)).isoformat()) for i in range(100)]
        superseded = [play(i, accuracy=100, played_at=(NOW - timedelta(minutes=110-i)).isoformat()) for i in range(10)]
        a = engine.assess(current, now=NOW)
        b = engine.assess(superseded + current, now=NOW)
        self.assertEqual(len(b["window"]), 100)
        self.assertEqual(len(b["session_window"]), 20)
        self.assertEqual(a, b)
        candidates = catalog(a["baseline"])
        self.assertEqual(engine.recommend(candidates, a), engine.recommend(candidates, b))

    def test_rank_and_pp_do_not_change_any_recommendation_target(self):
        current = [play(i, accuracy=95) for i in range(6)]
        enhanced = deepcopy(current)
        for item in enhanced:
            item.update(rank=1, global_rank=1, pp=90_000, historical_best_pp=999_999)
        a, b = engine.assess(current, now=NOW), engine.assess(enhanced, now=NOW)
        candidates = catalog(a["baseline"])
        self.assertEqual(engine.recommend(candidates, a), engine.recommend(candidates, b))

    def test_analysis_and_recommendation_do_not_mutate_inputs(self):
        plays = [play(i, tags=[{"name": "skillset/jumps", "source": "manual"}]) for i in range(6)]
        plays_before = deepcopy(plays)
        profile = engine.assess(plays, now=NOW)
        candidates = catalog(profile["baseline"])
        analysis = analyze_tags(profile["window"], candidates, profile["baseline"])
        profile_before, catalog_before, tags_before = deepcopy(profile), deepcopy(candidates), deepcopy(analysis)
        engine.recommend(candidates, profile, tag_analysis=analysis)
        self.assertEqual(plays, plays_before)
        self.assertEqual(profile, profile_before)
        self.assertEqual(candidates, catalog_before)
        self.assertEqual(analysis, tags_before)

    def test_poor_recent_accuracy_remains_reachable_in_recommendation_cards(self):
        profile = engine.assess([play(i, accuracy=82, misses=4) for i in range(2)], now=NOW)
        candidates = catalog(profile["baseline"])
        groups = engine.recommend(candidates, profile)
        principal = next(group for group in groups if group["stage"] == "practice")
        self.assertTrue(principal["maps"])
        for beatmap in principal["maps"]:
            expected = beatmap["expectation"]
            self.assertEqual(expected["grade_min"], "B")
            self.assertLessEqual(expected["accuracy_min"], 85)


if __name__ == "__main__":
    unittest.main()
