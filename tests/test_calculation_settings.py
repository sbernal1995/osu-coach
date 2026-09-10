"""Behavioral checks for configurable calculations and immutable progress evidence."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
import unittest

import engine
from evidence import recency_weight, session_count, reference_settings_signature
from expectations import expectation_for
from player_profile import build_player_profile
from progress_rules import rank_evidence
from progress_store import ProgressStore
from settings import settings_context, validate_settings
from tag_analysis import analyze_tags


NOW = datetime.now(timezone.utc)


def play(index, **changes):
    result = {"id": f"settings-{index}", "beatmap_key": f"map-{index}", "beatmap_id": 100 + index,
              "played_at": (NOW - timedelta(minutes=180 - index)).isoformat(),
              "mode": 0, "client": "lazer", "mods": [], "stars": 4.5,
              "accuracy": 98, "misses": 1, "judged_objects": 400, "object_count": 400,
              "max_combo": 450, "map_max_combo": 500, "passed": True, "completion": 1,
              "bpm": 150, "ar": 8, "length": 120}
    result.update(changes)
    return result


def candidate(index, **changes):
    result = {"key": f"candidate-{index}", "id": 1000 + index, "set_id": 2000 + index,
              "title": f"Candidate {index}", "artist": "Fixture", "version": "Insane", "mode": 0,
              "stars": 4.5, "bpm": 150, "ar": 8, "length": 120, "object_count": 400,
              "max_combo": 500, "source": "local", "tags": []}
    result.update(changes)
    return result


class ConfigurableCalculationTests(unittest.TestCase):
    def test_windows_calibration_and_initial_reference_resolve_each_context(self):
        rows = [play(i, played_at=(NOW - timedelta(days=12-i)).isoformat()) for i in range(12)]
        original = engine.assess(rows, NOW)
        custom = validate_settings({"reference_plays": 8, "reference_days": 10, "session_plays": 3,
                                    "session_days": 2, "calibration_plays": 8, "calibration_maps": 8,
                                    "initial_stars": 3.75})
        with settings_context(custom):
            limited = engine.assess(rows, NOW)
            self.assertEqual((limited["attempts"], limited["session"]["attempts"]), (8, 2))
            self.assertEqual((limited["window_plays"], limited["window_days"]), (8, 10))
            self.assertEqual(engine.assess(rows[-5:], NOW)["phase"], "calibrating")
            self.assertEqual(engine.assess([], NOW)["baseline"], 3.75)
            self.assertIn("8 partidas", limited["reference_method"])
        self.assertEqual(engine.assess(rows, NOW), original)
        self.assertEqual(reference_settings_signature(), "default-v2")

    def test_recency_and_trim_settings_change_measured_level_without_touching_input(self):
        rows = [play(i, stars=6 if i < 5 else 4, played_at=(NOW-timedelta(days=20 if i < 5 else 1)).isoformat())
                for i in range(10)]
        before = deepcopy(rows)
        default = engine.assess(rows, NOW)["baseline"]
        with settings_context({"half_life_days": 100, "trim_percent": 0}):
            self.assertGreater(engine.assess(rows, NOW)["baseline"], default + .5)
            self.assertAlmostEqual(recency_weight(play(1, played_at=(NOW-timedelta(days=100)).isoformat()), NOW), .5)
        self.assertEqual(rows, before)

    def test_attempt_cap_changes_repeated_map_evidence(self):
        rows = [play(0, beatmap_key="repeat", stars=2), play(1, beatmap_key="repeat", stars=6),
                play(2, stars=4), play(3, stars=4), play(4, stars=4)]
        with settings_context({"trim_percent": 0, "max_attempts_per_map": 1}):
            one = engine.assess(rows, NOW)["baseline"]
        with settings_context({"trim_percent": 0, "max_attempts_per_map": 2}):
            two = engine.assess(rows, NOW)["baseline"]
        self.assertGreater(one, two + .4)

    def test_profile_and_tags_use_same_custom_minima_gap_band_and_strength(self):
        rows = [play(i, accuracy=95, played_at=(NOW-timedelta(minutes=80-i*20)).isoformat()) for i in range(4)]
        profile = {"window": rows, "session_window": rows, "baseline": 4.5,
                   "evaluated_at": NOW.isoformat(), "phase": "training", "challenge_unlocked": True}
        maps = [candidate(i, key=f"map-{i}", tags=[{"name": "skillset/streams", "source": "community"}]) for i in range(4)]
        self.assertEqual(build_player_profile(profile)["status"], "learning")
        self.assertEqual(session_count(rows), 1)
        custom = {"profile_min_plays": 4, "profile_min_maps": 4, "profile_min_sessions": 2,
                  "session_gap_minutes": 15, "strong_accuracy": 94, "comparable_star_band": .2}
        with settings_context(validate_settings(custom)):
            player = build_player_profile(profile)
            tags = analyze_tags(rows, maps, 4.5, now=NOW)
            self.assertEqual(player["status"], "ready")
            self.assertEqual(next(x for x in player["dimensions"] if x["key"] == "accuracy")["status"], "strength")
            self.assertEqual(tags["items"][0]["status"], "strength")
            self.assertEqual(tags["evidence"]["min_plays"], 4)
            self.assertEqual(player["evidence"]["min_maps"], 4)
            self.assertEqual(player["evidence"]["comparable_sessions"], 4)
            self.assertEqual(tags["items"][0]["comparable_sessions"], 4)
            far = deepcopy(rows)
            for row in far:
                row["stars"] = 4.8
            self.assertEqual(build_player_profile({**profile, "window": far})["status"], "learning")
            self.assertEqual(analyze_tags(far, maps, 4.5, now=NOW)["items"][0]["status"], "learning")

    def test_physical_limits_and_stage_targets_follow_configuration(self):
        profile = engine.assess([play(i) for i in range(5)], NOW)
        with settings_context({"warmup_offset": .7, "challenge_increment": .4,
                               "bpm_margin": 30, "ar_margin": 1.2,
                               "length_multiplier": 1.5, "length_extra_seconds": 40}):
            self.assertEqual(engine.physical_limits(profile), {"bpm": 180, "ar": 9.2, "length": 220})
            groups = engine.recommend([], profile)
            self.assertAlmostEqual(groups[0]["target"], profile["baseline"]-.7)
            self.assertAlmostEqual(groups[-1]["target"], profile["baseline"]+.4)

    def test_star_tolerance_and_challenge_gate_are_configurable(self):
        rows = [play(i, accuracy=95, misses=4) for i in range(5)]
        self.assertTrue(engine.assess(rows, NOW)["challenge_unlocked"])
        with settings_context({"challenge_accuracy": 96, "challenge_maps": 4}):
            self.assertFalse(engine.assess(rows, NOW)["challenge_unlocked"])
        with settings_context({"challenge_accuracy": 95, "challenge_miss_percent": .5}):
            self.assertFalse(engine.assess(rows, NOW)["challenge_unlocked"])
        profile = {**engine.assess(rows, NOW), "baseline": 4.5}
        maps = [candidate(1, stars=4.9)]
        self.assertEqual(engine.recommend(maps, profile, stages={"practice"})[0]["maps"], [])
        with settings_context({"star_tolerance_above": .5}):
            self.assertEqual(len(engine.recommend(maps, profile, stages={"practice"})[0]["maps"]), 1)

    def test_recovery_drop_changes_only_current_practice_target(self):
        profile = engine.assess([play(i, passed=False if i >= 3 else True, accuracy=85 if i >= 3 else 98) for i in range(5)], NOW)
        with settings_context({"recovery_drop": .6}):
            current = engine.assess(profile["window"], NOW)
            updated = engine.apply_player_profile(current, {"progression": {"mode": "advance", "allow_challenge": True}})
            self.assertEqual(updated["session"]["adjustment"], -.6)
            self.assertEqual(updated["baseline"], profile["baseline"])
            self.assertEqual(updated["training_adjustments"]["practice_offset"], -.6)

    def test_expectations_use_custom_evidence_band_and_provisional_strength(self):
        profile = {"window": [], "baseline": 4.5, "phase": "training"}
        default = expectation_for(candidate(1), profile, "practice")
        with settings_context({"strong_accuracy": 94, "strong_combo_percent": 60, "comparable_star_band": .2}):
            custom = expectation_for(candidate(1), profile, "practice")
            self.assertLess(custom["accuracy_min"], default["accuracy_min"])
            self.assertLess(custom["combo_min"], default["combo_min"])
            self.assertEqual(custom["evidence_star_band"], {"min": 4.3, "max": 4.7})

    def test_rank_grid_and_requirements_keep_next_rung_above_existing_rank(self):
        profile = {"phase": "training", "baseline": 4.7,
                   "window": [play(i, accuracy=96, misses=3, max_combo=350, stars=4.9) for i in range(2)]}
        self.assertIsNone(rank_evidence(profile)["candidate_stars"])
        with settings_context({"rank_step": .3, "rank_required_maps": 2, "strong_accuracy": 95,
                               "strong_miss_percent": 1, "strong_combo_percent": 70}):
            result = rank_evidence(profile, earned_stars=4.75)
            self.assertEqual(result["next_stars"], 5)
            self.assertEqual(result["candidate_stars"], 4.7)
            self.assertEqual(result["required_maps"], 2)
            self.assertIn("95 %", " ".join(result["requirements"]))
            self.assertIsNone(rank_evidence(profile, earned_stars=10.4)["next_stars"])


class ConfigurableProgressTests(unittest.TestCase):
    def test_settings_change_never_awards_rank_or_rewrites_curve_and_new_points_start_own_comparison(self):
        since = (NOW-timedelta(days=1)).isoformat()
        rows = [play(i, accuracy=96) for i in range(5)]
        with closing(sqlite3.connect(":memory:")) as db:
            store = ProgressStore(db)
            store.sync("player", since, rows)
            default = store.snapshot("player", since, engine.assess(rows, NOW))
            frozen = db.execute("SELECT * FROM coach_progress_points ORDER BY play_id").fetchall()
            self.assertIsNone(default["rank"]["stars"])
            with settings_context({"strong_accuracy": 95}):
                store.sync("player", since, rows)
                changed = store.snapshot("player", since, engine.assess(rows, NOW))
                self.assertEqual(db.execute("SELECT * FROM coach_progress_points ORDER BY play_id").fetchall(), frozen)
                self.assertIsNone(changed["rank"]["stars"])
                self.assertIsNone(changed["initial_reference"])
                self.assertIsNone(changed["change"])
                self.assertTrue(changed["has_other_settings"])
                rows.append(play(5, accuracy=96))
                store.sync("player", since, rows, observed_id=rows[-1]["id"])
                observed = store.snapshot("player", since, engine.assess(rows, NOW))
                self.assertEqual(observed["tracked_plays"], 6)
                self.assertEqual(observed["rank"]["stars"], 4.5)
                self.assertEqual(observed["initial_reference"], observed["history"][-1]["reference"])
                self.assertEqual(observed["change"], 0)
                self.assertEqual(db.execute("SELECT * FROM coach_progress_points WHERE play_id<>? ORDER BY play_id", (rows[-1]["id"],)).fetchall(), frozen)
                reopened = ProgressStore(db).snapshot("player", since, engine.assess(rows, NOW))
                self.assertEqual(reopened, observed)
            restored = store.snapshot("player", since, engine.assess(rows, NOW))
            self.assertEqual(restored["initial_reference"], default["initial_reference"])
            self.assertEqual(restored["tracked_plays"], 6)
            self.assertEqual(restored["rank"]["stars"], 4.5)

    def test_legacy_default_signature_and_changed_rank_rules_preserve_evidence(self):
        since = (NOW-timedelta(days=1)).isoformat()
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("""CREATE TABLE coach_progress_points (profile TEXT, epoch TEXT, play_id TEXT,
                played_at TEXT, reference REAL, calibrated INTEGER, source TEXT, method_version INTEGER,
                PRIMARY KEY (profile, epoch, play_id))""")
            db.execute("INSERT INTO coach_progress_points VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("player", since, "old", NOW.isoformat(), 4.4, 1, "recorded", 2))
            store = ProgressStore(db)
            assessment = engine.assess([play(i) for i in range(5)], NOW)
            default = store.snapshot("player", since, assessment)
            self.assertEqual(default["initial_reference"], 4.4)
            self.assertEqual(default["history"][0]["settings_signature"], "default-v2")
            with settings_context({"rank_step": .5, "rank_required_maps": 1}):
                store.sync("player", since, assessment["window"])
                current = store.snapshot("player", since, assessment)
                self.assertEqual(current["initial_reference"], 4.4)
                self.assertIsNone(current["rank"]["stars"])
                self.assertEqual(current["tracked_plays"], 1)
            self.assertIsNone(db.execute("SELECT settings_signature FROM coach_progress_points").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
