"""Personal progress is persistent evidence, isolated from live data and services."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app
import engine
from progress_store import ProgressStore


class ProgressIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime.now(timezone.utc)
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None,
                                       no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = app.Coach(self.args)
        self.coach.config["since"] = (self.now - timedelta(days=1)).isoformat()
        app.save_json(self.coach.config_path, self.coach.config)
        self.maps = [
            {"key": f"candidate-{i}", "id": 20000 + i, "set_id": 20000 + i,
             "title": f"Progress fixture {i}", "artist": "Fixture", "version": "Insane",
             "mode": 0, "stars": 4.0 + i * .05, "bpm": 150, "ar": 8, "length": 120,
             "object_count": 400, "max_combo": 500, "tags": [], "source": "local"}
            for i in range(24)
        ]
        self.coach.catalog = deepcopy(self.maps)

    def tearDown(self):
        self.coach.close()
        self.coach.db.close()
        self.temp.cleanup()

    def play(self, index, **changes):
        result = {
            "id": f"progress-{index}", "beatmap_key": f"played-{index}", "beatmap_id": 10000 + index,
            "played_at": (self.now - timedelta(minutes=180 - index)).isoformat(),
            "player": "Progress fixture", "client": "lazer", "mode": 0,
            "mods": [], "mod_key": app.DEFAULT_MOD_KEY, "stars": 4.5,
            "accuracy": 98.0, "misses": 0, "passed": True, "completion": 1.0,
            "object_count": 400, "judged_objects": 400, "max_combo": 490, "map_max_combo": 500,
            "bpm": 150, "ar": 8, "length": 120, "grade": "S",
        }
        result.update(changes)
        return result

    def seed(self, count=5, start=0, **changes):
        for index in range(start, start + count):
            self.coach.add_play(self.play(index, **changes))

    def progress(self):
        return self.coach.state()["coach_progress"]

    def reopen(self):
        self.coach.close()
        self.coach.db.close()
        self.coach = app.Coach(self.args)
        self.coach.catalog = deepcopy(self.maps)

    def test_empty_progress_exposes_learning_and_no_invented_record(self):
        self.assertIsNone(self.progress())
        self.coach.add_play(self.play(0))
        progress = self.progress()
        self.assertEqual("learning", progress["status"])
        self.assertIsNone(progress["initial_reference"])
        self.assertIsNone(progress["best_reference"])
        self.assertIsNone(progress["rank"]["stars"])
        self.assertEqual(1, len(progress["history"]))
        self.assertEqual(1, progress["tracked_plays"])
        self.assertIsNone(progress["change"])
        self.assertEqual(3, progress["next_rank"]["required_maps"])
        self.assertFalse(progress["next_rank"]["calibrated"])
        self.assertEqual(100, progress["next_rank"]["window_plays"])
        self.assertEqual(30, progress["next_rank"]["window_days"])
        self.assertEqual(2, progress["method_version"])
        self.assertFalse(progress["has_legacy_history"])
        self.assertEqual(2, progress["history"][0]["method_version"])

    def test_comparison_and_record_begin_at_first_calibrated_reference(self):
        self.seed(count=2, stars=6)
        self.seed(count=2, start=2, stars=4, accuracy=85, misses=20,
                  max_combo=100, passed=False, completion=.8)
        learning = self.progress()
        self.assertEqual(4, learning["tracked_plays"])
        self.assertEqual(4, len(learning["history"]))
        self.assertFalse(any(point["calibrated"] for point in learning["history"]))
        self.assertIsNone(learning["initial_reference"])
        self.assertIsNone(learning["best_reference"])
        self.coach.add_play(self.play(4))
        ready = self.progress()
        self.assertEqual("ready", ready["status"])
        calibrated = [point["reference"] for point in ready["history"] if point["calibrated"]]
        self.assertTrue(calibrated)
        self.assertEqual(calibrated[0], ready["initial_reference"])
        self.assertEqual(max(calibrated), ready["best_reference"])
        self.assertLess(ready["best_reference"], max(point["reference"] for point in learning["history"]))
        self.assertAlmostEqual(ready["current_reference"] - ready["initial_reference"], ready["change"])
        self.assertTrue(all(point["source"] == "recorded" for point in ready["history"]))

    def test_history_rank_and_milestones_survive_restart_without_duplicate_points(self):
        self.seed()
        expected = self.progress()
        self.assertIsNotNone(expected["rank"]["stars"])
        self.assertGreaterEqual(expected["rank"]["stars"], 4.5)
        self.reopen()
        actual = self.progress()
        for field in ("history", "tracked_plays", "initial_reference", "best_reference", "rank", "milestones"):
            self.assertEqual(expected[field], actual[field], field)
        self.assertEqual(actual, self.progress())

    def test_earned_rank_does_not_fall_when_recent_performance_falls(self):
        self.seed()
        earned = self.progress()
        self.seed(count=12, start=5, stars=3.4, accuracy=78, misses=30, max_combo=100,
                  passed=False, completion=.7, grade="F")
        struggling = self.progress()
        self.assertLess(struggling["current_reference"], earned["current_reference"])
        self.assertEqual(earned["rank"], struggling["rank"])
        self.assertGreaterEqual(struggling["best_reference"], earned["best_reference"])
        self.assertEqual(earned["initial_reference"], struggling["initial_reference"])

    def test_pending_rejected_duplicate_excluded_and_other_modes_do_not_add_progress(self):
        self.seed()
        expected = self.progress()["tracked_plays"]
        rejected = self.play(5, needs_confirmation=True)
        self.coach.add_play(rejected)
        self.assertEqual(expected, self.progress()["tracked_plays"])
        self.coach.confirm(rejected["id"], False)
        self.assertEqual(expected, self.progress()["tracked_plays"])
        pending = self.play(6, needs_confirmation=True)
        self.coach.add_play(pending)
        self.coach.confirm(pending["id"], True)
        expected += 1
        self.assertEqual(expected, self.progress()["tracked_plays"])
        self.coach.add_play(deepcopy(pending))
        self.coach.add_play(self.play(7, excluded=True))
        self.coach.add_play(self.play(8, mode=3))
        self.assertEqual(expected, self.progress()["tracked_plays"])
        self.assertEqual("recorded", self.progress()["history"][-1]["source"])

    def test_delayed_confirmation_records_first_calibrated_reference_without_rewriting_history(self):
        self.coach.add_play(self.play(0))
        delayed = self.play(1, needs_confirmation=True)
        self.coach.add_play(delayed)
        self.seed(count=3, start=2)
        before = self.progress()
        self.assertEqual("learning", before["status"])
        self.assertEqual(4, before["tracked_plays"])
        self.coach.confirm(delayed["id"], True)
        after = self.progress()
        self.assertEqual("ready", after["status"])
        self.assertEqual(5, after["tracked_plays"])
        self.assertEqual(5, len(after["history"]))
        self.assertEqual(before["history"], after["history"][:-1])
        latest = after["history"][-1]
        self.assertTrue(latest["calibrated"])
        self.assertEqual("recorded", latest["source"])
        self.assertEqual(latest["reference"], after["initial_reference"])
        self.assertEqual(latest["reference"], after["best_reference"])
        self.assertIsNotNone(after["change"])
        stored = next(play for play in self.coach.plays() if play["id"] == delayed["id"])
        self.assertEqual(delayed["played_at"], stored["played_at"])
        self.reopen()
        self.assertEqual(after, self.progress())

    def test_player_client_and_mod_profiles_keep_independent_rank_and_history(self):
        self.seed()
        active = self.coach.active
        original = self.progress()
        variants = [
            {"player": "Another player"},
            {"client": "stable"},
            {"mods": [{"acronym": "HD"}], "mod_key": '{"mods":[{"acronym":"HD"}],"rate":1.0}'},
        ]
        for index, fields in enumerate(variants, 5):
            with self.subTest(fields=fields):
                self.coach.add_play(self.play(index, **fields))
                with patch.object(self.coach, "catalog_for", return_value=([], "")):
                    other = self.progress()
                self.assertEqual(1, other["tracked_plays"])
                self.assertIsNone(other["rank"]["stars"])
                self.assertIsNone(other["initial_reference"])
        self.coach.active = active
        self.assertEqual(original, self.progress())

    def test_reset_keeps_earned_rank_but_starts_new_comparison_and_evidence(self):
        self.seed()
        earned = self.progress()
        self.coach.reset()
        reset = self.progress()
        self.assertEqual(earned["rank"], reset["rank"])
        self.assertEqual(earned["milestones"], reset["milestones"])
        self.assertEqual([], reset["history"])
        self.assertEqual(0, reset["tracked_plays"])
        self.assertIsNone(reset["initial_reference"])
        self.assertIsNone(reset["best_reference"])
        self.assertFalse(reset["next_rank"]["calibrated"])
        self.assertEqual(0, reset["next_rank"]["completed_maps"])
        self.assertEqual(5, len(self.coach.plays()))
        self.reopen()
        self.assertEqual(reset, self.progress())

    def test_rank_award_does_not_change_engine_policy_or_existing_missions(self):
        self.seed(count=1)
        board = self.coach.state()["quest_board"]
        self.seed(count=4, start=1)
        state = self.coach.state()
        self.assertIsNotNone(state["coach_progress"]["rank"]["stars"])
        self.assertEqual(board, state["quest_board"])
        base = engine.assess(self.coach.plays(), since=self.coach.config["since"],
                             initial=self.coach.config.get("initial_stars", 2.5))
        adjusted = engine.apply_player_profile(base, state["player_profile"])
        expected = engine.recommend(self.coach.tag_store.enrich(self.maps), adjusted,
                                    tag_analysis=state["tag_analysis"], player_profile=state["player_profile"])
        self.assertEqual(base["baseline"], state["coach_progress"]["current_reference"])
        self.assertEqual(base["baseline"], state["profile"]["baseline"])
        self.assertEqual(expected, state["recommendations"])

    def test_next_rank_evidence_survives_twenty_but_expires_after_one_hundred_new_results(self):
        self.seed()
        earned = self.progress()["rank"]
        self.seed(count=2, start=5, stars=4.75)
        self.assertEqual(2, self.progress()["next_rank"]["completed_maps"])
        self.seed(count=20, start=7, stars=4.5, accuracy=92, misses=10, max_combo=250, grade="A")
        self.assertEqual(2, self.progress()["next_rank"]["completed_maps"])
        self.seed(count=80, start=27, stars=4.5, accuracy=92, misses=10, max_combo=250, grade="A")
        progress = self.progress()
        self.assertEqual(earned, progress["rank"])
        self.assertEqual(0, progress["next_rank"]["completed_maps"])
        self.assertEqual([], progress["next_rank"]["qualifying_maps"])
        self.assertEqual(107, progress["tracked_plays"])
        self.assertEqual(107, len(progress["history"]))

    def test_next_rank_evidence_uses_thirty_days_and_preserves_earned_milestone(self):
        self.coach.config["since"] = (self.now - timedelta(days=45)).isoformat()
        self.seed()
        earned = self.progress()["rank"]
        for index in (5, 6):
            self.coach.add_play(self.play(index, stars=4.75,
                played_at=(self.now - timedelta(days=10, minutes=index)).isoformat()))
        self.assertEqual(2, self.progress()["next_rank"]["completed_maps"])
        future = self.now + timedelta(days=21)
        assessment = engine.assess(self.coach.plays(), now=future, since=self.coach.config["since"])
        expired = self.coach.progress_store.snapshot(self.coach.active, self.coach.config["since"], assessment)
        self.assertEqual(0, expired["next_rank"]["completed_maps"])
        self.assertEqual(earned, expired["rank"])

    def test_next_rank_requires_three_distinct_solid_difficulties(self):
        self.seed()
        initial = self.progress()["rank"]["stars"]
        self.seed(count=2, start=5, stars=4.75)
        approaching = self.progress()
        self.assertEqual(initial, approaching["rank"]["stars"])
        self.assertEqual(4.75, approaching["next_rank"]["stars"])
        self.assertEqual(2, approaching["next_rank"]["completed_maps"])
        self.coach.add_play(self.play(7, stars=4.75, beatmap_key="played-6", beatmap_id=10006))
        self.assertEqual(initial, self.progress()["rank"]["stars"])
        self.assertEqual(2, self.progress()["next_rank"]["completed_maps"])
        self.coach.add_play(self.play(8, stars=4.75))
        achieved = self.progress()
        self.assertEqual(4.75, achieved["rank"]["stars"])
        self.assertEqual(5.0, achieved["next_rank"]["stars"])
        self.assertIn(4.75, [item["stars"] for item in achieved["milestones"]])

    def test_history_limit_retains_total_count_and_calibrated_start(self):
        self.seed(count=130)
        progress = self.progress()
        self.assertEqual(130, progress["tracked_plays"])
        self.assertLessEqual(len(progress["history"]), 120)
        self.assertGreater(len(progress["history"]), 1)
        self.assertIsNotNone(progress["initial_reference"])
        self.assertLessEqual(len(progress["milestones"]), 12)
        self.reopen()
        self.assertEqual(progress, self.progress())

    def test_legacy_scores_are_reconstructed_once_and_ignore_global_rank_and_pp(self):
        self.coach.close()
        self.coach.db.close()
        legacy = Path(self.temp.name) / "legacy"
        legacy.mkdir()
        self.args.data_dir = str(legacy)
        config = {"since": (self.now - timedelta(days=1)).isoformat(), "initial_stars": 2.5,
                  "maps_path": str(legacy / "maps"), "active": app.profile_key(self.play(0))}
        app.save_json(legacy / "config.json", config)
        scores = [self.play(index, pp=50000, global_rank=1, historical_best_accuracy=100) for index in range(5)]
        rows = [(score["id"], json.dumps(score), "accepted") for score in scores]
        for score, status in ((self.play(5, stars=10, needs_confirmation=True), "pending"),
                              (self.play(6, stars=10), "rejected"),
                              (self.play(7, stars=10, excluded=True), "accepted"),
                              (self.play(8, stars=10, played_at=(self.now - timedelta(days=2)).isoformat()), "accepted")):
            rows.append((score["id"], json.dumps(score), status))
        with closing(sqlite3.connect(legacy / "coach.sqlite3")) as db:
            with db:
                db.execute("CREATE TABLE plays (id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL)")
                db.executemany("INSERT INTO plays VALUES (?, ?, ?)", rows)
        self.coach = app.Coach(self.args)
        self.coach.catalog = deepcopy(self.maps)
        progress = self.progress()
        self.assertEqual(5, progress["tracked_plays"])
        self.assertEqual(5, len(progress["history"]))
        self.assertTrue(all(point["source"] == "reconstructed" for point in progress["history"]))
        expected = engine.assess(scores, since=config["since"])
        self.assertEqual(expected["baseline"], progress["current_reference"])
        self.assertLess(progress["rank"]["stars"], 5)
        self.reopen()
        self.assertEqual(progress, self.progress())


class ProgressMethodMigrationTests(unittest.TestCase):
    """Old schemas and reference values survive upgrades without live data."""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.since = (self.now - timedelta(days=60)).isoformat()
        self.profile = "migration-fixture"
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("""CREATE TABLE coach_progress_points (
            profile TEXT NOT NULL, epoch TEXT NOT NULL, play_id TEXT NOT NULL,
            played_at TEXT NOT NULL, reference REAL NOT NULL, calibrated INTEGER NOT NULL,
            source TEXT NOT NULL, PRIMARY KEY (profile, epoch, play_id))""")
        self.db.execute("""CREATE TABLE coach_rank_milestones (
            profile TEXT NOT NULL, stars REAL NOT NULL, earned_at TEXT NOT NULL,
            source TEXT NOT NULL, PRIMARY KEY (profile, stars))""")
        self.old_plays = [self.play(index, days=20) for index in range(5)]
        self.original_rows = [(self.profile, self.since, play["id"], play["played_at"],
                               9.9, 1, "recorded") for play in self.old_plays]
        self.db.executemany("INSERT INTO coach_progress_points VALUES (?, ?, ?, ?, ?, ?, ?)", self.original_rows)
        self.original_rank = (self.profile, 4.5, self.old_plays[-1]["played_at"], "recorded")
        self.db.execute("INSERT INTO coach_rank_milestones VALUES (?, ?, ?, ?)", self.original_rank)

    def play(self, index, *, days=0, **changes):
        result = {"id": f"migration-{index}", "beatmap_key": f"migration-map-{index}",
                  "beatmap_id": 30000 + index, "stars": 4.5, "mode": 0,
                  "played_at": (self.now - timedelta(days=days, minutes=20 - index)).isoformat(),
                  "accuracy": 98, "misses": 0, "judged_objects": 400,
                  "object_count": 400, "max_combo": 450, "map_max_combo": 500,
                  "passed": True, "completion": 1}
        result.update(changes)
        return result

    def old_columns(self):
        return self.db.execute("""SELECT profile, epoch, play_id, played_at, reference, calibrated, source
            FROM coach_progress_points ORDER BY play_id""").fetchall()

    def test_upgrade_adds_version_without_rewriting_old_points_or_ranks(self):
        store = ProgressStore(self.db)
        self.assertEqual(self.original_rows, self.old_columns())
        self.assertEqual([(1,)] * 5, self.db.execute("SELECT method_version FROM coach_progress_points").fetchall())
        self.assertEqual([self.original_rank], self.db.execute("SELECT * FROM coach_rank_milestones").fetchall())
        assessment = engine.assess(self.old_plays, now=self.now, since=self.since)
        snapshot = store.snapshot(self.profile, self.since, assessment)
        self.assertTrue(snapshot["has_legacy_history"])
        self.assertEqual(2, snapshot["method_version"])
        self.assertIsNone(snapshot["initial_reference"])
        self.assertIsNone(snapshot["best_reference"])
        self.assertIsNone(snapshot["change"])
        self.assertTrue(all(point["reference"] == 9.9 and point["method_version"] == 1
                            for point in snapshot["history"]))
        self.assertIn("no cuenta como mejora", snapshot["method_note"])
        self.assertIn("rangos ganados se conservan", snapshot["method_note"])

    def test_rereading_expanded_window_does_not_create_points_or_award_ranks(self):
        store = ProgressStore(self.db)
        accepted = self.old_plays + [self.play(index, days=10, stars=5) for index in range(5, 8)]
        for _ in range(2):
            store.sync(self.profile, self.since, accepted)
            self.assertEqual(self.original_rows, self.old_columns())
            self.assertEqual([self.original_rank], self.db.execute("SELECT * FROM coach_rank_milestones").fetchall())
        reopened = ProgressStore(self.db)
        reopened.sync(self.profile, self.since, accepted)
        self.assertEqual(self.original_rows, self.old_columns())
        self.assertEqual([self.original_rank], self.db.execute("SELECT * FROM coach_rank_milestones").fetchall())

    def test_first_new_result_starts_comparison_under_v2_and_preserves_v1_curve(self):
        store = ProgressStore(self.db)
        observed = self.play(5)
        accepted = self.old_plays + [observed]
        with patch("progress_store.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            store.sync(self.profile, self.since, accepted, observed_id=observed["id"])
        assessment = engine.assess(accepted, now=self.now, since=self.since)
        snapshot = store.snapshot(self.profile, self.since, assessment)
        self.assertEqual(self.original_rows, self.old_columns()[:5])
        self.assertEqual(6, snapshot["tracked_plays"])
        self.assertEqual([1] * 5 + [2], [point["method_version"] for point in snapshot["history"]])
        latest = snapshot["history"][-1]
        self.assertEqual(self.now.isoformat(), latest["played_at"])
        self.assertEqual("recorded", latest["source"])
        self.assertEqual(latest["reference"], snapshot["initial_reference"])
        self.assertEqual(latest["reference"], snapshot["best_reference"])
        self.assertEqual(0, snapshot["change"])
        self.assertEqual(4.5, snapshot["rank"]["stars"])
        store.sync(self.profile, self.since, accepted, observed_id=observed["id"])
        reopened = ProgressStore(self.db)
        reopened.sync(self.profile, self.since, accepted)
        self.assertEqual(snapshot, reopened.snapshot(self.profile, self.since, assessment))

    def test_expired_confirmation_records_current_reference_without_granting_rank(self):
        store = ProgressStore(self.db)
        accepted = self.old_plays + [self.play(index, days=10, stars=5) for index in range(5, 8)]
        expired = self.play(8, days=31, stars=5)
        with patch("progress_store.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            store.sync(self.profile, self.since, accepted + [expired], observed_id=expired["id"])
        snapshot = store.snapshot(self.profile, self.since,
                                  engine.assess(accepted + [expired], now=self.now, since=self.since))
        self.assertEqual(6, snapshot["tracked_plays"])
        self.assertEqual(4.5, snapshot["rank"]["stars"])
        self.assertEqual(self.now.isoformat(), snapshot["history"][-1]["played_at"])
        self.assertEqual(2, snapshot["history"][-1]["method_version"])

    def test_current_delayed_confirmation_can_use_other_accepted_maps_in_thirty_days(self):
        store = ProgressStore(self.db)
        known = self.old_plays + [self.play(index, days=10, stars=5) for index in (5, 6)]
        delayed = self.play(7, days=12, stars=5)
        with patch("progress_store.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            store.sync(self.profile, self.since, known + [delayed], observed_id=delayed["id"])
        snapshot = store.snapshot(self.profile, self.since,
                                  engine.assess(known + [delayed], now=self.now, since=self.since))
        self.assertEqual(5, snapshot["rank"]["stars"])
        self.assertEqual(self.now.isoformat(), snapshot["rank"]["earned_at"])
        self.assertEqual(self.original_rows, self.old_columns()[:5])
        self.assertEqual(6, snapshot["tracked_plays"])

    def test_initial_untracked_import_evaluates_at_historical_dates_not_today(self):
        store = ProgressStore(self.db)
        untracked = "another-untracked-profile"
        plays = [self.play(index, days=40 - index * 2) for index in range(5)]
        with patch("progress_store.assess", wraps=engine.assess) as evaluate:
            store.sync(untracked, self.since, plays)
        self.assertEqual(5, evaluate.call_count)
        self.assertEqual([engine.timestamp(play["played_at"]) for play in plays],
                         [call.kwargs["now"] for call in evaluate.call_args_list])
        current = engine.assess(plays, now=self.now, since=self.since)
        snapshot = store.snapshot(untracked, self.since, current)
        self.assertEqual("learning", snapshot["status"])
        self.assertEqual(4.5, snapshot["rank"]["stars"])
        self.assertTrue(all(point["source"] == "reconstructed" and point["method_version"] == 2
                            for point in snapshot["history"]))
        self.assertEqual([play["played_at"] for play in plays], [point["played_at"] for point in snapshot["history"]])


if __name__ == "__main__":
    unittest.main()
