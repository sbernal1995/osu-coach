"""One-attempt mission checks, map identity, dates and missing telemetry."""

import copy
from datetime import datetime, timedelta, timezone
import unittest

from osu_coach.core.quest_rules import evaluate_attempt
from osu_coach.core.mod_policy import context as mod_context


NOW = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


def iso(offset=0):
    return (NOW + timedelta(seconds=offset)).isoformat()


def quest(**changes):
    result = {"id": "quest-1", "created_at": iso(-120),
              "map": {"id": 123, "key": "a" * 32, "mode": 0,
                      "expectation": {"grade_min": "S", "accuracy_min": 97,
                                      "misses_max": 0, "combo_min": 400}}}
    result.update(changes)
    return result


def play(**changes):
    result = {"id": "attempt-1", "beatmap_id": 123, "beatmap_key": "a" * 32,
              "mode": 0, "client": "lazer", "played_at": iso(), "started_at": iso(-60),
              "passed": True, "completion": 1, "accuracy": 97, "misses": 0,
              "max_combo": 400, "map_max_combo": 500, "grade": "S"}
    result.update(changes)
    return result


def evaluate(task=None, attempt=None):
    return evaluate_attempt(quest() if task is None else task, play() if attempt is None else attempt, now=NOW)


def checks(result):
    return {check["key"]: check for check in result["checks"]}


class QuestIdentityTests(unittest.TestCase):
    def test_all_goals_met_in_same_attempt(self):
        result = evaluate()
        self.assertTrue(result["completed"])
        self.assertEqual(result["play_id"], "attempt-1")
        self.assertEqual([check["key"] for check in result["checks"]], ["complete", "grade", "accuracy", "misses", "combo"])
        self.assertTrue(all(check["status"] == "met" for check in result["checks"]))

    def test_different_hash_rejected_even_when_numeric_id_matches(self):
        self.assertIsNone(evaluate(attempt=play(beatmap_key="b" * 32)))

    def test_remote_id_and_hash_match_through_positive_id(self):
        task = quest()
        task["map"]["key"] = "remote:123"
        self.assertTrue(evaluate(task)["completed"])
        task["map"]["checksum"] = "b" * 32
        self.assertIsNone(evaluate(task))

    def test_exact_key_works_without_online_ids(self):
        task = quest()
        task["map"]["id"] = 0
        self.assertTrue(evaluate(task, play(beatmap_id=0))["completed"])

    def test_remote_key_can_supply_positive_map_identity(self):
        task = quest()
        task["map"].update(key="remote:123", id=0)
        self.assertTrue(evaluate(task)["completed"])

    def test_two_unknown_or_different_maps_are_not_equal(self):
        task = quest()
        task["map"].update(key="", id=0)
        self.assertIsNone(evaluate(task, play(beatmap_key="", beatmap_id=0)))
        self.assertIsNone(evaluate(attempt=play(beatmap_key="osu:456", beatmap_id=456)))

    def test_attempt_id_is_not_used_as_map_id(self):
        task = quest()
        task["map"]["key"] = "remote:123"
        self.assertIsNone(evaluate(task, play(id="123", beatmap_id=0, beatmap_key="")))

    def test_hash_case_and_opaque_exact_keys(self):
        self.assertTrue(evaluate(attempt=play(beatmap_key="A" * 32))["completed"])
        task = quest()
        task["map"].update(key="fixture-map", id=0)
        self.assertTrue(evaluate(task, play(beatmap_key="fixture-map", beatmap_id=0))["completed"])

    def test_pending_excluded_wrong_mode_and_missing_identity_are_rejected(self):
        for changes in ({"needs_confirmation": True}, {"excluded": True}, {"mode": 1},
                        {"mode": None}, {"mode": False}, {"id": None}, {"id": ""}):
            with self.subTest(changes=changes):
                self.assertIsNone(evaluate(attempt=play(**changes)))


class QuestDateTests(unittest.TestCase):
    def test_play_must_be_on_or_after_mission_creation(self):
        self.assertIsNone(evaluate(attempt=play(played_at=iso(-121), started_at=None)))
        self.assertTrue(evaluate(attempt=play(played_at=iso(-120), started_at=iso(-120)))["completed"])

    def test_song_started_before_creation_does_not_count(self):
        self.assertIsNone(evaluate(attempt=play(started_at=iso(-121))))
        self.assertTrue(evaluate(attempt=play(started_at=None))["completed"])

    def test_invalid_missing_and_naive_dates_rejected(self):
        for stamp in (None, "bad", "", "2026-09-09T10:00:00"):
            with self.subTest(stamp=stamp):
                self.assertIsNone(evaluate(attempt=play(played_at=stamp)))
                self.assertIsNone(evaluate(quest(created_at=stamp)))
        self.assertIsNone(evaluate(attempt=play(started_at="bad")))
        self.assertIsNone(evaluate(attempt=play(started_at=iso(1))))

    def test_future_tolerance_is_exactly_five_seconds(self):
        self.assertTrue(evaluate(attempt=play(played_at=iso(5)))["completed"])
        self.assertIsNone(evaluate(attempt=play(played_at=iso(5.001))))

    def test_dates_with_different_offsets_compare_in_utc(self):
        self.assertTrue(evaluate(attempt=play(played_at="2026-09-09T07:00:00-03:00"))["completed"])


