"""Independent boundaries for earned-rank evidence, without persistence."""
import copy
import unittest

from osu_coach.core.progress_rules import rank_evidence


def play(index, **changes):
    result = {
        "id": f"attempt-{index}", "beatmap_id": index + 100,
        "beatmap_key": f"hash-{index}", "title": f"Song {index}", "version": "Insane",
        "stars": 4.75, "accuracy": 98, "misses": 1, "judged_objects": 400,
        "max_combo": 450, "map_max_combo": 500, "passed": True,
        "completion": 1, "mode": 0, "played_at": "2026-09-08T12:00:00+00:00",
    }
    result.update(changes)
    return result


def profile(plays=None, **changes):
    result = {"phase": "training", "baseline": 4.66,
              "window": [play(index) for index in range(3)] if plays is None else plays}
    result.update(changes)
    return result


class RankEvidenceTests(unittest.TestCase):
    def test_candidate_is_highest_quarter_supported_by_three_maps(self):
        result = rank_evidence(profile([play(0, stars=4.75), play(1, stars=4.99), play(2, stars=5.25)]))
        self.assertEqual(result["candidate_stars"], 4.75)
        self.assertEqual(result["next_stars"], 4.5)
        self.assertEqual(result["completed_maps"], 2)
        self.assertEqual(result["required_maps"], 3)
        self.assertTrue(result["calibrated"])
        self.assertEqual((result["window_plays"], result["window_days"]), (100, 30))

    def test_next_rank_uses_earned_rank_not_fluctuating_baseline(self):
        for baseline in (1, 4.66, 9):
            with self.subTest(baseline=baseline):
                result = rank_evidence(profile(baseline=baseline), earned_stars=4.5)
                self.assertEqual(result["next_stars"], 4.75)
                self.assertEqual(result["completed_maps"], 3)

    def test_cap_has_no_next_rank_or_fabricated_evidence(self):
        result = rank_evidence(profile([play(i, stars=10.5) for i in range(3)]), earned_stars=10.5)
        self.assertEqual(result["candidate_stars"], 10.5)
        self.assertIsNone(result["next_stars"])
        self.assertEqual(result["qualifying_maps"], [])
        self.assertEqual(result["completed_maps"], 0)

    def test_calibration_never_grants_rank_despite_strong_results(self):
        for phase in (None, "calibrating", "unknown"):
            with self.subTest(phase=phase):
                result = rank_evidence(profile(phase=phase))
                self.assertFalse(result["calibrated"])
                self.assertIsNone(result["candidate_stars"])
                self.assertEqual(result["completed_maps"], 0)

    def test_unknown_level_and_empty_window_do_not_invent_evidence(self):
        for baseline in (None, "unknown", float("nan"), float("inf"), True):
            with self.subTest(baseline=baseline):
                result = rank_evidence(profile([], baseline=baseline))
                self.assertEqual(result["next_stars"], .5)
                self.assertIsNone(result["candidate_stars"])
                self.assertEqual(result["completed_maps"], 0)
        self.assertFalse(rank_evidence(None)["calibrated"])

    def test_initial_goal_is_floored_and_clamped(self):
        for baseline, expected in ((4.66, 4.5), (4.75, 4.75), (.49, .5), (11, 10.5), (-1, .5)):
            with self.subTest(baseline=baseline):
                self.assertEqual(rank_evidence(profile([], baseline=baseline))["next_stars"], expected)

    def test_exact_quality_boundaries_count(self):
        rows = [play(i, accuracy=97, misses=1, judged_objects=200,
                     max_combo=400, map_max_combo=500, completion=.98) for i in range(3)]
        result = rank_evidence(profile(rows), earned_stars=4.5)
        self.assertEqual(result["candidate_stars"], 4.75)
        self.assertEqual(result["completed_maps"], 3)
        self.assertEqual(result["qualifying_maps"][0]["combo_ratio"], .8)

    def test_values_just_below_each_threshold_do_not_count(self):
        for change in ({"accuracy": 96.999999}, {"judged_objects": 199},
                       {"max_combo": 399}, {"completion": .979999}):
            with self.subTest(change=change):
                row = play(2, accuracy=97, misses=1, judged_objects=200,
                           max_combo=400, map_max_combo=500, completion=.98)
                row.update(change)
                result = rank_evidence(profile([play(0), play(1), row]))
                self.assertIsNone(result["candidate_stars"])
                self.assertEqual(result["completed_maps"], 2)

    def test_rounded_accuracy_cannot_prove_exact_threshold(self):
        rows = [play(0), play(1), play(2, accuracy=97, accuracy_rounded=True)]
        result = rank_evidence(profile(rows))
        self.assertIsNone(result["candidate_stars"])
        self.assertEqual(result["completed_maps"], 2)

    def test_missing_required_measurement_is_not_zero_or_success(self):
        for field in ("completion", "accuracy", "misses", "judged_objects", "max_combo", "map_max_combo", "stars", "passed"):
            with self.subTest(field=field):
                row = play(2)
                del row[field]
                result = rank_evidence(profile([play(0), play(1), row]))
                self.assertIsNone(result["candidate_stars"])
                self.assertEqual(result["completed_maps"], 2)

    def test_invalid_measurements_cannot_qualify(self):
        for field in ("completion", "accuracy", "misses", "judged_objects", "max_combo", "map_max_combo", "stars"):
            for value in (True, float("nan"), float("inf"), "bad", -1):
                with self.subTest(field=field, value=value):
                    self.assertEqual(rank_evidence(profile([play(0, **{field: value})]))["completed_maps"], 0)

    def test_impossible_counts_and_ranges_are_rejected(self):
        for change in ({"judged_objects": 0}, {"map_max_combo": 0}, {"accuracy": 100.01},
                       {"completion": 1.01}, {"max_combo": 501}, {"misses": .5},
                       {"judged_objects": 400.5}, {"max_combo": 450.5}, {"map_max_combo": 500.5}):
            with self.subTest(change=change):
                self.assertEqual(rank_evidence(profile([play(0, **change)]))["completed_maps"], 0)

    def test_passed_requires_actual_boolean_true(self):
        for passed in (False, None, 1, "true"):
            with self.subTest(passed=passed):
                self.assertEqual(rank_evidence(profile([play(0, passed=passed)]))["completed_maps"], 0)

    def test_excluded_pending_and_other_modes_are_not_evidence(self):
        for change in ({"excluded": True}, {"needs_confirmation": True}, {"mode": 1}, {"mode": False}):
            with self.subTest(change=change):
                self.assertEqual(rank_evidence(profile([play(0, **change)]))["completed_maps"], 0)

    def test_same_map_id_with_three_hash_revisions_counts_once(self):
        rows = [play(i, beatmap_id=1234) for i in range(3)]
        result = rank_evidence(profile(rows))
        self.assertIsNone(result["candidate_stars"])
        self.assertEqual(result["completed_maps"], 1)

    def test_different_difficulty_ids_in_same_set_are_distinct(self):
        rows = [play(i, set_id=444, title="Same song", version=f"Difficulty {i}") for i in range(3)]
        self.assertEqual(rank_evidence(profile(rows))["candidate_stars"], 4.75)

    def test_hash_fallback_and_missing_identity(self):
        rows = [play(i, beatmap_id=0) for i in range(3)]
        self.assertEqual(rank_evidence(profile(rows))["completed_maps"], 3)
        rows = [play(i, beatmap_id=None, beatmap_key="SameHash") for i in range(3)]
        self.assertEqual(rank_evidence(profile(rows))["completed_maps"], 1)
        self.assertEqual(rank_evidence(profile([play(0, beatmap_id=None, beatmap_key=None)]))["completed_maps"], 0)

    def test_missing_key_can_use_observed_positive_map_id(self):
        result = rank_evidence(profile([play(0, beatmap_key=None)]))
        self.assertEqual(result["qualifying_maps"][0]["key"], "osu:100")

    def test_repetition_cannot_inflate_three_distinct_maps_requirement(self):
        rows = [play(0, id=f"replay-{i}") for i in range(20)]
        result = rank_evidence(profile(rows))
        self.assertEqual(result["completed_maps"], 1)
        self.assertIsNone(result["candidate_stars"])

    def test_metrics_never_combine_from_different_attempts(self):
        rows = [play(i, accuracy=99, max_combo=300) for i in range(3)]
        rows += [play(i, id=f"retry-{i}", accuracy=94, max_combo=500) for i in range(3)]
        result = rank_evidence(profile(rows))
        self.assertEqual(result["completed_maps"], 0)
        self.assertIsNone(result["candidate_stars"])

    def test_best_qualifying_attempt_is_kept_as_a_whole(self):
        rows = [play(0, accuracy=98.5, misses=2, max_combo=425),
                play(0, id="retry", accuracy=98, misses=0, max_combo=500),
                play(0, id="failure", accuracy=100, misses=0, max_combo=500, passed=False)]
        actual = rank_evidence(profile(rows))["qualifying_maps"][0]
        self.assertEqual((actual["accuracy"], actual["misses"], actual["combo_ratio"]), (98.5, 2, .85))

    def test_stars_are_rounded_to_two_decimals_before_inclusive_band(self):
        rows = [play(0, stars=4.746), play(1, stars=5.254), play(2, stars=5.0)]
        result = rank_evidence(profile(rows), earned_stars=4.5)
        self.assertEqual(result["candidate_stars"], 4.75)
        self.assertEqual(result["completed_maps"], 3)
        self.assertEqual({m["stars"] for m in result["qualifying_maps"]}, {4.75, 5, 5.25})
        rows[1]["stars"] = 5.255
        self.assertEqual(rank_evidence(profile(rows), earned_stars=4.5)["completed_maps"], 2)

    def test_three_maps_far_apart_do_not_form_a_rank_band(self):
        result = rank_evidence(profile([play(0, stars=2), play(1, stars=4.75), play(2, stars=7)]))
        self.assertIsNone(result["candidate_stars"])
        self.assertEqual(result["completed_maps"], 1)

    def test_candidate_and_current_next_evidence_are_separate(self):
        result = rank_evidence(profile(), earned_stars=5)
        self.assertEqual(result["candidate_stars"], 4.75)
        self.assertEqual(result["next_stars"], 5.25)
        self.assertEqual(result["completed_maps"], 0)

    def test_only_three_supporting_maps_are_exposed(self):
        result = rank_evidence(profile([play(i) for i in range(8)]))
        self.assertEqual(result["completed_maps"], 3)
        self.assertEqual(len(result["qualifying_maps"]), 3)

    def test_minimum_rank_and_upper_bound(self):
        for stars, expected in ((.49, None), (.5, .5), (10.5, 10.5), (11, 10.5), (11.01, None)):
            with self.subTest(stars=stars):
                self.assertEqual(rank_evidence(profile([play(i, stars=stars) for i in range(3)]))["candidate_stars"], expected)

    def test_history_pp_and_global_rank_are_ignored(self):
        source = profile([])
        source.update(pp=90000, rank=1, best_scores=[play(i) for i in range(3)], earned_stars=10.5)
        result = rank_evidence(source)
        self.assertEqual(result["completed_maps"], 0)
        self.assertIsNone(result["candidate_stars"])
        self.assertEqual(result["next_stars"], 4.5)

    def test_input_and_returned_metadata_are_independent(self):
        source = profile()
        before = copy.deepcopy(source)
        result = rank_evidence(source)
        result["qualifying_maps"][0]["accuracy"] = 0
        result["requirements"].clear()
        self.assertEqual(source, before)
        self.assertTrue(rank_evidence(source)["requirements"])


if __name__ == "__main__":
    unittest.main()
