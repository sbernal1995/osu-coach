"""Persistent missions through Coach and HTTP, isolated from the live service."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from osu_coach import app
from osu_coach.demo import demo_data


class QuestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None,
                                       no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = app.Coach(self.args)
        self.maps, self.samples = demo_data()
        self.coach.catalog = deepcopy(self.maps)
        self.coach.config["since"] = self.samples[0]["played_at"]
        app.save_json(self.coach.config_path, self.coach.config)
        self.assigned_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        self.serial = 0

    def tearDown(self):
        self.coach.close()
        self.coach.db.close()
        self.temp.cleanup()

    def seed(self):
        for sample in self.samples:
            self.coach.add_play(deepcopy(sample))

    def initial_board(self):
        self.seed()
        with patch("osu_coach.storage.quest_store.utcnow", return_value=self.assigned_at.isoformat()):
            return self.coach.state()["quest_board"]

    @staticmethod
    def quests(board):
        return [quest for group in board["groups"] for quest in group["quests"]]

    def first_quest(self, board=None):
        return self.quests(board or self.coach.state()["quest_board"])[0]

    def current_quest(self, identifier):
        state = self.coach.state()
        quests = self.quests(state["quest_board"]) + state["quest_completions"]["items"]
        return next(q for q in quests if q["id"] == identifier)

    def attempt(self, quest, **changes):
        self.serial += 1
        beatmap = quest["map"]
        result = deepcopy(self.samples[-1])
        result.update(id=f"quest-attempt-{self.serial}", played_at=datetime.now(timezone.utc).isoformat(),
                      started_at=(self.assigned_at + timedelta(seconds=1)).isoformat(),
                      beatmap_key=beatmap["key"], beatmap_id=beatmap.get("id", 0),
                      title=beatmap.get("title"), artist=beatmap.get("artist"), version=beatmap.get("version"),
                      stars=beatmap["stars"], accuracy=100.0, misses=0,
                      max_combo=beatmap["max_combo"], map_max_combo=beatmap["max_combo"],
                      object_count=beatmap["object_count"], judged_objects=beatmap["object_count"],
                      grade="SS", passed=True, completion=1.0)
        result.update(changes)
        return result

    def test_unknown_player_does_not_get_an_assigned_board(self):
        state = self.coach.state()
        self.assertIsNone(state["quest_board"])
        self.assertEqual([], state["quest_history"])

    def test_initial_assignment_copies_recommended_goals_and_remains_stable(self):
        board = self.initial_board()
        self.assertEqual("active", board["status"])
        self.assertGreater(board["total_count"], 0)
        self.assertEqual(len(self.quests(board)), board["total_count"])
        self.assertEqual(0, board["completed_count"])
        self.assertFalse(board["all_completed"])
        self.assertTrue(all(q["status"] == "pending" and q["attempt_count"] == 0
                            and q["map"].get("expectation") for q in self.quests(board)))
        self.assertEqual(board, self.coach.state()["quest_board"])
        self.assertEqual(board, self.coach.state()["quest_board"])

    def test_assignment_is_frozen_when_catalog_tags_and_profile_change(self):
        board = self.initial_board()
        unrelated = self.attempt(self.first_quest(board), beatmap_key="unrelated-map", beatmap_id=987654,
                                 stars=5.5, accuracy=75, misses=30, grade="C")
        self.coach.add_play(unrelated)
        self.coach.catalog = []
        self.coach.tag_store.sets = {"1": {"fetched_at": datetime.now(timezone.utc).isoformat(),
                                            "maps": {"123": [{"name": "skillset/streams", "count": 50}]}}}
        self.assertEqual(board, self.coach.state()["quest_board"])

    def test_assigned_goals_and_completed_progress_survive_restart(self):
        board = self.initial_board()
        quest = self.first_quest(board)
        self.coach.add_play(self.attempt(quest))
        state = self.coach.state()
        expected, completions = state["quest_board"], state["quest_completions"]
        self.assertEqual("completed", self.current_quest(quest["id"])["status"])
        self.coach.close()
        self.coach.db.close()
        self.coach = app.Coach(self.args)
        self.coach.catalog = deepcopy(self.maps)
        restarted = self.coach.state()
        self.assertEqual(expected, restarted["quest_board"])
        self.assertEqual(completions, restarted["quest_completions"])

    def test_success_completes_once_and_later_failure_cannot_undo_it(self):
        board = self.initial_board()
        quest = self.first_quest(board)
        success = self.attempt(quest)
        self.coach.add_play(success)
        completed = self.current_quest(quest["id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual(success["id"], completed["completed_play_id"])
        self.assertTrue(completed["completed_at"])
        self.assertEqual(1, completed["attempt_count"])
        self.coach.add_play(deepcopy(success))
        self.assertEqual(completed, self.current_quest(quest["id"]))
        self.coach.add_play(self.attempt(quest, passed=False, completion=.2, grade="F"))
        after = self.current_quest(quest["id"])
        self.assertEqual("completed", after["status"])
        self.assertEqual(completed["completed_at"], after["completed_at"])
        self.assertEqual(success["id"], after["completed_play_id"])

    def test_distinct_partial_attempts_cannot_combine_into_a_completion(self):
        quest = self.first_quest(self.initial_board())
        goal = quest["map"]["expectation"]
        self.coach.add_play(self.attempt(quest, misses=goal["misses_max"] + 1))
        self.coach.add_play(self.attempt(quest, accuracy=goal["accuracy_min"] - 1))
        self.coach.add_play(self.attempt(quest, max_combo=goal["combo_min"] - 1))
        current = self.current_quest(quest["id"])
        self.assertEqual("in_progress", current["status"])
        self.assertEqual(3, current["attempt_count"])
        self.coach.add_play(self.attempt(quest))
        current = self.current_quest(quest["id"])
        self.assertEqual("completed", current["status"])
        self.assertEqual(4, current["attempt_count"])

    def test_explicit_low_grade_and_failed_or_partial_results_do_not_complete(self):
        quest = self.first_quest(self.initial_board())
        for fields in ({"grade": "D"}, {"passed": False, "grade": "F"}, {"completion": .5}):
            with self.subTest(fields=fields):
                self.coach.add_play(self.attempt(quest, **fields))
                self.assertNotEqual("completed", self.current_quest(quest["id"])["status"])
        self.assertEqual(3, self.current_quest(quest["id"])["attempt_count"])

    def test_pending_needs_acceptance_and_rejection_never_completes(self):
        quest = self.first_quest(self.initial_board())
        pending = self.attempt(quest, needs_confirmation=True)
        self.coach.add_play(pending)
        self.assertEqual("pending", self.current_quest(quest["id"])["status"])
        self.assertEqual(0, self.current_quest(quest["id"])["attempt_count"])
        self.coach.confirm(pending["id"], False)
        self.assertEqual(0, self.current_quest(quest["id"])["attempt_count"])
        accepted = self.attempt(quest, needs_confirmation=True)
        self.coach.add_play(accepted)
        self.coach.confirm(accepted["id"], True)
        self.assertEqual("completed", self.current_quest(quest["id"])["status"])
        self.assertEqual(1, self.current_quest(quest["id"])["attempt_count"])
        with self.assertRaises(ValueError):
            self.coach.confirm(accepted["id"], True)

    def assert_skipped_without_attempt_or_achievement(self, before, quest, state):
        after = state["quest_board"]
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(0, state["quest_completions"]["total"])
        self.assertEqual(0, after["completed_count"])
        self.assertEqual(1, state["quest_skips"]["total"])
        skipped = state["quest_skips"]["items"][0]
        self.assertEqual(quest["id"], skipped["id"])
        self.assertEqual("skipped", skipped["status"])
        self.assertEqual("played_before_assignment", skipped["skipped_reason"])
        self.assertEqual(quest["map"], skipped["map"])
        self.assertEqual(0, skipped["attempt_count"])
        self.assertIsNone(skipped["last_attempt"])
        self.assertIsNone(skipped["completed_play_id"])
        self.assertEqual(0, self.coach.db.execute(
            "SELECT COUNT(*) FROM quest_attempts WHERE quest_id=?", (quest["id"],)).fetchone()[0])
        current = {item["id"]: item for item in self.quests(after)}
        if quest["id"] in current:
            self.assertEqual("skipped", current[quest["id"]]["status"])
        for other in self.quests(before):
            if other["id"] != quest["id"]:
                self.assertEqual(other, current[other["id"]])

    def test_scores_finished_before_assignment_are_skipped_without_counting(self):
        before = self.initial_board()
        quest = self.first_quest(before)
        earlier = (self.assigned_at - timedelta(seconds=1)).isoformat()
        self.coach.add_play(self.attempt(quest, played_at=earlier, started_at=earlier))
        self.assert_skipped_without_attempt_or_achievement(before, quest, self.coach.state())

    def test_scores_started_before_assignment_do_not_count(self):
        before = self.initial_board()
        quest = self.first_quest(before)
        earlier = (self.assigned_at - timedelta(seconds=1)).isoformat()
        self.coach.add_play(self.attempt(quest, started_at=earlier))
        state = self.coach.state()
        self.assertEqual(before, state["quest_board"])
        self.assertEqual(0, state["quest_completions"]["total"])
        self.assertEqual(0, state["quest_skips"]["total"])
        self.assertEqual(0, self.coach.db.execute(
            "SELECT COUNT(*) FROM quest_attempts WHERE quest_id=?", (quest["id"],)).fetchone()[0])

    def test_confirmed_scores_finished_before_assignment_are_skipped_without_counting(self):
        before = self.initial_board()
        quest = self.first_quest(before)
        earlier = (self.assigned_at - timedelta(seconds=1)).isoformat()
        pending = self.attempt(quest, played_at=earlier, started_at=earlier, needs_confirmation=True)
        self.coach.add_play(pending)
        self.assertEqual(before, self.coach.state()["quest_board"])
        self.coach.confirm(pending["id"], True)
        self.assert_skipped_without_attempt_or_achievement(before, quest, self.coach.state())

    def test_profile_and_map_mismatches_do_not_update_original_board(self):
        board = self.initial_board()
        active = self.coach.active
        quest = self.first_quest(board)
        for fields in ({"player": "Another player"}, {"client": "stable"},
                       {"mod_key": '{"mods":[{"acronym":"HD"}],"rate":1.0}', "mods": [{"acronym": "HD"}]},
                       {"beatmap_key": "different-map", "beatmap_id": 7654321}):
            with self.subTest(fields=fields):
                self.coach.add_play(self.attempt(quest, **fields))
                self.coach.active = active
                self.assertEqual(board, self.coach.state()["quest_board"])

    def test_every_goal_is_logged_and_slots_refill_without_replacing_board(self):
        board = self.initial_board()
        originals = self.quests(board)
        original_ids = {quest["id"] for quest in originals}
        for quest in originals:
            self.coach.add_play(self.attempt(quest))
        state = self.coach.state()
        current, completions = state["quest_board"], state["quest_completions"]
        self.assertEqual(board["id"], current["id"])
        self.assertEqual(original_ids, {quest["id"] for quest in completions["items"]})
        self.assertTrue(all(quest["status"] == "completed" for quest in completions["items"]))
        self.assertEqual(len(originals), completions["total"])
        self.assertEqual(len(originals), current["completed_count"])
        active = self.quests(current)
        replacements = [quest for quest in active if quest["id"] not in original_ids]
        self.assertEqual(len(originals), len(active))
        self.assertTrue(replacements)
        self.assertTrue(all(quest["status"] == "pending" and quest["attempt_count"] == 0
                            for quest in replacements))
        self.assertTrue(all(quest["status"] == "completed" for quest in active
                            if quest["id"] in original_ids))
        self.assertEqual(current["completed_count"] + len(replacements), current["total_count"])
        # Completed maps removed in one refresh can become suitable fallback
        # candidates for another waiting slot on the next refresh. Pending
        # assignments stay frozen throughout, and victories are never counted
        # again just because a slot is renewed.
        refreshed_state = self.coach.state()
        refreshed = refreshed_state["quest_board"]
        self.assertEqual(current["id"], refreshed["id"])
        self.assertEqual(current["profile_label"], refreshed["profile_label"])
        self.assertEqual(completions, refreshed_state["quest_completions"])
        self.assertEqual(current["completed_count"], refreshed["completed_count"])
        self.assertLessEqual(refreshed["waiting_count"], current["waiting_count"])
        for before_group, after_group in zip(current["groups"], refreshed["groups"]):
            self.assertEqual(before_group["stage"], after_group["stage"])
            self.assertEqual(len(before_group["quests"]), len(after_group["quests"]))
            for before_quest, after_quest in zip(before_group["quests"], after_group["quests"]):
                if before_quest["status"] != "completed" or before_quest["id"] == after_quest["id"]:
                    self.assertEqual(before_quest, after_quest)
                else:
                    self.assertEqual("pending", after_quest["status"])
                    self.assertEqual(0, after_quest["attempt_count"])
                    self.assertEqual(before_quest["id"], after_quest["replaces_quest_id"])
        if refreshed["waiting_count"] == 0:
            self.assertEqual(refreshed, self.coach.state()["quest_board"])

    def test_new_assignment_archives_old_board_and_rejects_stale_identifiers(self):
        board = self.initial_board()
        self.coach.new_quests(board["id"])
        state = self.coach.state()
        new = state["quest_board"]
        self.assertNotEqual(board["id"], new["id"])
        self.assertEqual(0, new["completed_count"])
        self.assertIn(board["id"], [item["id"] for item in state["quest_history"]])
        with self.assertRaises(ValueError):
            self.coach.new_quests(board["id"])
        self.assertEqual(new, self.coach.state()["quest_board"])

    def test_no_candidates_cannot_archive_a_usable_assignment(self):
        board = self.initial_board()
        self.coach.catalog = []
        with patch.object(self.coach.discovery_store, "candidates", return_value=[]):
            with self.assertRaises(ValueError):
                self.coach.new_quests(board["id"])
            state = self.coach.state()
        self.assertEqual(board, state["quest_board"])
        self.assertEqual([], state["quest_history"])

    def test_confirming_an_attempt_from_previous_board_cannot_complete_new_goals(self):
        board = self.initial_board()
        original = self.first_quest(board)
        pending = self.attempt(original, needs_confirmation=True)
        self.coach.add_play(pending)
        self.coach.new_quests(board["id"])
        new = self.coach.state()["quest_board"]
        reassigned = next(quest for quest in self.quests(new) if quest["map"]["key"] == original["map"]["key"])
        self.assertNotEqual(original["id"], reassigned["id"])
        self.assertLess(datetime.fromisoformat(pending["played_at"]), datetime.fromisoformat(reassigned["created_at"]))
        archived = self.coach.db.execute("SELECT data FROM quest_boards WHERE id=?", (board["id"],)).fetchone()[0]
        self.coach.confirm(pending["id"], True)
        self.assert_skipped_without_attempt_or_achievement(new, reassigned, self.coach.state())
        self.assertEqual(archived, self.coach.db.execute(
            "SELECT data FROM quest_boards WHERE id=?", (board["id"],)).fetchone()[0])

    def test_a_remote_goal_can_be_completed_after_downloading_that_exact_difficulty(self):
        for index, beatmap in enumerate(self.coach.catalog):
            beatmap.update(id=10000 + index, key=f"remote:{10000 + index}", source="discovery")
        board = self.initial_board()
        quest = self.first_quest(board)
        downloaded = deepcopy(quest["map"])
        downloaded.update(key="a" * 64, source="local")
        self.coach.catalog = [downloaded]
        self.coach.add_play(self.attempt(quest, beatmap_key=downloaded["key"]))
        self.assertEqual("completed", self.current_quest(quest["id"])["status"])

    def test_remote_availability_updates_after_scan_without_changing_assigned_board(self):
        discovered = deepcopy(self.maps)
        for index, beatmap in enumerate(discovered):
            beatmap.update(id=10000 + index, key=f"remote:{10000 + index}", source="online", local=False)
            beatmap["popularity"] = {"scope": "beatmapset", "rating": 9.0, "rating_votes": 25, "votes": 25, "play_count": 25000}
        self.coach.discovery_store.maps = discovered
        self.coach.catalog = []
        board = self.initial_board()
        quest = self.first_quest(board)
        before = self.coach.state()
        self.assertFalse(before["quest_availability"][quest["id"]]["installed"])
        downloaded = deepcopy(quest["map"])
        downloaded.update(key="a" * 32, source="local", local=True)
        self.coach.catalog = [downloaded]
        after = self.coach.state()
        self.assertTrue(after["quest_availability"][quest["id"]]["installed"])
        self.assertEqual(board, after["quest_board"])
        self.assertEqual(0, self.first_quest(after["quest_board"])["attempt_count"])

    def test_legacy_search_updates_from_catalog_without_rewriting_mission_or_results(self):
        board = self.initial_board()
        quest = self.first_quest(board)
        self.coach.add_play(self.attempt(quest, accuracy=80, misses=20, grade="B"))
        board = self.coach.state()["quest_board"]
        quest = self.first_quest(board)
        unicode_title = "春待ちクローバー (Swing Arrangement) [Dictate Edit]"
        romanized = "Harumachi Clover (Swing Arrangement) [Dictate Edit]"
        legacy_fields = {"id": 1762724, "set_id": 842412, "title": unicode_title,
                         "artist": "Will Stetson", "creator": "Sotarks", "version": "Expert"}
        quest["map"].update(legacy_fields, search_text=unicode_title + " Expert")
        quest["map"].pop("title_romanized", None)
        quest["map"].pop("artist_romanized", None)
        local = next(beatmap for beatmap in self.coach.catalog if beatmap["key"] == quest["map"]["key"])
        local.update(legacy_fields)
        stored_json = json.dumps(board, ensure_ascii=False)
        with self.coach.db:
            self.coach.db.execute("UPDATE quest_boards SET data=? WHERE id=?", (stored_json, board["id"]))
        old_results = self.coach.db.execute("SELECT id, data, status FROM plays ORDER BY id").fetchall()
        before = self.coach.state()
        self.assertEqual("1762724", before["quest_availability"][quest["id"]]["search_text"])
        local.update(title_romanized=romanized, artist_romanized="Will Stetson")
        app.save_json(self.coach.data / "catalog.json", {"root": self.coach.config["maps_path"],
                                                       "calculator": app.CALCULATOR_ID, "maps": self.coach.catalog})
        after = self.coach.state()
        search = after["quest_availability"][quest["id"]]
        self.assertTrue(search["installed"])
        self.assertEqual("1762724", search["search_text"])
        self.assertEqual("id", search["search_method"])
        self.assertEqual(romanized, search["title_romanized"])
        self.assertEqual("Will Stetson", search["artist_romanized"])
        self.assertEqual("Sotarks", search["creator"])
        self.assertIn("harumachi clover swing arrangement dictate edit", search["search_title_text"].casefold())
        self.assertIn("expert", search["search_title_text"].casefold())
        self.assertNotIn("春待ち", search["search_title_text"])
        self.assertEqual(board, after["quest_board"])
        self.assertEqual(stored_json, self.coach.db.execute("SELECT data FROM quest_boards WHERE id=?", (board["id"],)).fetchone()[0])
        self.assertEqual(old_results, self.coach.db.execute("SELECT id, data, status FROM plays ORDER BY id").fetchall())
        self.coach.close()
        self.coach.db.close()
        self.coach = app.Coach(self.args)
        reopened = self.coach.state()
        self.assertEqual(board, reopened["quest_board"])
        self.assertEqual(search, reopened["quest_availability"][quest["id"]])
        self.assertEqual(stored_json, self.coach.db.execute("SELECT data FROM quest_boards WHERE id=?", (board["id"],)).fetchone()[0])

    def test_updated_stars_do_not_rewrite_missions_attempts_or_progress(self):
        board = self.initial_board()
        quest = self.first_quest(board)
        self.coach.add_play(self.attempt(quest, accuracy=80, misses=20, grade="B"))
        before = self.coach.state()
        tables = ("quest_boards", "plays", "quest_attempts", "coach_progress_points", "coach_rank_milestones")
        stored = {table: self.coach.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        local = next(m for m in self.coach.catalog if m["key"] == quest["map"]["key"])
        local.update(stars=4.001641712836648, calculator=app.CALCULATOR_ID)
        after = self.coach.state()
        self.assertEqual({"stars": local["stars"], "calculator": app.CALCULATOR_ID},
                         after["quest_availability"][quest["id"]]["difficulty"])
        self.assertEqual(before["quest_board"], after["quest_board"])
        self.assertEqual(stored, {table: self.coach.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables})

    def test_display_stars_use_adjusted_profile_and_exact_revision(self):
        quest = self.first_quest(self.initial_board())
        local = next(m for m in self.coach.catalog if m["key"] == quest["map"]["key"])
        local.update(calculator=app.CALCULATOR_ID)
        adjusted = deepcopy(self.coach.catalog)
        current = next(m for m in adjusted if m["key"] == local["key"])
        current["stars"] = 5.6
        with patch.object(self.coach, "catalog_for", return_value=(adjusted, "")):
            state = self.coach.state()
        self.assertEqual(5.6, state["quest_availability"][quest["id"]]["difficulty"]["stars"])
        current["key"] = "another-checksum"
        with patch.object(self.coach, "catalog_for", return_value=(adjusted, "")):
            state = self.coach.state()
        self.assertIsNone(state["quest_availability"][quest["id"]]["difficulty"])

    def test_unversioned_catalog_preserves_identity_but_disables_old_stars(self):
        board = self.initial_board()
        quest = self.first_quest(board)
        app.save_json(self.coach.data / "catalog.json", {"root": self.coach.config["maps_path"],
                                                       "maps": self.coach.catalog})
        self.coach.close()
        self.coach.db.close()
        self.coach = app.Coach(self.args)
        self.assertTrue(self.coach.catalog_stale)
        self.assertTrue(all(m["stars"] is None for m in self.coach.catalog))
        state = self.coach.state()
        self.assertEqual(board, state["quest_board"])
        self.assertTrue(state["quest_availability"][quest["id"]]["installed"])
        self.assertTrue(state["quest_availability"][quest["id"]]["difficulty_pending"])
        self.assertIsNone(state["quest_availability"][quest["id"]]["difficulty"])

    def test_remote_search_joins_exact_downloaded_difficulty_not_another_map_of_same_set(self):
        discovered = deepcopy(self.maps)
        for index, beatmap in enumerate(discovered):
            beatmap.update(id=10000 + index, key=f"remote:{10000 + index}", source="online", local=False)
            beatmap["popularity"] = {"scope": "beatmapset", "rating": 9.0, "rating_votes": 25, "votes": 25, "play_count": 25000}
        self.coach.discovery_store.maps = discovered
        self.coach.catalog = []
        board = self.initial_board()
        quest = self.first_quest(board)
        wrong = deepcopy(quest["map"])
        wrong.update(id=999999, key="b" * 32, source="local", local=True,
                     title_romanized="Wrong difficulty", artist_romanized="Wrong artist", creator="Wrong mapper")
        self.coach.catalog = [wrong]
        before = self.coach.state()["quest_availability"][quest["id"]]
        self.assertFalse(before["installed"])
        self.assertNotIn("wrong", before["search_title_text"].casefold())
        downloaded = deepcopy(quest["map"])
        downloaded.update(key="a" * 32, source="local", local=True,
                          title_romanized="Exact Romanized Song", artist_romanized="Exact Artist", creator="Exact mapper")
        self.coach.catalog.append(downloaded)
        state = self.coach.state()
        search = state["quest_availability"][quest["id"]]
        self.assertTrue(search["installed"])
        self.assertEqual(str(quest["map"]["id"]), search["search_text"])
        self.assertEqual("Exact Romanized Song", search["title_romanized"])
        self.assertEqual("Exact mapper", search["creator"])
        self.assertEqual(board, state["quest_board"])

    def test_local_search_does_not_borrow_metadata_from_different_checksum_with_same_id(self):
        for index, beatmap in enumerate(self.coach.catalog):
            beatmap.update(id=10000 + index, key=f"{index + 1:032x}")
        board = self.initial_board()
        quest = self.first_quest(board)
        changed = deepcopy(quest["map"])
        changed.update(key="f" * 32, title_romanized="Wrong revision", creator="Wrong mapper")
        self.coach.catalog = [changed]
        state = self.coach.state()
        search = state["quest_availability"][quest["id"]]
        self.assertFalse(search["installed"])
        self.assertNotIn("wrong", search["search_title_text"].casefold())
        self.assertNotEqual("Wrong mapper", search["creator"])
        self.assertEqual(board, state["quest_board"])

    def test_different_local_checksum_cannot_complete_goal_with_same_online_id(self):
        for index, beatmap in enumerate(self.coach.catalog):
            beatmap.update(id=10000 + index, key=f"{index + 1:064x}")
        board = self.initial_board()
        quest = self.first_quest(board)
        different_revision = self.attempt(quest, beatmap_key="f" * 64)
        self.coach.add_play(different_revision)
        self.assertEqual(board, self.coach.state()["quest_board"])
        stored = next(play for play in self.coach.plays() if play["id"] == different_revision["id"])
        self.assertEqual("f" * 64, stored["beatmap_key"])

    def test_history_is_bounded_and_scoped_to_active_player(self):
        original = self.initial_board()
        active = self.coach.active
        for _ in range(7):
            self.coach.new_quests(self.coach.state()["quest_board"]["id"])
        history = self.coach.state()["quest_history"]
        self.assertEqual(5, len(history))
        self.assertNotIn(original["id"], [item["id"] for item in history])
        other = self.attempt(self.first_quest(), player="Another player")
        self.coach.add_play(other)
        self.assertEqual([], self.coach.state()["quest_history"])
        self.coach.active = active
        self.assertEqual(history, self.coach.state()["quest_history"])

    def test_reset_archives_board_preserves_scores_and_rejects_old_pending(self):
        board = self.initial_board()
        pending = self.attempt(self.first_quest(board), needs_confirmation=True)
        self.coach.add_play(pending)
        old_count = len(self.coach.plays())
        self.coach.reset()
        state = self.coach.state()
        self.assertEqual(old_count, len(self.coach.plays()))
        self.assertEqual([], state["pending"])
        self.assertNotEqual(board["id"], state["quest_board"]["id"])
        self.assertEqual(0, state["quest_board"]["completed_count"])
        self.assertIn(board["id"], [item["id"] for item in state["quest_history"]])
        with self.assertRaises(ValueError):
            self.coach.confirm(pending["id"], True)

    def test_new_quests_http_requires_token_origin_and_current_board(self):
        board = self.initial_board()
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        server.coach = self.coach
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f"http://127.0.0.1:{server.server_port}"

        def request(identifier, headers):
            payload = json.dumps({"board_id": identifier}).encode()
            return Request(address + "/api/quests/new", data=payload, headers=headers)

        try:
            valid_headers = {"Content-Type": "application/json", "X-Coach-Token": self.coach.token}
            for headers in ({"Content-Type": "application/json"},
                            {**valid_headers, "Origin": "https://unrelated.example"}):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request(board["id"], headers), timeout=5)
                self.assertEqual(403, caught.exception.code)
                self.assertEqual(board, self.coach.state()["quest_board"])
            with urlopen(request(board["id"], {**valid_headers, "Origin": address}), timeout=5) as response:
                self.assertTrue(json.load(response)["ok"])
            new = self.coach.state()["quest_board"]
            self.assertNotEqual(board["id"], new["id"])
            with self.assertRaises(HTTPError) as caught:
                urlopen(request(board["id"], valid_headers), timeout=5)
            self.assertEqual(400, caught.exception.code)
            self.assertEqual(new, self.coach.state()["quest_board"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