class QuestCheckTests(unittest.TestCase):
    def test_accuracy_is_not_rounded_before_comparison(self):
        result = evaluate(attempt=play(accuracy=96.999999))
        self.assertFalse(result["completed"])
        self.assertEqual(checks(result)["accuracy"]["status"], "unmet")
        self.assertEqual(checks(result)["accuracy"]["actual"], 96.999999)

    def test_completion_threshold_and_missing_completion(self):
        self.assertTrue(evaluate(attempt=play(completion=.98))["completed"])
        self.assertEqual(checks(evaluate(attempt=play(completion=.97999)))["complete"]["status"], "unmet")
        for field in ("passed", "completion"):
            attempt = play()
            del attempt[field]
            result = evaluate(attempt=attempt)
            self.assertFalse(result["completed"])
            self.assertEqual(checks(result)["complete"]["status"], "unknown")
            self.assertEqual(checks(result)["grade"]["status"], "unknown")

    def test_partial_grade_is_not_trusted(self):
        result = evaluate(attempt=play(completion=.5, grade="SS"))
        self.assertEqual(checks(result)["grade"]["status"], "unknown")
        self.assertFalse(result["completed"])

    def test_failed_play_cannot_complete_even_with_good_numbers_and_stale_s(self):
        result = evaluate(attempt=play(passed=False, completion=.5, grade="S", accuracy=99))
        self.assertFalse(result["completed"])
        self.assertEqual(checks(result)["complete"]["status"], "unmet")
        self.assertEqual(checks(result)["grade"]["actual"], "F")

    def test_missing_measurements_are_unknown_not_zero(self):
        for field, key in (("accuracy", "accuracy"), ("misses", "misses"), ("max_combo", "combo")):
            with self.subTest(field=field):
                attempt = play()
                del attempt[field]
                result = evaluate(attempt=attempt)
                self.assertEqual(checks(result)[key]["status"], "unknown")
                self.assertIsNone(checks(result)[key]["actual"])
                self.assertFalse(result["completed"])

    def test_invalid_counts_percentages_and_rounded_accuracy_stay_unknown(self):
        for changes, key in (({"accuracy": float("nan")}, "accuracy"), ({"accuracy": 101}, "accuracy"),
                             ({"misses": -1}, "misses"), ({"misses": .5}, "misses"),
                             ({"max_combo": True}, "combo"), ({"accuracy_rounded": True}, "accuracy")):
            with self.subTest(changes=changes):
                self.assertEqual(checks(evaluate(attempt=play(**changes)))[key]["status"], "unknown")

    def test_zero_misses_and_exact_combo_are_valid_inclusive_goals(self):
        self.assertTrue(evaluate()["completed"])
        result = evaluate(attempt=play(misses=1))
        self.assertEqual(checks(result)["misses"]["status"], "unmet")
        result = evaluate(attempt=play(max_combo=399))
        self.assertEqual(checks(result)["combo"]["status"], "unmet")

    def test_each_silver_grade_equals_its_base_grade(self):
        for actual in ("SH", "SS", "SSH", "X", "XH"):
            with self.subTest(actual=actual):
                self.assertTrue(evaluate(attempt=play(grade=actual))["completed"])
        task = quest()
        task["map"]["expectation"]["grade_min"] = "SSH"
        self.assertTrue(evaluate(task, play(grade="SS"))["completed"])
        self.assertFalse(evaluate(task, play(grade="S"))["completed"])

    def test_stored_grade_remains_authoritative(self):
        result = evaluate(attempt=play(grade="A", accuracy=99))
        self.assertFalse(result["completed"])
        self.assertEqual(checks(result)["grade"]["status"], "unmet")

    def test_lazer_fallback_grade_uses_actual_accuracy_and_misses(self):
        self.assertTrue(evaluate(attempt=play(grade=None))["completed"])
        result = evaluate(attempt=play(grade=None, misses=1))
        self.assertEqual(checks(result)["grade"]["actual"], "A")

    def test_stable_fallback_checks_300_and_50_ratios(self):
        task = quest()
        task["map"]["expectation"]["accuracy_min"] = 96
        good = play(client="stable", grade=None, n300=95, n100=5, n50=0, accuracy=96.666666666)
        bad = play(client="stable", grade=None, n300=96, n100=0, n50=4, accuracy=96.666666666)
        self.assertTrue(evaluate(task, good)["completed"])
        self.assertEqual(checks(evaluate(task, bad))["grade"]["actual"], "A")
        self.assertFalse(evaluate(task, bad)["completed"])
        del good["n50"]
        self.assertEqual(checks(evaluate(task, good))["grade"]["status"], "unknown")

    def test_goals_from_separate_attempts_are_never_combined(self):
        self.assertFalse(evaluate(attempt=play(id="a", accuracy=99, misses=1))["completed"])
        self.assertFalse(evaluate(attempt=play(id="b", accuracy=96, misses=0))["completed"])

    def test_absent_optional_targets_are_omitted_but_complete_is_always_required(self):
        task = quest()
        task["map"]["expectation"] = {"complete_required": False, "grade_min": None, "combo_min": None}
        result = evaluate(task)
        self.assertEqual([check["key"] for check in result["checks"]], ["complete"])
        self.assertFalse(evaluate(task, play(passed=False))["completed"])

    def test_malformed_present_goal_does_not_make_mission_easier(self):
        task = quest()
        task["map"]["expectation"]["accuracy_min"] = "bad"
        self.assertEqual(checks(evaluate(task))["accuracy"]["status"], "unknown")
        self.assertFalse(evaluate(task)["completed"])

    def test_evaluation_is_stateless_repeatable_and_does_not_mutate_inputs(self):
        task, attempt = quest(), play()
        before = copy.deepcopy((task, attempt))
        first, duplicate = evaluate(task, attempt), evaluate(task, attempt)
        self.assertEqual(first, duplicate)
        self.assertEqual(before, (task, attempt))


