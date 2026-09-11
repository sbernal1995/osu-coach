"""Duration/mod policy, native variants, and persistent mission attribution."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from osu_coach import app
from osu_coach.core import engine, mod_policy
from osu_coach.core.quest_rules import evaluate_attempt
from osu_coach.demo import demo_data
from osu_coach.integrations.lazer_calculator import CALCULATOR_ID
from osu_coach.settings import settings_context, validate_settings
from osu_coach.storage.variant_store import VariantStore
from tests.test_catalog import MAP
from tests.test_engine import NOW, play, beatmap


class RecommendationPolicyTests(unittest.TestCase):
    def test_duration_is_inclusive_optional_and_rejects_unknown_when_limited(self):
        with settings_context({"recommendation_min_seconds": 90, "recommendation_max_seconds": 240}):
            for length, allowed in ((89, False), (90, True), (240, True), (241, False), (None, False)):
                self.assertEqual(allowed, mod_policy.duration_ok({"length": length}))
        self.assertTrue(mod_policy.duration_ok({"length": 9000}))

    def test_validation_rejects_inverted_ranges_and_unknown_mods(self):
        for changes in ({"recommendation_min_seconds": 301, "recommendation_max_seconds": 300},
                        {"recommendation_min_seconds": -1}, {"recommendation_max_seconds": "120"},
                        {"recommendation_mods": "RX"}, {"recommendation_mods": ["HD"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_settings(changes)
        self.assertEqual(300, validate_settings({"recommendation_min_seconds": 300})["recommendation_min_seconds"])

    def test_all_stages_and_sources_apply_duration(self):
        profile = engine.assess([play(i) for i in range(8)], NOW)
        with settings_context({"recommendation_min_seconds": 90, "recommendation_max_seconds": 240}):
            for stage in ("warmup", "practice", "challenge"):
                target = engine.recommend([], profile, stages={stage})[0]["target"]
                for source in ("local", "online"):
                    maps = [beatmap(i, stars=target, source=source, length=length)
                            for i, length in enumerate((30, 90, 240, 600))]
                    selected = engine.recommend(maps, profile, stages={stage}, fill_online=True)[0]["maps"]
                    self.assertEqual([90, 240], sorted(m["length"] for m in selected))

    def test_force_and_free_have_explicit_conditions(self):
        with settings_context({"recommendation_mods": "HDDT"}):
            choices = mod_policy.options()
            self.assertEqual(["DT", "HD"], [m["acronym"] for m in choices[0]["mods"]])
            self.assertEqual(1.5, choices[0]["rate"])
            self.assertFalse(mod_policy.preference_ok({"length": 120}))
        with settings_context({"recommendation_mods": "free"}):
            self.assertEqual(8, len(mod_policy.options()))
            self.assertIn(mod_policy.context(), mod_policy.options())

    def test_exact_mod_settings_and_rate_are_required(self):
        expected = mod_policy.context("HDDT")
        valid = {"client": "lazer", "mods": [{"acronym": "HD"}, {"acronym": "DT", "settings": {"speed_change": 1.5}}],
                 "mod_key": '{"rate":1.5}'}
        self.assertTrue(mod_policy.conditions_match(expected, valid))
        for changed in ({"mods": []}, {"client": "stable"}, {"mod_key": '{"rate":1.25}'}):
            self.assertFalse(mod_policy.conditions_match(expected, {**valid, **changed}))

    def test_free_does_not_repeat_same_difficulty_with_multiple_mods(self):
        profile = engine.assess([play(i) for i in range(8)], NOW)
        rows = [mod_policy.stamp(beatmap(200, stars=profile["baseline"]), value) for value in
                (mod_policy.context(), mod_policy.context("HD"), mod_policy.context("HR"))]
        selected = [m for group in engine.recommend(rows, profile) for m in group["maps"]]
        self.assertEqual(1, len(selected))

    def test_mod_discovery_broadens_only_prefilter_not_final_eligibility(self):
        requirements = [{"min_stars": 4, "max_stars": 4.5, "min_length": 100, "max_length": 200, "max_ar": 9}]
        with settings_context({"recommendation_mods": "DT"}):
            broad = mod_policy.public_requirements(requirements)
            self.assertLess(broad[0]["min_stars"], 4)
            self.assertIsNone(broad[0]["max_length"])
        self.assertEqual(100, requirements[0]["min_length"])


class VariantCalculationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stop = threading.Event()
        self.store = VariantStore(self.root, self.stop)
        path = self.root / "test.osu"
        path.write_text(MAP, encoding="utf-8")
        self.map = {"key": "fixture", "id": 123, "path": str(path), "stars": 1.54, "length": 6, "source": "local"}

    def tearDown(self):
        self.stop.set()
        self.store.close()
        self.temp.cleanup()

    def wait(self):
        self.store.thread.join(8)
        self.assertFalse(self.store.thread.is_alive())

    def test_native_stars_and_duration_are_recalculated_and_cached(self):
        choices = [mod_policy.context("DT"), mod_policy.context("HT")]
        result, _ = self.store.variants([self.map], choices)
        self.assertEqual([], result)
        self.wait()
        result, warning = self.store.variants([self.map], choices)
        self.assertEqual("", warning)
        faster, slower = result
        self.assertGreater(faster["stars"], slower["stars"])
        self.assertLess(faster["length"], slower["length"])
        self.assertEqual(180, faster["bpm"])
        self.assertEqual(90, slower["bpm"])
        with settings_context({"recommendation_min_seconds": slower["length"]}):
            self.assertFalse(mod_policy.duration_ok(faster))
            self.assertTrue(mod_policy.duration_ok(slower))
        reopened = VariantStore(self.root, self.stop, calculate=Mock(side_effect=AssertionError("cache must be reused")))
        self.assertEqual(result, reopened.variants([self.map], choices)[0])

    def test_failed_calculation_never_relabels_nomod_stars_as_dt(self):
        self.store.calculate = Mock(side_effect=RuntimeError("not available"))
        self.store.variants([self.map], [mod_policy.context("DT")])
        self.wait()
        result, warning = self.store.variants([self.map], [mod_policy.context("DT")])
        self.assertEqual([], result)
        self.assertIn("no se pudieron calcular", warning)

    def test_new_checksum_and_calculator_version_invalidate_cache(self):
        choice = mod_policy.context("HD")
        original = self.store.key(self.map, choice)
        self.assertNotEqual(original, self.store.key({**self.map, "key": "changed"}, choice))
        self.store.path.write_text(json.dumps({"calculator": "old", "entries": {"bad": {}}}), encoding="utf-8")
        self.assertEqual({}, VariantStore(self.root, self.stop).cache)

    def test_downloaded_definition_is_calculated_and_shared_by_mods(self):
        remote = {**self.map, "source": "online"}
        remote.pop("path")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = MAP.encode()
        opener = Mock()
        opener.open.return_value = response
        with patch("osu_coach.storage.variant_store.build_opener", return_value=opener):
            hd = self.store._calculate(remote, mod_policy.context("HD"))
            hr = self.store._calculate(remote, mod_policy.context("HR"))
        self.assertEqual(1, opener.open.call_count)
        self.assertEqual("https://osu.ppy.sh/osu/123", opener.open.call_args.args[0].full_url)
        self.assertEqual(CALCULATOR_ID, hd["calculator"])
        self.assertEqual(CALCULATOR_ID, hr["calculator"])
        self.assertTrue((self.root/"map-files/123.osu").is_file())

    def test_failed_file_download_is_not_repeated_for_each_mod(self):
        remote = {**self.map, "source": "online"}
        remote.pop("path")
        opener = Mock()
        opener.open.side_effect = OSError("unavailable")
        with patch("osu_coach.storage.variant_store.build_opener", return_value=opener):
            for mod in ("HD", "HR"):
                with self.assertRaises((OSError, ValueError)):
                    self.store._calculate(remote, mod_policy.context(mod))
        self.assertEqual(1, opener.open.call_count)


class ModMissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None, no_tosu=True,
                                       tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = app.Coach(self.args)
        maps, samples = demo_data()
        self.coach.catalog = maps
        self.samples = samples
        self.coach.config["since"] = samples[0]["played_at"]
        for sample in samples:
            self.coach.add_play(deepcopy(sample))
        self.owner = self.coach.active
        self.board = self.coach.state()["quest_board"]
        self.quest = self.board["groups"][0]["quests"][0]
        self.quest["created_at"] = (datetime.now(timezone.utc)-timedelta(seconds=4)).isoformat()
        self.quest["map"].update(play_conditions=mod_policy.context("HD"), mods_label="HD")
        with self.coach.db:
            self.coach.db.execute("UPDATE quest_boards SET data=? WHERE id=?",
                                 (json.dumps(self.board), self.board["id"]))

    def tearDown(self):
        self.coach.close()
        self.coach.db.close()
        self.temp.cleanup()

    def result(self, **changes):
        m = self.quest["map"]
        p = {**self.samples[-1], "id": "new-mod-score", "played_at": datetime.now(timezone.utc).isoformat(),
             "started_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
             "beatmap_key": m["key"], "beatmap_id": m["id"], "mods": [{"acronym": "HD"}],
             "mod_key": '{"mods":[{"acronym":"HD"}],"rate":1}', "accuracy": 100, "misses": 0,
             "grade": "SSH", "passed": True, "completion": 1, "stars": m["stars"],
             "max_combo": m["max_combo"], "map_max_combo": m["max_combo"]}
        p.update(changes)
        return p

    def test_prescribed_hd_completes_original_mission_and_counts_in_same_progress(self):
        before = len(self.coach.training_plays())
        self.coach.add_play(self.result())
        self.assertEqual(self.owner, self.coach.active)
        self.assertEqual(before+1, len(self.coach.training_plays()))
        row = self.coach.db.execute("SELECT data FROM quest_completions WHERE quest_id=?", (self.quest["id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(self.quest["map"]["expectation"], json.loads(row[0])["map"]["expectation"])
        recorded = self.coach.training_plays()[0]
        self.assertEqual([{"acronym": "HD"}], recorded["mods"])
        self.assertEqual(self.owner, recorded["coach_profile"])
        with patch.object(self.coach.discovery_store, "sync") as discovery:
            self.coach.sync_discovery(force=True)
        self.assertEqual([], discovery.call_args.args[1]["mods"])

    def test_wrong_mod_cannot_complete_or_join_training_profile(self):
        result = self.result(mods=[], mod_key='{"mods":[],"rate":1}')
        evaluation = evaluate_attempt(self.quest, result)
        self.assertFalse(evaluation["completed"])
        self.assertEqual("unmet", evaluation["checks"][0]["status"])
        self.assertEqual("Sin mods", evaluation["checks"][0]["actual"])
        other = self.result(player="Other")
        self.coach.associate_mission(other)
        self.assertNotIn("coach_profile", other)

    def test_pending_confirmation_keeps_quest_owner_and_only_counts_after_acceptance(self):
        self.coach.add_play(self.result(needs_confirmation=True))
        self.assertIsNone(self.coach.db.execute("SELECT data FROM quest_completions WHERE quest_id=?", (self.quest["id"],)).fetchone())
        self.coach.confirm("new-mod-score", True)
        self.assertEqual(self.owner, self.coach.active)
        self.assertIsNotNone(self.coach.db.execute("SELECT data FROM quest_completions WHERE quest_id=?", (self.quest["id"],)).fetchone())

    def test_recommendation_filters_never_rewrite_existing_attempts_or_goals(self):
        self.coach.add_play(self.result(accuracy=80, grade="B", misses=20))
        before = self.coach.quest_store.current(app.scope_key(self.owner, self.coach.config["since"]))
        q = next(q for g in before["groups"] for q in g["quests"] if q["id"] == self.quest["id"])
        self.coach.update_settings({"recommendation_max_seconds": 1, "recommendation_mods": "NM"})
        after = self.coach.state()["quest_board"]
        self.assertEqual(q, next(item for g in after["groups"] for item in g["quests"] if item["id"] == q["id"]))

    def test_force_setting_retires_only_incompatible_pending_missions_and_survives_restart(self):
        self.coach.variants.calculate = Mock(side_effect=lambda m, c: {
            "stars": m["stars"], "length": m["length"], "calculator": CALCULATOR_ID})
        for m in self.coach.catalog:
            m["path"] = "synthetic-path"
        before_plays = self.coach.db.execute("SELECT * FROM plays").fetchall()
        self.coach.update_settings({"recommendation_mods": "HD"})
        self.coach.state()
        self.coach.variants.thread.join(4)
        state = self.coach.state()
        quests = [q for g in state["quest_board"]["groups"] for q in g["quests"] if q["status"] == "pending"]
        self.assertTrue(quests)
        self.assertTrue(all(q["map"]["mods_label"] == "HD" for q in quests))
        self.assertEqual(before_plays, self.coach.db.execute("SELECT * FROM plays").fetchall())
        saved = deepcopy(state["quest_board"])
        self.coach.close()
        self.coach.db.close()
        self.coach = app.Coach(self.args)
        self.assertEqual("HD", self.coach.settings["recommendation_mods"])
        self.assertEqual(saved, self.coach.state()["quest_board"])
