"""Individual mission replacement preserves pending work and earned completions."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app


class QuestRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None,
                                       no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = app.Coach(self.args)
        self.now = datetime.now(timezone.utc)
        self.serial = 0
        self.maps = []
        for stars in (4.3, 4.65, 4.8):
            for _ in range(12):
                index = len(self.maps)
                self.maps.append({"key": f"rotation-map-{index}", "id": 10000 + index,
                                  "set_id": 10000 + index, "title": f"Rotation song {index}",
                                  "artist": "Fixture", "version": "Insane", "stars": stars,
                                  "mode": 0, "bpm": 150, "ar": 8, "length": 120,
                                  "object_count": 400, "max_combo": 500, "source": "local", "tags": []})
        self.coach.catalog = deepcopy(self.maps)
        self.coach.config["since"] = (self.now - timedelta(days=1)).isoformat()
        app.save_json(self.coach.config_path, self.coach.config)
        for index in range(5):
            self.coach.add_play(self.seed_play(index))
        with patch("quest_store.utcnow", return_value=(self.now - timedelta(seconds=2)).isoformat()):
            self.initial = self.coach.state()["quest_board"]

    def tearDown(self):
        self.coach.close()
        self.coach.db.close()
        self.temp.cleanup()

    def seed_play(self, index):
        return {"id": f"rotation-seed-{index}", "beatmap_key": f"unrelated-seed-{index}",
                "beatmap_id": 20000 + index, "played_at": (self.now - timedelta(minutes=15-index)).isoformat(),
                "player": "Rotation fixture", "client": "lazer", "mode": 0,
                "mods": [], "mod_key": app.DEFAULT_MOD_KEY, "stars": 4.5,
                "accuracy": 98, "misses": 0, "max_combo": 490, "map_max_combo": 500,
                "object_count": 400, "judged_objects": 400, "passed": True,
                "completion": 1, "grade": "S", "bpm": 150, "ar": 8, "length": 120}

    @staticmethod
    def quests(board):
        return [quest for group in board["groups"] for quest in group["quests"]]

    @staticmethod
    def song(quest):
        return tuple(str(quest["map"].get(field, "")).casefold() for field in ("artist", "title"))

    def play(self, quest, **changes):
        self.serial += 1
        beatmap = quest["map"]
        now = datetime.now(timezone.utc).isoformat()
        result = self.seed_play(0)
        result.update(id=f"rotation-attempt-{self.serial}", beatmap_key=beatmap["key"],
                      beatmap_id=beatmap.get("id", 0), stars=beatmap["stars"], played_at=now,
                      started_at=now, accuracy=100, misses=0, grade="SS",
                      max_combo=beatmap["max_combo"], map_max_combo=beatmap["max_combo"],
                      object_count=beatmap["object_count"], judged_objects=beatmap["object_count"])
        result.update(changes)
        return result

    def reopen(self):
        self.coach.close()
        self.coach.db.close()
        self.coach = app.Coach(self.args)
        self.coach.catalog = deepcopy(self.maps)

    def test_one_completed_mission_replaces_only_that_slot_on_state_refresh(self):
        before = self.initial
        old = self.quests(before)[0]
        self.assertEqual(9, len(self.quests(before)))
        self.coach.add_play(self.play(old))
        state = self.coach.state()
        after = state["quest_board"]
        self.assertEqual(before["id"], after["id"])
        self.assertTrue(after["automatic_refresh"])
        self.assertEqual(9, after["active_count"])
        self.assertEqual(0, after["waiting_count"])
        self.assertEqual(1, after["retired_count"])
        self.assertEqual(1, after["completed_count"])
        self.assertEqual(10, after["total_count"])
        old_quests = {quest["id"]: quest for quest in self.quests(before)}
        new_quests = {quest["id"]: quest for quest in self.quests(after)}
        self.assertNotIn(old["id"], new_quests)
        for identifier in old_quests.keys() - {old["id"]}:
            self.assertEqual(old_quests[identifier], new_quests[identifier])
        replacement = next(quest for identifier, quest in new_quests.items() if identifier not in old_quests)
        self.assertIn(replacement, after["groups"][0]["quests"])
        self.assertEqual("pending", replacement["status"])
        self.assertEqual(0, replacement["attempt_count"])
        self.assertNotIn(replacement["map"]["key"], {quest["map"]["key"] for quest in old_quests.values()})
        self.assertNotIn(self.song(replacement), {self.song(quest) for quest in old_quests.values()})
        self.assertEqual(1, state["quest_completions"]["total"])
        achievement = state["quest_completions"]["items"][0]
        self.assertEqual(old["id"], achievement["id"])
        self.assertEqual(old["map"]["expectation"], achievement["map"]["expectation"])
        self.assertEqual("completed", achievement["status"])
        self.assertEqual(after, self.coach.state()["quest_board"])

    def test_pending_attempt_feedback_and_targets_remain_frozen_after_other_completion(self):
        quests = self.quests(self.initial)
        self.coach.add_play(self.play(quests[1], accuracy=80, misses=25, max_combo=100, grade="B"))
        before = self.coach.state()["quest_board"]
        in_progress = next(quest for quest in self.quests(before) if quest["id"] == quests[1]["id"])
        self.assertEqual("in_progress", in_progress["status"])
        self.coach.add_play(self.play(quests[0]))
        after = self.coach.state()["quest_board"]
        for old in self.quests(before):
            if old["id"] != quests[0]["id"]:
                self.assertEqual(old, next(quest for quest in self.quests(after) if quest["id"] == old["id"]))

    def test_multiple_completions_before_refresh_replace_every_slot_without_collisions(self):
        previous = self.quests(self.initial)
        # These nine successes raise the reference, so the library also needs
        # maps in the new range shared by practice and challenge.
        for index in range(20):
            extra = deepcopy(self.maps[0])
            extra.update(key=f"advanced-{index}", id=30000 + index, set_id=30000 + index,
                         title=f"Advanced song {index}", stars=5.0)
            self.coach.catalog.append(extra)
        for quest in previous:
            self.coach.add_play(self.play(quest))
        state = self.coach.state()
        board = state["quest_board"]
        refreshed = self.quests(board)
        self.assertEqual(self.initial["id"], board["id"])
        self.assertEqual(9, len(refreshed))
        self.assertEqual(9, board["active_count"])
        self.assertEqual(0, board["waiting_count"])
        self.assertEqual(9, board["retired_count"])
        self.assertEqual(9, state["quest_completions"]["total"])
        self.assertTrue(all(quest["status"] == "pending" for quest in refreshed))
        self.assertEqual(9, len({quest["map"]["id"] for quest in refreshed}))
        self.assertEqual(9, len({self.song(quest) for quest in refreshed}))
        self.assertTrue({quest["map"]["id"] for quest in previous}.isdisjoint(
            quest["map"]["id"] for quest in refreshed))

    def test_completion_log_is_idempotent_and_survives_new_board_reset_and_restart(self):
        quest = self.quests(self.initial)[0]
        success = self.play(quest)
        self.coach.add_play(success)
        after = self.coach.state()
        expected = after["quest_completions"]
        self.coach.add_play(deepcopy(success))
        self.assertEqual(expected, self.coach.state()["quest_completions"])
        self.coach.new_quests(after["quest_board"]["id"])
        self.assertEqual(expected, self.coach.state()["quest_completions"])
        self.coach.reset()
        self.assertEqual(expected, self.coach.state()["quest_completions"])
        self.reopen()
        self.assertEqual(expected, self.coach.state()["quest_completions"])

    def test_no_alternative_keeps_completed_visible_until_catalog_gains_a_valid_map(self):
        old = self.quests(self.initial)[0]
        self.coach.catalog = [deepcopy(quest["map"]) for quest in self.quests(self.initial)]
        for index, unsuitable in enumerate(({"stars": 9}, {"bpm": 300}, {"ar": 11}, {"length": 900},
                                            {"id": old["map"]["id"]},
                                            {"key": old["map"]["key"], "id": 0,
                                             "title": old["map"]["title"].upper()})):
            candidate = deepcopy(old["map"])
            candidate.update(key=f"unsuitable-{index}", id=90000 + index, set_id=90000 + index,
                             title=f"Unsuitable song {index}")
            candidate.update(unsuitable)
            self.coach.catalog.append(candidate)
        self.coach.add_play(self.play(old))
        waiting = self.coach.state()
        quest = next(quest for quest in self.quests(waiting["quest_board"]) if quest["id"] == old["id"])
        self.assertEqual("completed", quest["status"])
        self.assertEqual(1, waiting["quest_board"]["waiting_count"])
        self.assertEqual(1, waiting["quest_completions"]["total"])
        self.assertEqual(waiting["quest_board"], self.coach.state()["quest_board"])
        candidate = deepcopy(old["map"])
        candidate.update(key="newly-scanned", id=999999, set_id=999999, title="New scanned song")
        self.coach.catalog.append(candidate)
        refreshed = self.coach.state()
        self.assertEqual(self.initial["id"], refreshed["quest_board"]["id"])
        self.assertEqual(0, refreshed["quest_board"]["waiting_count"])
        self.assertNotIn(old["id"], {quest["id"] for quest in self.quests(refreshed["quest_board"])})
        self.assertIn("newly-scanned", {quest["map"]["key"] for quest in self.quests(refreshed["quest_board"])})
        self.assertEqual(1, refreshed["quest_completions"]["total"])

    def test_old_result_or_late_confirmation_cannot_complete_a_replacement(self):
        old = self.quests(self.initial)[0]
        success = self.play(old)
        self.coach.add_play(success)
        state = self.coach.state()
        old_ids = {quest["id"] for quest in self.quests(self.initial)}
        replacement = next(quest for quest in self.quests(state["quest_board"]) if quest["id"] not in old_ids)
        too_early = (datetime.fromisoformat(replacement["created_at"]) - timedelta(seconds=1)).isoformat()
        delayed = self.play(replacement, played_at=too_early, started_at=too_early, needs_confirmation=True)
        self.coach.add_play(delayed)
        self.coach.confirm(delayed["id"], True)
        self.coach.add_play(deepcopy(success))
        after = self.coach.state()
        current = {quest["id"]: quest for quest in self.quests(after["quest_board"])}
        self.assertEqual(state["quest_board"]["id"], after["quest_board"]["id"])
        self.assertNotIn(replacement["id"], current)
        for quest in self.quests(state["quest_board"]):
            if quest["id"] != replacement["id"]:
                self.assertEqual(quest, current[quest["id"]])
        self.assertEqual(1, after["quest_skips"]["total"])
        omitted = after["quest_skips"]["items"][0]
        self.assertEqual(replacement["id"], omitted["id"])
        self.assertEqual("played_before_assignment", omitted["skipped_reason"])
        self.assertEqual(0, omitted["attempt_count"])
        self.assertEqual(replacement["map"]["expectation"], omitted["map"]["expectation"])
        self.assertEqual(1, after["quest_board"]["completed_count"])
        self.assertEqual(1, after["quest_completions"]["total"])

    def test_completed_difficulties_are_not_recommended_again(self):
        board = self.initial
        completed_keys = set()
        for _ in range(3):
            quest = board["groups"][0]["quests"][0]
            completed_keys.add(quest["map"]["key"])
            self.coach.add_play(self.play(quest))
            board = self.coach.state()["quest_board"]
            visible = self.quests(board)
            self.assertTrue(completed_keys.isdisjoint(quest["map"]["key"] for quest in visible))
            self.assertEqual(len(visible), len({quest["map"]["id"] for quest in visible}))
        self.assertEqual(3, self.coach.state()["quest_completions"]["total"])

    def test_rotated_board_and_completion_log_are_isolated_by_player_and_mods(self):
        self.coach.add_play(self.play(self.quests(self.initial)[0]))
        expected = self.coach.state()
        original_profile = self.coach.active
        for fields in ({"player": "Another player"},
                       {"mods": [{"acronym": "HD"}], "mod_key": '{"mods":[{"acronym":"HD"}],"rate":1.0}'}):
            with self.subTest(fields=fields):
                self.coach.add_play(self.play(self.quests(expected["quest_board"])[0], **fields))
                with patch.object(self.coach, "catalog_for", return_value=([], "")):
                    other = self.coach.state()
                self.assertEqual(0, other["quest_completions"]["total"])
                self.assertEqual([], other["quest_completions"]["items"])
        self.coach.active = original_profile
        actual = self.coach.state()
        self.assertEqual(expected["quest_board"], actual["quest_board"])
        self.assertEqual(expected["quest_completions"], actual["quest_completions"])

    def test_active_and_archived_legacy_completions_are_migrated_once(self):
        active = deepcopy(self.initial)
        archived = deepcopy(self.initial)
        archived.update(id="legacy-archived-board", status="archived", archived_at=self.now.isoformat())
        achieved_ids = []
        for index, board in enumerate((active, archived)):
            quest = board["groups"][0]["quests"][0]
            quest.update(id=f"legacy-achievement-{index}", status="completed", attempt_count=1,
                         completed_at=self.now.isoformat(), completed_play_id=f"legacy-result-{index}")
            achieved_ids.append(quest["id"])
            for remaining in self.quests(board)[1:]:
                remaining["id"] = f"legacy-{index}-" + remaining["id"]
            board.update(completed_count=1, total_count=len(self.quests(board)))
        config = deepcopy(self.coach.config)
        scope = app.scope_key(self.coach.active, config["since"])
        self.coach.close()
        self.coach.db.close()
        legacy = Path(self.temp.name) / "legacy"
        legacy.mkdir()
        app.save_json(legacy / "config.json", config)
        with closing(sqlite3.connect(legacy / "coach.sqlite3")) as db:
            with db:
                db.execute("CREATE TABLE plays (id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL)")
                for index in range(5):
                    play = self.seed_play(index)
                    db.execute("INSERT INTO plays VALUES (?, ?, 'accepted')", (play["id"], json.dumps(play)))
                db.execute("CREATE TABLE quest_boards (id TEXT PRIMARY KEY, scope TEXT NOT NULL, profile TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL)")
                for board in (active, archived):
                    db.execute("INSERT INTO quest_boards VALUES (?, ?, ?, ?, ?)",
                               (board["id"], scope, config["active"], board["status"], json.dumps(board)))
        self.args.data_dir = str(legacy)
        self.coach = app.Coach(self.args)
        self.coach.catalog = deepcopy(self.maps)
        migrated = self.coach.state()["quest_completions"]
        self.assertEqual(2, migrated["total"])
        self.assertEqual(set(achieved_ids), {quest["id"] for quest in migrated["items"]})
        self.reopen()
        self.assertEqual(migrated, self.coach.state()["quest_completions"])


if __name__ == "__main__":
    unittest.main()