class QuestGraceTests(unittest.TestCase):
    def attempt(self, allowed_missing=0, **changes):
        return evaluate_attempt(quest(), play(**changes), now=NOW, allowed_missing=allowed_missing)

    def test_one_allowed_missing_accepts_a_single_unmet_goal(self):
        result = self.attempt(1, misses=2)
        self.assertTrue(result["completed"])
        self.assertEqual(result["grace"], 1)
        self.assertEqual(result["required"], 4)
        self.assertEqual(result["required_total"], 5)
        self.assertEqual(result["met"], 4)
        self.assertEqual([check["status"] for check in result["checks"]],
                         ["met", "met", "met", "unmet", "met"])
        self.assertEqual(checks(result)["complete"]["status"], "met")

    def test_grace_limit_is_exactly_allowed_missing(self):
        self.assertFalse(self.attempt(1, misses=2, max_combo=50)["completed"])
        result = self.attempt(2, misses=2, max_combo=50)
        self.assertTrue(result["completed"])
        self.assertEqual(result["met"], 3)
        self.assertEqual(result["required"], 3)

    def test_finishing_the_map_is_never_waived(self):
        result = self.attempt(4, passed=False, completion=.5, grade="S", accuracy=99)
        self.assertFalse(result["completed"])
        self.assertEqual(checks(result)["complete"]["status"], "unmet")
        self.assertGreaterEqual(result["met"], result["required"])

    def test_default_stays_all_goals_strict(self):
        result = self.attempt(misses=2)
        self.assertFalse(result["completed"])
        self.assertEqual(result["grace"], 0)
        self.assertEqual(result["required"], 5)
        self.assertEqual(result["required_total"], 5)

    def test_unknown_measurement_can_be_the_waived_objective(self):
        attempt = play()
        del attempt["accuracy"]
        result = evaluate_attempt(quest(), attempt, now=NOW, allowed_missing=1)
        self.assertTrue(result["completed"])
        self.assertEqual(checks(result)["accuracy"]["status"], "unknown")

    def test_invalid_allowed_missing_values_fall_back_to_strict(self):
        for bad in (None, "x", True, -5, float("nan")):
            with self.subTest(bad=bad):
                result = self.attempt(bad, misses=2)
                self.assertFalse(result["completed"])
                self.assertEqual(result["grace"], 0)

    def test_single_check_mission_keeps_complete_as_the_only_bar(self):
        task = quest()
        task["map"]["expectation"] = {"complete_required": False, "grade_min": None, "combo_min": None}
        for bad in (1, 3):
            with self.subTest(bad=bad):
                result = evaluate_attempt(task, play(passed=False), now=NOW, allowed_missing=bad)
                self.assertFalse(result["completed"])
                self.assertEqual(result["required"], 1)
                result = evaluate_attempt(task, play(), now=NOW, allowed_missing=bad)
                self.assertTrue(result["completed"])
                self.assertEqual(result["required_total"], 1)

    def test_plan_required_keys_waive_optional_goals_without_grace(self):
        task = quest()
        task["map"]["expectation"]["required_keys"] = ["accuracy"]
        result = evaluate_attempt(task, play(grade="B", misses=5, max_combo=10), now=NOW)
        self.assertTrue(result["completed"])
        self.assertEqual(result["required_total"], 2)
        self.assertEqual(result["required"], 2)
        self.assertEqual(result["met"], 2)
        self.assertEqual(checks(result)["complete"]["required"], True)
        self.assertEqual(checks(result)["accuracy"]["required"], True)
        self.assertEqual(checks(result)["grade"]["required"], False)
        self.assertEqual(checks(result)["misses"]["status"], "unmet")

    def test_grace_waives_plan_required_goals_up_to_the_setting(self):
        task = quest()
        task["map"]["expectation"].update(required_keys=["accuracy", "misses"], misses_max=0)
        result = evaluate_attempt(task, play(misses=3), now=NOW, allowed_missing=1)
        self.assertTrue(result["completed"])
        self.assertEqual(result["required"], 2)
        self.assertEqual(result["met"], 2)
        beyond = evaluate_attempt(task, play(misses=3, accuracy=95), now=NOW, allowed_missing=1)
        self.assertFalse(beyond["completed"])
        self.assertEqual(beyond["met"], 1)

    def test_prescribed_mods_are_never_waived_by_grace(self):
        task = quest()
        task["map"]["play_conditions"] = mod_context("HD")
        wrong = play(misses=0)
        result = evaluate_attempt(task, wrong, now=NOW, allowed_missing=4)
        self.assertFalse(result["completed"])
        self.assertEqual(result["checks"][0]["key"], "mods")
        self.assertEqual(result["checks"][0]["status"], "unmet")
        self.assertEqual(result["checks"][0]["actual"], "Sin mods")
        exact = play(mods=[{"acronym": "HD"}], mod_key='{"mods":[{"acronym":"HD"}],"rate":1}')
        self.assertTrue(evaluate_attempt(task, exact, now=NOW, allowed_missing=0)["completed"])

    def test_mods_and_finished_play_remain_mandatory_when_plan_narrows_goals(self):
        task = quest()
        task["map"]["expectation"]["required_keys"] = ["accuracy"]
        for changes in ({"passed": False}, {"completion": .5}):
            with self.subTest(changes=changes):
                self.assertFalse(evaluate_attempt(task, play(accuracy=97, **changes), now=NOW,
                                                  allowed_missing=3)["completed"])


