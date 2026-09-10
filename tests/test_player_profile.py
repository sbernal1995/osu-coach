"""Recent player observations, evidence boundaries and conservative progression."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from osu_coach.core.player_profile import build_player_profile


NOW = datetime(2026, 9, 8, 20, tzinfo=timezone.utc)


def play(index, key=None, **changes):
    result = {"id": f"play-{index}", "beatmap_key": key or f"map-{index}",
              "played_at": (NOW + timedelta(minutes=index, hours=2 if index >= 4 else 0)).isoformat(),
              "mode": 0, "client": "lazer", "mods": [], "stars": 4.5,
              "accuracy": 98, "misses": 1, "judged_objects": 400, "object_count": 400,
              "max_combo": 450, "map_max_combo": 500, "passed": True, "completion": 1}
    result.update(changes)
    return result


def profile(plays=(), **changes):
    plays = list(plays)
    result = {"window": plays, "session_window": plays[-20:], "evaluated_at": (NOW + timedelta(days=1)).isoformat(),
              "baseline": 4.5, "challenge_unlocked": True, "phase": "training"}
    result.update(changes)
    return result


def dimension(result, key):
    return next(item for item in result["dimensions"] if item["key"] == key)


def tag_analysis(status="practice", **changes):
    item = {"tag": "skillset/streams", "name": "Streams", "status": status, "confidence": "medium",
            "comparable_plays": 8, "comparable_maps": 5, "comparable_sessions": 2, "stats_scope": "comparable", "plays": 8,
            "accuracy": 95, "cooccurs": True}
    item.update(changes)
    return {"items": [item]}


class PlayerProfileTests(unittest.TestCase):
    def test_empty_profile_has_unknown_metrics_and_preserves_existing_challenge(self):
        result = build_player_profile(profile())
        self.assertEqual(result["status"], "learning")
        self.assertEqual(result["progression"]["mode"], "calibrate")
        self.assertTrue(result["progression"]["allow_challenge"])
        self.assertEqual(result["strengths"], [])
        self.assertEqual(result["weaknesses"], [])
        self.assertTrue(all(item["value"] is None for item in result["dimensions"]))
        self.assertTrue(result["unknowns"])

    def test_eight_data_points_five_distinct_maps_and_two_sessions_are_required(self):
        for plays in ([play(i) for i in range(7)], [play(i, key=f"repeat-{i % 4}") for i in range(12)],
                      [play(i, played_at=(NOW + timedelta(minutes=i)).isoformat()) for i in range(8)]):
            result = build_player_profile(profile(plays))
            self.assertEqual(result["status"], "learning")
            self.assertEqual(result["strengths"], [])
            self.assertEqual(result["weaknesses"], [])
        result = build_player_profile(profile([play(i, key=f"map-{i % 5}") for i in range(8)]))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(dimension(result, "accuracy")["status"], "strength")

    def test_latest_two_attempts_per_map_get_equal_map_weight(self):
        plays = [play(0, key="a", accuracy=40), play(1, key="a", accuracy=80), play(2, key="a", accuracy=90),
                 play(3, key="b", accuracy=100), play(4, key="c", accuracy=100), play(5, key="d", accuracy=100)]
        for item in plays:
            item["played_at"] = NOW.isoformat()
        plays[0]["played_at"] = (NOW - timedelta(days=1)).isoformat()
        result = build_player_profile(profile(plays))
        self.assertEqual(result["evidence"]["comparable_plays"], 5)
        self.assertEqual(dimension(result, "accuracy")["value"], 96.25)

    def test_numeric_data_missing_is_unknown_and_not_zero(self):
        plays = [play(i, accuracy=None, misses=None, max_combo=None, map_max_combo=None) for i in range(8)]
        result = build_player_profile(profile(plays))
        for key in ("accuracy", "misses", "combo", "consistency"):
            item = dimension(result, key)
            self.assertIsNone(item["value"])
            self.assertEqual(item["status"], "learning")
            self.assertEqual(item["samples"], 0)
        self.assertEqual(dimension(result, "completion")["value"], 100)

    def test_nan_infinite_negative_and_boolean_measurements_stay_unknown(self):
        plays = [play(i, accuracy=float("nan"), misses=-1, max_combo=True, map_max_combo=float("inf")) for i in range(8)]
        result = build_player_profile(profile(plays))
        for key in ("accuracy", "misses", "combo"):
            self.assertIsNone(dimension(result, key)["value"])

    def test_partial_perfect_accuracy_never_becomes_a_precision_strength(self):
        plays = [play(i, accuracy=100, passed=False, completion=.1, max_combo=500) for i in range(8)]
        result = build_player_profile(profile(plays))
        self.assertIsNone(dimension(result, "accuracy")["value"])
        self.assertIsNone(dimension(result, "combo")["value"])
        self.assertEqual(dimension(result, "completion")["status"], "practice")
        self.assertEqual(result["progression"]["mode"], "recover")
        self.assertFalse(result["progression"]["allow_challenge"])

    def test_each_metric_requires_its_own_complete_evidence(self):
        plays = [play(i) for i in range(7)] + [play(7, passed=False, completion=.5)]
        result = build_player_profile(profile(plays))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(dimension(result, "accuracy")["status"], "learning")
        self.assertEqual(dimension(result, "misses")["confidence"], "medium")

    def test_early_failure_miss_rate_uses_judged_objects_and_not_total_map(self):
        plays = [play(i, passed=False, completion=.1, misses=5, judged_objects=50, object_count=500) for i in range(5)]
        result = build_player_profile(profile(plays))
        self.assertEqual(dimension(result, "misses")["value"], 10)
        for item in plays:
            item["judged_objects"] = None
        result = build_player_profile(profile(plays))
        self.assertIsNone(dimension(result, "misses")["value"])

    def test_complete_play_misses_can_use_full_object_count(self):
        result = build_player_profile(profile([play(i, judged_objects=None) for i in range(5)]))
        self.assertEqual(dimension(result, "misses")["value"], .25)

    def test_old_success_does_not_replace_missing_latest_measurements(self):
        plays = [play(0, key="repeat"), play(1, key="repeat", accuracy=None), play(2, key="repeat", accuracy=None)]
        result = build_player_profile(profile(plays))
        self.assertEqual(dimension(result, "accuracy")["samples"], 0)

    def test_outside_band_duplicate_and_unconfirmed_plays_do_not_create_findings(self):
        valid = play(0)
        plays = [valid, deepcopy(valid), play(1, stars=5.01), play(2, stars=3.99),
                 play(3, needs_confirmation=True), play(4, excluded=True), play(5, mode=3), play(6, played_at="invalid")]
        result = build_player_profile(profile(plays))
        self.assertEqual(result["evidence"]["comparable_plays"], 1)
        self.assertEqual(result["strengths"], [])

    def test_finite_comparable_star_band_boundary_is_inclusive(self):
        plays = [play(0, stars=4), play(1, stars=5)] + [play(i) for i in range(2, 8)]
        self.assertEqual(build_player_profile(profile(plays))["status"], "ready")

    def test_consistency_is_population_deviation_of_map_mean_accuracies(self):
        plays = [play(0, key="a", accuracy=94), play(1, key="a", accuracy=98),
                 play(2, key="b", accuracy=98), play(3, key="b", accuracy=98), play(4, key="c", accuracy=100)]
        result = build_player_profile(profile(plays))
        item = dimension(result, "consistency")
        self.assertEqual(item["value"], 1.63)
        self.assertEqual(item["unit"], "pp")
        self.assertIn("desviación estándar", item["evidence"])

    def test_stable_low_accuracy_does_not_create_a_consistency_strength(self):
        result = build_player_profile(profile([play(i, accuracy=80) for i in range(8)]))
        self.assertEqual(dimension(result, "consistency")["value"], 0)
        self.assertEqual(dimension(result, "consistency")["status"], "steady")
        self.assertEqual(dimension(result, "accuracy")["status"], "practice")

    def test_observed_strengths_advance_without_manufacturing_skill_tags(self):
        result = build_player_profile(profile([play(i) for i in range(8)]))
        self.assertEqual(result["progression"]["mode"], "advance")
        self.assertTrue(result["progression"]["allow_challenge"])
        self.assertEqual(result["weaknesses"], [])
        self.assertTrue(all(item["key"] in {"accuracy", "misses", "combo", "completion", "consistency"} for item in result["strengths"]))
        self.assertEqual(result["priorities"][0]["key"], result["progression"]["focus_key"])

    def test_unknown_combo_is_neutral_for_advance_with_other_strong_measurements(self):
        plays = [play(i, max_combo=None, map_max_combo=None) for i in range(8)]
        result = build_player_profile(profile(plays))
        self.assertEqual(result["progression"]["mode"], "advance")
        self.assertEqual(dimension(result, "combo")["status"], "learning")

    def test_steady_combo_is_an_area_to_consolidate_and_never_a_weakness(self):
        plays = [play(i, accuracy=96.64, misses=2, judged_objects=360, max_combo=390) for i in range(8)]
        result = build_player_profile(profile(plays))
        self.assertEqual(dimension(result, "combo")["value"], 78)
        self.assertEqual(dimension(result, "combo")["status"], "steady")
        self.assertFalse(any(item["key"] == "combo" for item in result["weaknesses"]))
        self.assertEqual(result["priorities"][0]["key"], "combo")
        self.assertEqual(result["progression"]["mode"], "consolidate")
        self.assertTrue(result["progression"]["allow_challenge"])
        self.assertIn("0,10", " ".join(result["progression"]["reasons"]))

    def test_weak_combo_consolidates_but_preserves_existing_challenge(self):
        result = build_player_profile(profile([play(i, max_combo=300) for i in range(8)]))
        self.assertEqual(dimension(result, "combo")["status"], "practice")
        self.assertEqual(result["progression"]["mode"], "consolidate")
        self.assertTrue(result["progression"]["allow_challenge"])

    def test_historical_completion_weakness_does_not_trigger_session_recovery(self):
        plays = [play(i, passed=i > 1, completion=1 if i > 1 else .5) for i in range(8)]
        result = build_player_profile(profile(plays))
        self.assertLess(dimension(result, "completion")["value"], 80)
        self.assertEqual(dimension(result, "completion")["status"], "practice")
        self.assertEqual(result["progression"]["mode"], "consolidate")

    def test_two_recent_failures_can_pause_challenge_before_profile_is_ready(self):
        result = build_player_profile(profile([play(i, passed=False, completion=.2) for i in range(2)]))
        self.assertEqual(result["status"], "learning")
        self.assertEqual(result["weaknesses"], [])
        self.assertEqual(result["progression"]["mode"], "recover")
        self.assertFalse(result["progression"]["allow_challenge"])
        self.assertEqual(result["priorities"][0]["key"], "completion")

    def test_repeated_map_cap_does_not_make_an_old_failure_seem_recent(self):
        plays = [play(0, key="old", passed=False, completion=.2),
                 play(1, key="repeated"), play(2, key="repeated"),
                 play(3, key="repeated", passed=False, completion=.2)]
        result = build_player_profile(profile(plays))
        self.assertEqual(result["progression"]["mode"], "calibrate")
        self.assertTrue(result["progression"]["allow_challenge"])

    def test_profile_never_unlocks_previously_locked_challenge(self):
        result = build_player_profile(profile([play(i) for i in range(8)], challenge_unlocked=False))
        self.assertFalse(result["progression"]["allow_challenge"])

    def test_supported_tag_evidence_becomes_association_priority(self):
        active = profile([play(i) for i in range(8)])
        result = build_player_profile(active, tag_analysis())
        finding = next(item for item in result["weaknesses"] if "tag" in item)
        self.assertEqual(finding["tag"], "skillset/streams")
        self.assertEqual(finding["label"], "Mapas con streams")
        self.assertIn("otras etiquetas", finding["evidence"])
        self.assertEqual(result["priorities"][0]["tag"], "skillset/streams")
        self.assertTrue(result["progression"]["allow_challenge"])

    def test_weak_outside_band_or_unrecognized_tags_do_not_create_findings(self):
        for changes in ({"confidence": "low"}, {"comparable_maps": 4}, {"comparable_plays": 7}, {"comparable_sessions": 1},
                        {"stats_scope": "outside_band"}, {"tag": "anime"}):
            result = build_player_profile(profile([play(i) for i in range(8)]), tag_analysis(**changes))
            self.assertFalse(any("tag" in item for item in result["weaknesses"] + result["strengths"]))

    def test_intermediate_tags_with_sufficient_evidence_are_not_described_as_unknown(self):
        result = build_player_profile(profile([play(i) for i in range(8)]), tag_analysis(status="learning"))
        self.assertFalse(any("Streams" in item or "tipos de patrones" in item for item in result["unknowns"]))
        self.assertFalse(any("tag" in item for item in result["weaknesses"] + result["strengths"]))

    def test_tag_strengths_are_kept_separate_from_general_accuracy_skill_inference(self):
        result = build_player_profile(profile([play(i) for i in range(8)]), tag_analysis(status="strength"))
        self.assertTrue(any(item.get("tag") == "skillset/streams" for item in result["strengths"]))
        self.assertFalse(any(item["key"] in {"aim", "stamina"} for item in result["dimensions"]))

    def test_inputs_rank_and_pp_have_no_effect_and_are_not_mutated(self):
        active, tags = profile([play(i) for i in range(8)]), tag_analysis()
        before_active, before_tags = deepcopy(active), deepcopy(tags)
        initial = build_player_profile(active, tags)
        self.assertEqual(active, before_active)
        self.assertEqual(tags, before_tags)
        enhanced = deepcopy(active)
        enhanced.update(rank=1, pp=99_999)
        for item in enhanced["window"]:
            item.update(rank=1, pp=99_999, historical_best_pp=999_999)
        self.assertEqual(initial, build_player_profile(enhanced, tags))

    def test_recent_maps_outweigh_older_maps_without_repetition_bonus(self):
        rows = [play(i, accuracy=80, played_at=(NOW - timedelta(days=20)).isoformat()) for i in range(5)]
        rows += [play(i, accuracy=100, played_at=NOW.isoformat()) for i in range(5, 10)]
        result = build_player_profile(profile(rows, evaluated_at=NOW.isoformat()))
        self.assertEqual(96, dimension(result, "accuracy")["value"])
        self.assertEqual(2, dimension(result, "accuracy")["sessions"])
        self.assertEqual((100, 30), (result["evidence"]["max_plays"], result["evidence"]["days"]))

    def test_session_identity_is_fixed_before_missing_metric_filter(self):
        rows = [play(i, played_at=(NOW + timedelta(minutes=minute)).isoformat())
                for i, minute in enumerate((0, 10, 20, 30, 100, 110, 120, 130))]
        rows.append(play(8, accuracy=None, played_at=(NOW + timedelta(minutes=60)).isoformat()))
        result = build_player_profile(profile(rows))
        self.assertEqual(1, dimension(result, "accuracy")["sessions"])
        self.assertEqual("learning", dimension(result, "accuracy")["status"])

    def test_empty_session_window_does_not_treat_historical_failures_as_current_recovery(self):
        rows = [play(i, passed=False, completion=.5,
                     played_at=(NOW - timedelta(days=10 if i < 4 else 8)).isoformat()) for i in range(8)]
        result = build_player_profile(profile(rows, session_window=[]))
        self.assertEqual("practice", dimension(result, "completion")["status"])
        self.assertNotEqual("recover", result["progression"]["mode"])


if __name__ == "__main__":
    unittest.main()
