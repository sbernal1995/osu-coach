"""Per-map training goals must stay comparable, achievable and transparent."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from osu_coach.core.expectations import expectation_for


NOW = datetime(2026, 9, 8, 20, tzinfo=timezone.utc)


def play(index, key=None, **changes):
    result = {"id": str(index), "beatmap_key": key or str(index),
              "played_at": (NOW + timedelta(minutes=index)).isoformat(),
              "mode": 0, "client": "lazer", "mods": [], "stars": 4.5,
              "accuracy": 97, "misses": 2, "object_count": 400,
              "judged_objects": 400, "map_max_combo": 500, "max_combo": 400,
              "passed": True, "completion": 1}
    result.update(changes)
    return result


def beatmap(**changes):
    result = {"key": "recommended", "stars": 4.5, "mode": 0,
              "object_count": 400, "max_combo": 500, "tags": []}
    result.update(changes)
    return result


def profile(window=(), **changes):
    result = {"window": list(window), "baseline": 4.5, "phase": "training", "challenge_unlocked": True}
    result.update(changes)
    return result


def tag_item(tag="skillset/jumps", **changes):
    result = {"tag": tag, "name": "Saltos", "status": "practice",
              "confidence": "medium", "stats_scope": "comparable",
              "comparable_plays": 8, "comparable_maps": 5, "comparable_sessions": 2}
    result.update(changes)
    return result


class ExpectationTests(unittest.TestCase):
    def test_empty_profile_has_explicit_provisional_training_goal(self):
        result = expectation_for(beatmap(), profile(), "practice")
        self.assertEqual(result["kind"], "training_target")
        self.assertEqual(result["confidence"], "provisional")
        self.assertEqual((result["samples"], result["distinct_maps"]), (0, 0))
        self.assertTrue(result["complete_required"])
        self.assertEqual(result["grade_min"], "A")
        self.assertIn("inicial", result["basis"])
        self.assertIn("orientativo", result["note"])

    def test_different_difficulties_have_different_accuracy_targets(self):
        active = profile([play(i) for i in range(6)])
        easier = expectation_for(beatmap(stars=4.2), active, "practice")
        harder = expectation_for(beatmap(stars=4.8), active, "practice")
        self.assertGreater(easier["accuracy_min"], harder["accuracy_min"])
        self.assertEqual(easier["samples"], harder["samples"])

    def test_miss_budget_scales_with_map_objects(self):
        active = profile([play(i, misses=4) for i in range(6)])
        short = expectation_for(beatmap(object_count=200), active, "practice")
        long = expectation_for(beatmap(object_count=800), active, "practice")
        self.assertLess(short["misses_max"], long["misses_max"])

    def test_short_map_does_not_get_impossible_one_miss_allowance(self):
        result = expectation_for(beatmap(object_count=5, max_combo=5), profile(), "practice")
        self.assertEqual(result["misses_max"], 0)
        self.assertLessEqual(result["combo_min"], 5)
        self.assertGreaterEqual(100 * (1 - result["misses_max"] / 5), result["accuracy_min"])

    def test_missing_map_counts_do_not_invent_counts_or_s(self):
        result = expectation_for(beatmap(object_count=None, max_combo=None), profile(), "practice")
        self.assertIsNone(result["misses_max"])
        self.assertIsNone(result["combo_min"])
        self.assertNotEqual(result["grade_min"], "S")

    def test_partial_perfect_scores_cannot_raise_accuracy(self):
        partials = [play(i, accuracy=100, passed=False, completion=.1) for i in range(6)]
        result = expectation_for(beatmap(), profile(partials), "practice")
        initial = expectation_for(beatmap(), profile(), "practice")
        self.assertEqual(result["samples"], 0)
        self.assertLessEqual(result["accuracy_min"], initial["accuracy_min"])
        self.assertEqual(result["confidence"], "provisional")
        self.assertIn("priorizá completar", result["note"])

    def test_single_lucky_score_has_limited_effect(self):
        initial = expectation_for(beatmap(), profile(), "practice")
        lucky = expectation_for(beatmap(), profile([play(1, accuracy=100, misses=0)]), "practice")
        self.assertLessEqual(lucky["accuracy_min"] - initial["accuracy_min"], 1)
        self.assertEqual(lucky["confidence"], "provisional")

    def test_returning_player_with_low_accuracy_gets_reachable_b_or_c(self):
        for accuracy, expected_grade in ((80, "B"), (85, "B"), (72, "C")):
            with self.subTest(accuracy=accuracy):
                active = profile([play(i, accuracy=accuracy) for i in range(6)])
                result = expectation_for(beatmap(), active, "practice")
                self.assertLessEqual(result["accuracy_min"], accuracy + 3)
                self.assertGreaterEqual(result["accuracy_min"], accuracy)
                self.assertEqual(result["grade_min"], expected_grade)

    def test_one_completed_play_uses_singular_evidence_label(self):
        result = expectation_for(beatmap(), profile([play(0)]), "practice")
        self.assertIn("1 partida completa de 1 mapa cercano", result["basis"])

    def test_numeric_string_accuracy_is_accepted(self):
        result = expectation_for(beatmap(), profile([play(0, accuracy="82.5")]), "practice")
        self.assertLessEqual(result["accuracy_min"], 85.5)

    def test_outlier_and_repeat_do_not_dominate_independent_maps(self):
        normal = [play(i, accuracy=95) for i in range(4)]
        expected = expectation_for(beatmap(), profile(normal), "practice")
        repeated = normal + [play(i, key="lucky", accuracy=100) for i in range(4, 14)]
        result = expectation_for(beatmap(), profile(repeated), "practice")
        self.assertLessEqual(abs(result["accuracy_min"] - expected["accuracy_min"]), .5)
        self.assertEqual(result["samples"], 6)
        self.assertEqual(result["distinct_maps"], 5)

    def test_two_latest_attempts_replace_older_success(self):
        old = play(0, key="same", accuracy=100)
        recent = [play(i, key="same", accuracy=90) for i in (1, 2)]
        a = expectation_for(beatmap(), profile([old] + recent), "practice")
        b = expectation_for(beatmap(), profile(recent), "practice")
        self.assertEqual(a, b)

    def test_duplicate_ids_and_invalid_unconfirmed_results_are_ignored(self):
        valid = play(0)
        noisy = [valid, deepcopy(valid), play(1, accuracy=float("nan")),
                 play(2, excluded=True), play(3, needs_confirmation=True),
                 play(4, mode=3)]
        result = expectation_for(beatmap(), profile(noisy), "practice")
        self.assertEqual((result["samples"], result["distinct_maps"]), (1, 1))

    def test_outside_star_band_does_not_set_goal(self):
        active = profile([play(i, accuracy=100, stars=2.5) for i in range(6)])
        result = expectation_for(beatmap(), active, "practice")
        self.assertEqual(result["samples"], 0)
        self.assertEqual(result["accuracy_min"], 96)

    def test_confidence_requires_independent_completed_evidence(self):
        result = expectation_for(beatmap(), profile([play(i) for i in range(6)]), "practice")
        self.assertEqual(result["confidence"], "provisional")  # OD is missing in this fixture.
        result = expectation_for(beatmap(), profile([play(i, key="one") for i in range(6)]), "practice")
        self.assertEqual(result["confidence"], "provisional")

    def test_rank_pp_and_historical_results_have_no_effect(self):
        plays = [play(i) for i in range(6)]
        changed = deepcopy(plays)
        for item in changed:
            item.update(rank=1, pp=10_000, historical_best_accuracy=100)
        a = expectation_for(beatmap(), profile(plays), "practice")
        b = expectation_for(beatmap(), profile(changed), "practice")
        self.assertEqual(a, b)

    def test_stages_control_required_precision_and_locked_challenge_is_consolidation(self):
        active = profile([play(i) for i in range(6)])
        warm = expectation_for(beatmap(), active, "warmup")
        practice = expectation_for(beatmap(), active, "practice")
        challenge = expectation_for(beatmap(), active, "challenge")
        self.assertGreater(warm["accuracy_min"], practice["accuracy_min"])
        self.assertGreater(practice["accuracy_min"], challenge["accuracy_min"])
        active["challenge_unlocked"] = False
        self.assertEqual(expectation_for(beatmap(), active, "challenge"),
                         expectation_for(beatmap(), active, "consolidate"))

    def test_weak_tag_evidence_is_neutral(self):
        candidate = beatmap(tags=["skillset/jumps"])
        active = profile([play(i) for i in range(6)])
        initial = expectation_for(candidate, active, "practice")
        analysis = {"items": [tag_item(confidence="low")]}
        result = expectation_for(candidate, active, "practice", analysis)
        self.assertEqual(result, initial)

    def test_tag_cannot_adjust_goal_without_map_and_session_diversity(self):
        candidate = beatmap(tags=["skillset/jumps"])
        active = profile([play(i) for i in range(8)])
        initial = expectation_for(candidate, active, "practice")
        for changes in ({"comparable_plays": 7}, {"comparable_maps": 4}, {"comparable_sessions": 1}):
            with self.subTest(changes=changes):
                result = expectation_for(candidate, active, "practice", {"items": [tag_item(**changes)]})
                self.assertEqual(result, initial)

    def test_practice_tag_relaxes_goal_without_stacking_cooccurring_tags(self):
        candidate = beatmap(tags=["skillset/jumps", "tech/aim control"])
        active = profile([play(i) for i in range(6)])
        initial = expectation_for(candidate, active, "practice")
        single = expectation_for(candidate, active, "practice", {"items": [tag_item()]})
        multiple = expectation_for(candidate, active, "practice", {
            "items": [tag_item(), tag_item("tech/aim control", name="Control del apuntado")]})
        self.assertEqual(initial["accuracy_min"] - single["accuracy_min"], .5)
        self.assertEqual(single["accuracy_min"], multiple["accuracy_min"])

    def test_tag_outside_its_analysis_band_is_neutral(self):
        candidate = beatmap(tags=["skillset/jumps"])
        active = profile([play(i) for i in range(6)])
        initial = expectation_for(candidate, active, "practice")
        analysis = {"items": [tag_item()], "band": {"min": 5, "max": 6}}
        result = expectation_for(candidate, active, "practice", analysis)
        self.assertEqual(initial, result)

    def test_lazer_s_never_allows_misses(self):
        for count in (10, 200, 800):
            for stage in ("warmup", "practice", "consolidate", "challenge"):
                result = expectation_for(beatmap(object_count=count), profile(), stage)
                if result["grade_min"] == "S":
                    self.assertEqual(result["misses_max"], 0)

    def test_fresh_client_and_mods_are_forwarded_to_grade_rules(self):
        from unittest.mock import patch
        active = profile([play(1, client="stable", mods=[{"acronym": "HD"}])])
        with patch("osu_coach.core.expectations.target_grade", return_value={"grade": "A", "label": "A o mejor"}) as grade:
            result = expectation_for(beatmap(), active, "practice")
        grade.assert_called_once_with("stable", result["accuracy_min"], result["misses_max"], [{"acronym": "HD"}])


if __name__ == "__main__":
    unittest.main()