class QuestModsTests(unittest.TestCase):
    def task(self, stars=5.0, conditions=None):
        task = quest()
        task["map"]["stars"] = stars
        if conditions is not None:
            task["map"]["play_conditions"] = conditions
        return task

    def test_extra_harder_mods_complete_a_plain_mission_through_stars(self):
        task = self.task(5.0, conditions=mod_context())
        attempt = play(mods=[{"acronym": "HD"}], mod_key='{"mods":[{"acronym":"HD"}],"rate":1}', stars=5.4)
        result = evaluate_attempt(task, attempt, now=NOW)
        self.assertTrue(result["completed"])
        check = checks(result)["mods"]
        self.assertEqual(check["status"], "met")
        self.assertEqual(check["target"], "Sin mods")
        self.assertEqual(check["actual"], "HD")
        self.assertIn("5.4★ > 5★", check["reason"])

    def test_easier_equal_or_unmeasurable_loadouts_keep_requiring_exact_conditions(self):
        task = self.task(5.0, conditions=mod_context())
        for changes in ({"stars": 4.4}, {"stars": 5.0}, {}):
            with self.subTest(changes=changes):
                attempt = play(mods=[{"acronym": "HT"}], mod_key='{"mods":[{"acronym":"HT"}],"rate":.75}', **changes)
                result = evaluate_attempt(task, attempt, now=NOW)
                self.assertEqual(checks(result)["mods"]["status"], "unmet")
                self.assertFalse(result["completed"])

    def test_prescribed_mods_stay_stricter_than_plain_or_lighter_plays(self):
        task = self.task(6.0, conditions=mod_context("DT"))
        result = evaluate_attempt(task, play(stars=5.6), now=NOW)
        self.assertEqual(checks(result)["mods"]["status"], "unmet")
        self.assertFalse(result["completed"])

    def test_exact_conditions_need_no_star_fallback_or_reason(self):
        task = self.task(4.0, conditions=mod_context("HD"))
        attempt = play(mods=[{"acronym": "HD"}], mod_key='{"mods":[{"acronym":"HD"}],"rate":1}', stars=4.1)
        result = evaluate_attempt(task, attempt, now=NOW)
        self.assertEqual(checks(result)["mods"]["status"], "met")
        self.assertIsNone(checks(result)["mods"]["reason"])


if __name__ == "__main__":
    unittest.main()
