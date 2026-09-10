"""Grade boundaries and honest minimum goals for osu!standard."""

import unittest

from grades import evaluate_grade, normalize_grade, target_grade


def lazer(accuracy, misses=0, **changes):
    play = {"client": "lazer", "mode": 0, "accuracy": accuracy, "misses": misses, "passed": True}
    play.update(changes)
    return play


def stable(n300, n100=0, n50=0, misses=0, **changes):
    play = {"client": "stable", "mode": 0, "n300": n300, "n100": n100,
            "n50": n50, "misses": misses, "passed": True}
    play.update(changes)
    return play


class TargetGradeTests(unittest.TestCase):
    def test_lazer_accuracy_boundaries_and_misses(self):
        cases = [(95, 0, "S"), (94.999, 0, "A"), (99, 1, "A"), (100, None, "A"),
                 (90, 5, "A"), (89.999, 0, "B"), (80, 5, "B"), (79.999, 0, "C"),
                 (70, 5, "C"), (69.999, 0, "D"), (100, 0, "S")]
        for accuracy, misses, expected in cases:
            with self.subTest(accuracy=accuracy, misses=misses):
                target = target_grade("lazer", accuracy, misses)
                self.assertEqual(target["grade"], expected)
                self.assertEqual(target["label"], f"{expected} o mejor")
                self.assertFalse(target["grade_is_conditional"])

    def test_classic_mod_preserves_lazer_rule(self):
        self.assertEqual(target_grade("lazer", 95, 0, ["CL"]), target_grade("lazer", 95, 0))

    def test_stable_goals_include_judgement_conditions(self):
        target = target_grade("stable", 97, 0)
        self.assertEqual(target["grade"], "S")
        self.assertTrue(target["grade_is_conditional"])
        self.assertIn("Más del 90 %", " ".join(target["requirements"]))
        self.assertIn("como máximo 1 %", " ".join(target["requirements"]))
        target = target_grade("stable", 95, 3)
        self.assertEqual(target["grade"], "A")
        self.assertIn("o más del 90 %", " ".join(target["requirements"]))

    def test_silver_colour_keeps_minimum_goal(self):
        for mods in (["HD"], [{"acronym": "FL"}], "HD+HR"):
            with self.subTest(mods=mods):
                target = target_grade("lazer", 97, 0, mods)
                self.assertEqual(target["grade"], "S")
                self.assertIn("plateada", target["note"])
        self.assertNotIn("plateada", target_grade("lazer", 95, 1, ["HD"])["note"])
        self.assertNotIn("plateada", target_grade("lazer", 97, 0, ["TC", "BL"])["note"])

    def test_unknown_or_invalid_target_does_not_invent_grade(self):
        cases = [(None, 97, 0), ("lazer", float("nan"), 0), ("stable", 101, 0),
                 ("lazer", 97, -1), ("lazer", 97, 1.5), ("lazer", True, 0)]
        for client, accuracy, misses in cases:
            with self.subTest(client=client, accuracy=accuracy, misses=misses):
                self.assertIsNone(target_grade(client, accuracy, misses)["grade"])


class ActualGradeTests(unittest.TestCase):
    def test_lazer_inclusive_boundaries(self):
        for accuracy, expected in [(100, "SS"), (99.999, "S"), (95, "S"), (94.999, "A"),
                                   (90, "A"), (89.999, "B"), (80, "B"), (79.999, "C"),
                                   (70, "C"), (69.999, "D")]:
            with self.subTest(accuracy=accuracy):
                self.assertEqual(evaluate_grade(lazer(accuracy)), expected)

    def test_lazer_misses_downgrade_high_grades(self):
        self.assertEqual(evaluate_grade(lazer(99, 1)), "A")
        self.assertEqual(evaluate_grade(lazer(95, 2, mods=["CL"])), "A")

    def test_s_does_not_require_full_combo_or_complete_slider_tails(self):
        self.assertEqual(evaluate_grade(lazer(97, max_combo=100, map_max_combo=400,
                                              slider_breaks=4, slider_tail_misses=2)), "S")

    def test_stable_strict_300_ratio_thresholds(self):
        for n300, misses, expected in [(91, 0, "S"), (90, 0, "A"), (81, 0, "A"),
                                      (80, 0, "B"), (71, 0, "B"), (70, 0, "C"),
                                      (61, 0, "C"), (60, 0, "D"), (91, 1, "A"),
                                      (90, 1, "B"), (81, 1, "B"), (80, 1, "C")]:
            with self.subTest(n300=n300, misses=misses):
                self.assertEqual(evaluate_grade(stable(n300, 100 - n300 - misses, misses=misses)), expected)

    def test_stable_s_accepts_exactly_one_percent_50s(self):
        self.assertEqual(evaluate_grade(stable(91, 8, 1)), "S")
        self.assertEqual(evaluate_grade(stable(91, 7, 2)), "A")

    def test_same_aggregate_accuracy_can_have_different_stable_grade(self):
        # Both are 96 2/3 %, but 4 % 50s prevents S in the second score.
        self.assertEqual(evaluate_grade(stable(95, 5)), "S")
        self.assertEqual(evaluate_grade(stable(96, 0, 4)), "A")

    def test_stable_s_is_possible_below_lazer_cutoff(self):
        self.assertEqual(evaluate_grade(stable(91, 9, accuracy=94)), "S")
        self.assertEqual(evaluate_grade(lazer(94)), "A")

    def test_stable_ss_and_silver_mods(self):
        self.assertEqual(evaluate_grade(stable(100)), "SS")
        self.assertEqual(evaluate_grade(stable(100, mods=["FL"])), "SSH")
        self.assertEqual(evaluate_grade(lazer(98, mods=["HD"])), "SH")
        self.assertEqual(evaluate_grade(lazer(98, mods=["TC", "BL"])), "S")

    def test_stored_grade_is_authoritative_and_numeric_rank_is_ignored(self):
        self.assertEqual(evaluate_grade(lazer(99, grade="a")), "A")
        self.assertEqual(evaluate_grade(lazer(99, score_grade="XH")), "SSH")
        self.assertEqual(evaluate_grade(lazer(99, rank=1)), "S")
        self.assertIsNone(normalize_grade("1"))
        self.assertIsNone(normalize_grade(1))

    def test_missing_or_invalid_data_stays_unknown(self):
        cases = [{}, {"client": "stable", "accuracy": 99, "misses": 0, "passed": True},
                 lazer(99, misses=None), lazer(99, passed=None), lazer(99, mode=1),
                 lazer(99, completion=.5), lazer(100, accuracy_rounded=True),
                 lazer(float("nan")), lazer(101), stable(0), stable(-1, 100), stable(1.5, 99)]
        for play in cases:
            with self.subTest(play=play):
                self.assertIsNone(evaluate_grade(play))

    def test_observed_failure_is_f(self):
        self.assertEqual(evaluate_grade(lazer(99, passed=False)), "F")


if __name__ == "__main__":
    unittest.main()
