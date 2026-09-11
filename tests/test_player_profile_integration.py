"""Player-profile policy integration without touching a live store or network."""

import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import tempfile
import unittest
from osu_coach.settings import settings_context

from osu_coach import app
from osu_coach.core import engine
from osu_coach.core.expectations import expectation_for
from osu_coach.core.grades import target_grade


NOW = datetime.now(timezone.utc)


def play(index, **changes):
    item = {
        "id": f"profile-attempt-{index}", "beatmap_key": f"played-{index}",
        "beatmap_id": 1000 + index, "played_at": (NOW - timedelta(minutes=(240 if index < 4 else 120) - index)).isoformat(),
        "player": "Profile fixture", "client": "lazer", "mode": 0, "mods": [],
        "mod_key": app.DEFAULT_MOD_KEY, "stars": 4.0, "accuracy": 98.0,
        "passed": True, "completion": 1.0, "object_count": 400, "judged_objects": 400,
        "misses": 0, "max_combo": 450, "map_max_combo": 500,
        "bpm": 150, "ar": 8, "length": 120,
    }
    item.update(changes)
    return item


def beatmap(index, **changes):
    item = {"key": f"candidate-{index}", "id": 2000 + index, "set_id": 2000 + index,
            "title": f"Fixture song {index}", "artist": "Fixture", "version": "Insane",
            "mode": 0, "stars": 4, "bpm": 150, "ar": 8, "length": 120,
            "object_count": 400, "max_combo": 500, "tags": [], "source": "local"}
    item.update(changes)
    return item


def progression_profile(unlocked=True):
    profile = engine.assess([play(i) for i in range(5)], NOW)
    profile.update(baseline=4.0, challenge_unlocked=unlocked)
    return profile


def player_policy(mode="advance", status="ready", *, tag=None):
    return {"status": status, "progression": {"mode": mode, "focus_key": tag or "combo",
            "label": "Fixture focus", "reason": "Fixture evidence"},
            "priorities": [{"key": tag or "combo", "label": "Fixture focus", "tag": tag,
                            "action": "Practicar con control.", "reason": "Fixture evidence"}]}


def tag_analysis():
    return {"focus_tag": "skillset/jumps", "focus_name": "Saltos", "band": {"min": 3.5, "max": 4.5},
            "items": [{"tag": name, "name": label, "status": "practice", "confidence": "medium",
                       "stats_scope": "comparable", "comparable_plays": 8, "comparable_maps": 5, "comparable_sessions": 2,
                       "evidence_weight": .6} for name, label in
                      [("skillset/jumps", "Saltos"), ("skillset/streams", "Streams")]]}


class PlayerPolicyIntegrationTests(unittest.TestCase):
    def test_applying_policy_preserves_baseline_and_input_objects(self):
        profile = progression_profile()
        player = player_policy("recover")
        before_profile, before_player = copy.deepcopy(profile), copy.deepcopy(player)
        adjusted = engine.apply_player_profile(profile, player)
        self.assertIsNot(adjusted, profile)
        self.assertEqual(profile, before_profile)
        self.assertEqual(player, before_player)
        self.assertEqual(adjusted["baseline"], profile["baseline"])
        self.assertEqual(adjusted["window"], profile["window"])

    def test_recovery_moves_practice_down_and_pauses_challenge(self):
        profile = progression_profile()
        player = player_policy("recover")
        adjusted = engine.apply_player_profile(profile, player)
        groups = engine.recommend([], adjusted, player_profile=player)
        self.assertEqual(groups[1]["target"], 3.85)
        self.assertFalse(adjusted["challenge_unlocked"])
        self.assertEqual(groups[2]["label"], "Consolidar")
        self.assertLessEqual(groups[2]["target"], 4)

    def test_recovery_can_apply_before_general_profile_is_ready(self):
        profile = progression_profile()
        player = player_policy("recover", "learning")
        adjusted = engine.apply_player_profile(profile, player)
        self.assertEqual(adjusted["training_adjustments"]["practice_offset"], -.15)
        self.assertFalse(adjusted["challenge_unlocked"])

    def test_ready_consolidation_reduces_step_without_revoking_existing_unlock(self):
        profile = progression_profile()
        player = player_policy("consolidate")
        adjusted = engine.apply_player_profile(profile, player)
        groups = engine.recommend([], adjusted, player_profile=player)
        self.assertTrue(adjusted["challenge_unlocked"])
        self.assertEqual(groups[1]["target"], 3.9)
        self.assertEqual(groups[2]["target"], 4.0)

    def test_learning_consolidation_does_not_apply_an_unbacked_difficulty_adjustment(self):
        profile = progression_profile()
        player = player_policy("consolidate", "learning")
        adjusted = engine.apply_player_profile(profile, player)
        groups = engine.recommend([], adjusted, player_profile=player)
        self.assertEqual(groups[1]["target"], 4)
        self.assertEqual(groups[2]["target"], 4.0)

    def test_other_modes_keep_existing_small_step(self):
        for mode in ("advance", "calibrate"):
            with self.subTest(mode=mode):
                player = player_policy(mode)
                adjusted = engine.apply_player_profile(progression_profile(), player)
                groups = engine.recommend([], adjusted, player_profile=player)
                self.assertEqual(groups[1]["target"], 4)
                self.assertEqual(groups[2]["target"], 4.0)

    def test_player_profile_cannot_unlock_a_base_locked_challenge(self):
        for mode in ("advance", "consolidate", "recover"):
            with self.subTest(mode=mode):
                adjusted = engine.apply_player_profile(progression_profile(False), player_policy(mode))
                self.assertFalse(adjusted["challenge_unlocked"])

    def test_repeated_state_adaptation_does_not_accumulate_offsets(self):
        player = player_policy("recover")
        once = engine.apply_player_profile(progression_profile(), player)
        twice = engine.apply_player_profile(once, player)
        self.assertEqual(once["baseline"], twice["baseline"])
        self.assertEqual(once["training_adjustments"], twice["training_adjustments"])

    def test_profile_focus_substitutes_existing_tag_bonus(self):
        analysis = tag_analysis()
        player = player_policy("calibrate", "ready", tag="skillset/streams")
        profile = engine.apply_player_profile(progression_profile(), player)
        maps = [beatmap(1, key="neutral"),
                beatmap(2, key="streams", stars=4.02, tags=[{"name": "skillset/streams", "source": "manual"}]),
                beatmap(3, key="jumps", stars=4.02, tags=[{"name": "skillset/jumps", "source": "manual"}])]
        expected_analysis = copy.deepcopy(analysis)
        expected_analysis.update(focus_tag="skillset/streams", focus_name="Streams")
        reference = engine.recommend(maps, profile, limit=1, tag_analysis=expected_analysis)
        actual = engine.recommend(maps, profile, limit=1, tag_analysis=analysis, player_profile=player)
        self.assertEqual([m["key"] for m in actual[1]["maps"]], [m["key"] for m in reference[1]["maps"]])
        self.assertEqual(actual[1]["maps"][0]["key"], "streams")
        self.assertEqual(analysis["focus_tag"], "skillset/jumps")

    def test_unbacked_player_tag_cannot_override_analysis_focus(self):
        analysis = tag_analysis()
        analysis["items"][1].update(confidence="low", comparable_plays=1, comparable_maps=1)
        player = player_policy("calibrate", tag="skillset/streams")
        profile = engine.apply_player_profile(progression_profile(), player)
        maps = [beatmap(1, key="neutral"),
                beatmap(2, key="streams", stars=4.02, tags=[{"name": "skillset/streams", "source": "manual"}]),
                beatmap(3, key="jumps", stars=4.02, tags=[{"name": "skillset/jumps", "source": "manual"}])]
        groups = engine.recommend(maps, profile, limit=1, tag_analysis=analysis, player_profile=player)
        self.assertEqual(groups[1]["maps"][0]["key"], "jumps")

    def test_profile_keeps_grade_and_numeric_targets_coherent_without_second_tag_adjustment(self):
        analysis = tag_analysis()
        player = player_policy("consolidate", tag="skillset/streams")
        profile = engine.apply_player_profile(progression_profile(), player)
        maps = [beatmap(i, stars=3.5 + .025 * i, tags=[{"name": "skillset/streams", "source": "manual"}])
                for i in range(35)]
        lookup = {m["key"]: m for m in maps}
        groups = engine.recommend(maps, profile, tag_analysis=analysis, player_profile=player)
        count = 0
        for group in groups:
            for item in group["maps"]:
                count += 1
                actual = item["expectation"]
                from osu_coach.core.training import training_goal
                expected = expectation_for(lookup[item["key"]], profile, actual['stage'], analysis)
                expected = training_goal(expected, lookup[item['key']], profile, actual['stage'], actual.get('focus'))
                for key in ("accuracy_min", "misses_max", "combo_min", "grade_min"):
                    self.assertEqual(actual[key], expected[key])
                grade = target_grade("lazer", actual["accuracy_min"], actual["misses_max"])
                self.assertEqual(actual["grade_min"], grade["grade"])
        self.assertGreater(count, 0)

    @settings_context({"bpm_hard_limit": True})
    def test_player_focus_cannot_bypass_physical_limits(self):
        analysis = tag_analysis()
        player = player_policy("consolidate", tag="skillset/streams")
        profile = engine.apply_player_profile(progression_profile(), player)
        tags = [{"name": "skillset/streams", "source": "manual"}]
        invalid = [beatmap(1, bpm=166, tags=tags), beatmap(2, ar=8.71, tags=tags),
                   beatmap(4, stars=7, tags=tags)]
        groups = engine.recommend(invalid, profile, tag_analysis=analysis, player_profile=player)
        self.assertTrue(all(not group["maps"] for group in groups))


class PlayerProfileAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        args = SimpleNamespace(data_dir=self.temp.name, demo=False, maps=None, no_tosu=True,
                               tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = app.Coach(args)
        self.addCleanup(self.coach.close)
        self.addCleanup(self.coach.db.close)
        self.coach.config["since"] = (NOW - timedelta(days=6)).isoformat()

    def add(self, rows):
        for row in rows:
            self.coach.add_play(copy.deepcopy(row))

    def test_general_profile_needs_eight_attempts_five_maps_and_two_sessions(self):
        self.add([play(i) for i in range(7)])
        self.assertEqual(self.coach.state()["player_profile"]["status"], "learning")
        self.add([play(7)])
        self.assertEqual(self.coach.state()["player_profile"]["status"], "ready")

    def test_repetition_cannot_make_general_profile_ready(self):
        self.add([play(i, beatmap_key="same") for i in range(10)])
        state = self.coach.state()
        self.assertEqual(state["player_profile"]["status"], "learning")
        self.assertEqual(state["profile"]["distinct_maps"], 1)

    def test_other_players_clients_mods_and_expired_data_do_not_change_active_profile(self):
        self.add([play(i) for i in range(5)])
        active = self.coach.active
        before = self.coach.state()["player_profile"]
        noise = [play(20, player="Other player", accuracy=60, passed=False),
                 play(21, client="stable", accuracy=60, passed=False),
                 play(22, mods=["DT"], mod_key='{"mods":["DT"],"rate":1.5}', accuracy=60, passed=False),
                 play(23, played_at=(NOW - timedelta(days=9)).isoformat(), accuracy=60, passed=False)]
        self.add(noise)
        self.coach.active = active
        after = self.coach.state()["player_profile"]
        self.assertEqual(before, after)

    def test_rank_and_pp_have_no_effect_on_player_profile(self):
        rows = [play(i) for i in range(5)]
        self.add(rows)
        before = self.coach.state()["player_profile"]
        for row in rows:
            row.update(rank=1, global_rank=1, pp=999999, historical_best_pp=999999)
            self.coach.db.execute("UPDATE plays SET data=? WHERE id=?", (json.dumps(row), row["id"]))
        self.coach.db.commit()
        self.assertEqual(before, self.coach.state()["player_profile"])

    def test_recalibration_clears_profile_evidence_but_retains_records(self):
        self.add([play(i) for i in range(5)])
        self.coach.reset()
        state = self.coach.state()
        self.assertEqual(state["profile"]["attempts"], 0)
        self.assertEqual(state["player_profile"]["status"], "learning")
        self.assertEqual(len(self.coach.plays()), 5)

    def test_profile_keeps_longer_evidence_while_recent_session_stays_twenty(self):
        self.add([play(i) for i in range(25)])
        state = self.coach.state()
        self.assertEqual(state["profile"]["attempts"], 25)
        self.assertEqual(len(state["recent"]), 20)
        self.assertEqual(state["player_profile"]["evidence"]["plays"], 25)
        self.assertEqual(state["profile"]["session"]["attempts"], 20)
        expected_ids = {f"profile-attempt-{i}" for i in range(5, 25)}
        self.assertEqual({item["id"] for item in state["recent"]}, expected_ids)

    def test_profile_caps_at_one_hundred_while_preserving_the_full_score_log(self):
        self.add([play(i, played_at=(NOW - timedelta(hours=104-i)).isoformat()) for i in range(105)])
        state = self.coach.state()
        self.assertEqual(100, state["player_profile"]["evidence"]["plays"])
        self.assertEqual(20, len(state["recent"]))
        self.assertEqual(105, len(self.coach.plays()))

    def test_older_evidence_remains_in_profile_but_does_not_trigger_session_recovery(self):
        self.coach.config["since"] = (NOW - timedelta(days=31)).isoformat()
        self.add([play(i, played_at=(NOW - timedelta(days=20 if i < 4 else 10)).isoformat(),
                       passed=False, completion=.5, accuracy=80, misses=20, judged_objects=200) for i in range(8)])
        state = self.coach.state()
        self.assertEqual(8, state["player_profile"]["evidence"]["plays"])
        self.assertEqual(0, state["profile"]["session"]["attempts"])
        self.assertNotEqual("recover", state["player_profile"]["progression"]["mode"])

    def test_state_read_does_not_mutate_logged_scores(self):
        self.add([play(i, accuracy=96, max_combo=300) for i in range(5)])
        before = copy.deepcopy(self.coach.plays())
        first = self.coach.state()
        second = self.coach.state()
        self.assertEqual(before, self.coach.plays())
        self.assertEqual({key: value for key, value in first["profile"].items() if key != "evaluated_at"},
                         {key: value for key, value in second["profile"].items() if key != "evaluated_at"})
        self.assertEqual(first["player_profile"], second["player_profile"])

    def test_unknown_combo_counters_stay_unknown_in_current_player_profile(self):
        self.add([play(i, max_combo=None) for i in range(5)])
        player = self.coach.state()["player_profile"]
        combo = next(item for item in player["dimensions"] if item["key"] == "combo")
        self.assertEqual(combo["status"], "learning")
        self.assertIsNone(combo["value"])
        self.assertNotIn("combo", {item["key"] for item in player["strengths"]})

    def test_two_recent_comparable_failures_recover_before_general_profile_is_ready(self):
        self.add([play(i, passed=False, completion=.5, accuracy=80, misses=20, judged_objects=200)
                  for i in range(2)])
        state = self.coach.state()
        self.assertEqual(state["player_profile"]["status"], "learning")
        self.assertEqual(state["player_profile"]["progression"]["mode"], "recover")
        self.assertFalse(state["profile"]["challenge_unlocked"])
        self.assertEqual(state["profile"]["training_adjustments"]["practice_offset"], -.25)


if __name__ == "__main__":
    unittest.main()
