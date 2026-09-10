"""Played-difficulty exclusion across recommendations and mission renewal."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import json
import tempfile
import unittest
from unittest.mock import patch

import app


class PlayedRecommendationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None,
                                       no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = app.Coach(self.args)
        self.now = datetime.now(timezone.utc)
        self.serial = 0
        self.maps = []
        for stars in (4.3, 4.65, 4.8, 5.0):
            for _ in range(15):
                index = len(self.maps)
                self.maps.append({"key": f"played-map-{index}", "id": 60000 + index,
                                  "set_id": 60000 + index, "title": f"Unplayed song {index}",
                                  "artist": "History fixture", "version": "Insane", "stars": stars,
                                  "mode": 0, "bpm": 150, "ar": 8, "length": 120,
                                  "object_count": 400, "max_combo": 500,
                                  "source": "local", "tags": []})
        self.coach.catalog = deepcopy(self.maps)
        self.coach.config.update(since=(self.now - timedelta(days=2)).isoformat())
        self.coach.update_settings({"initial_stars": 4.65})
        for index in range(5):
            self.coach.add_play(self.result(played_at=(self.now - timedelta(minutes=15-index)).isoformat()))
        self.active = self.coach.active
        # All discovery in these tests is explicit and stays in memory.
        self.discovery = patch.object(self.coach.discovery_store, "candidates", return_value=[])
        self.discovery.start()

    def tearDown(self):
        self.discovery.stop()
        self.coach.close()
        self.coach.db.close()
        self.temp.cleanup()

    def result(self, beatmap=None, **changes):
        self.serial += 1
        result = {"id": f"history-result-{self.serial}", "beatmap_key": f"seed-{self.serial}",
                  "beatmap_id": 80000 + self.serial, "played_at": self.now.isoformat(),
                  "player": "History fixture", "client": "lazer", "mode": 0,
                  "mods": [], "mod_key": app.DEFAULT_MOD_KEY, "stars": 4.5,
                  "accuracy": 98, "misses": 0, "max_combo": 490, "map_max_combo": 500,
                  "object_count": 400, "judged_objects": 400, "passed": True,
                  "completion": 1, "grade": "S", "bpm": 150, "ar": 8, "length": 120}
        if beatmap:
            result.update(beatmap_key=beatmap["key"], beatmap_id=beatmap.get("id", 0),
                          beatmap_set_id=beatmap.get("set_id", 0), stars=beatmap["stars"])
            for field in ("title", "artist", "title_romanized", "artist_romanized", "version"):
                if beatmap.get(field):
                    result[field] = beatmap[field]
        result.update(changes)
        return result

    def stored(self, beatmap=None, status="accepted", **changes):
        """Set up old imported history without awarding a current mission."""
        changes.setdefault("played_at", (self.now - timedelta(days=90)).isoformat())
        result = self.result(beatmap, **changes)
        with self.coach.db:
            self.coach.db.execute("INSERT INTO plays VALUES (?, ?, ?)",
                                  (result["id"], json.dumps(result, ensure_ascii=False), status))
        return result

    def board(self):
        with patch("quest_store.utcnow", return_value=(self.now - timedelta(seconds=2)).isoformat()):
            return self.coach.state()["quest_board"]

    @staticmethod
    def quests(board):
        return [quest for group in board["groups"] for quest in group["quests"]] if board else []

    @staticmethod
    def recommended(state):
        return [beatmap for group in state["recommendations"] for beatmap in group["maps"]]

    def reopen(self):
        self.discovery.stop()
        catalog = deepcopy(self.coach.catalog)
        self.coach.close()
        self.coach.db.close()
        self.coach = app.Coach(self.args)
        self.coach.catalog = catalog
        self.discovery = patch.object(self.coach.discovery_store, "candidates", return_value=[])
        self.discovery.start()

    def test_full_accepted_history_excludes_old_difficulties_after_reset_and_restart(self):
        forbidden = self.maps[0]
        self.stored(forbidden)
        # A display limit or rows from other profiles must not hide old evidence.
        for index in range(505):
            self.stored(player=f"Other player {index}")
        for action in (lambda: None, self.coach.reset, self.reopen):
            action()
            state = self.coach.state()
            suggested = self.recommended(state) + [q["map"] for q in self.quests(state["quest_board"])]
            self.assertTrue(suggested)
            self.assertNotIn(forbidden["key"], {beatmap["key"] for beatmap in suggested})
        self.assertEqual(511, self.coach.db.execute("SELECT COUNT(*) FROM plays").fetchone()[0])

    def test_pending_rejected_and_other_profiles_do_not_block_the_difficulty(self):
        candidate = self.maps[0]
        self.coach.catalog = [deepcopy(candidate)]
        for status in ("pending", "rejected"):
            self.stored(candidate, status=status)
        self.stored(candidate, player="Different player")
        self.stored(candidate, client="stable")
        self.stored(candidate, mod_key='{"mods":[{"acronym":"HD"}],"rate":1.0}')
        self.stored(candidate, excluded=True)
        self.assertEqual([candidate["key"]], [m["key"] for m in self.recommended(self.coach.state())])
        # Accepted failures count as played; explicitly excluded records do not.
        self.stored(candidate, passed=False, completion=.2, accuracy=60)
        self.assertEqual([], self.recommended(self.coach.state()))

    def test_original_difficulty_stays_excluded_by_hash_and_online_id(self):
        original = deepcopy(self.maps[0])
        revised = deepcopy(original)
        revised.update(key="updated-local-checksum", title="Changed display title")
        fresh = deepcopy(self.maps[2])
        hash_only = deepcopy(self.maps[3])
        hash_only.update(id=0, set_id=0)
        self.coach.catalog = [original, revised, fresh, hash_only]
        # Catalog identity joins an old checksum-only result to the same online
        # difficulty after its local checksum or display metadata changes.
        self.stored(beatmap_key=original["key"], beatmap_id=0)
        self.stored(beatmap_key=hash_only["key"], beatmap_id=0)
        state = self.coach.state()
        self.assertEqual([fresh["key"]], [beatmap["key"] for beatmap in self.recommended(state)])
        self.assertEqual([fresh["key"]], [q["map"]["key"] for q in self.quests(state["quest_board"])])

    def test_other_difficulties_of_the_same_song_are_eligible_despite_shared_metadata(self):
        original = deepcopy(self.maps[0])
        original.update(title="春待ちクローバー", title_romanized="Harumachi Clover",
                        artist="歌手", artist_romanized="Fixture singer", creator="Original mapper")
        self.stored(original)
        variants = ({"version": "Another difficulty"},
                    {"version": "Another difficulty", "set_id": 99001},
                    {"version": original["version"], "creator": "Other mapper", "set_id": 99002},
                    {"title": "Harumachi Clover", "artist": "Fixture singer", "set_id": 99003})
        for index, fields in enumerate(variants):
            with self.subTest(fields=fields):
                candidate = deepcopy(original)
                candidate.update(key=f"another-difficulty-{index}", id=99010 + index)
                candidate.update(fields)
                self.coach.catalog = [original, candidate]
                state = self.coach.state(ensure_quests=False)
                self.assertEqual([candidate["key"]], [m["key"] for m in self.recommended(state)])
        board = self.coach.state()["quest_board"]
        self.assertEqual([candidate["key"]], [q["map"]["key"] for q in self.quests(board)])

    def test_pending_mission_survives_earlier_play_on_another_difficulty_of_its_song(self):
        before = self.board()
        quest = self.quests(before)[0]
        played = deepcopy(quest["map"])
        played.update(key="previously-played-difficulty", id=99020, version="Easier difficulty")
        self.stored(played)
        state = self.coach.state()
        self.assertEqual(before, state["quest_board"])
        self.assertEqual(0, state["quest_skips"]["total"])
        self.assertEqual(0, state["quest_completions"]["total"])

    def test_completed_slot_can_use_another_difficulty_of_the_same_song_and_set(self):
        before = self.board()
        quests = self.quests(before)
        target = quests[0]
        candidate = deepcopy(target["map"])
        candidate.update(key="replacement-difficulty", id=99021, version="Another difficulty")
        self.coach.catalog = [deepcopy(quest["map"]) for quest in quests] + [candidate]
        self.coach.add_play(self.result(target["map"], played_at=datetime.now(timezone.utc).isoformat(),
                                       accuracy=100, max_combo=500, grade="SS"))
        state = self.coach.state()
        after = state["quest_board"]
        self.assertEqual(before["id"], after["id"])
        current = {quest["id"]: quest for quest in self.quests(after)}
        self.assertNotIn(target["id"], current)
        self.assertIn(candidate["key"], {q["map"]["key"] for q in current.values()})
        for quest in quests[1:]:
            self.assertEqual(quest, current[quest["id"]])
        self.assertEqual(1, state["quest_completions"]["total"])
        self.assertEqual(0, state["quest_skips"]["total"])
        self.assertEqual(0, after["waiting_count"])

    def test_unstarted_legacy_mission_is_replaced_without_touching_other_eight(self):
        before = self.board()
        quests = self.quests(before)
        self.assertEqual(9, len(quests))
        stale = quests[0]
        self.stored(stale["map"])
        state = self.coach.state()
        after = state["quest_board"]
        self.assertEqual(before["id"], after["id"])
        current = {quest["id"]: quest for quest in self.quests(after)}
        self.assertEqual(9, len(current))
        self.assertNotIn(stale["id"], current)
        for quest in quests[1:]:
            self.assertEqual(quest, current[quest["id"]])
        replacement = next(quest for identifier, quest in current.items()
                           if identifier not in {old["id"] for old in quests})
        self.assertNotEqual(stale["map"]["key"], replacement["map"]["key"])
        self.assertEqual("pending", replacement["status"])
        self.assertEqual(0, replacement["attempt_count"])
        self.assertEqual(0, state["quest_completions"]["total"])
        self.assertEqual(0, after["completed_count"])
        self.assertEqual(1, after["skipped_count"])
        self.assertEqual(0, after["skipped_waiting_count"])
        self.assertEqual(0, after["retired_count"])
        self.assertEqual(10, after["total_count"])
        self.assertEqual(1, state["quest_skips"]["total"])
        omitted = state["quest_skips"]["items"][0]
        self.assertEqual(stale["id"], omitted["id"])
        self.assertEqual("skipped", omitted["status"])
        self.assertEqual("played_before_assignment", omitted["skipped_reason"])
        self.assertTrue(omitted["skipped_at"])
        self.assertEqual(stale["map"], omitted["map"])
        self.assertEqual(after, self.coach.state()["quest_board"])
        self.reopen()
        reopened = self.coach.state()
        self.assertEqual(after, reopened["quest_board"])
        self.assertEqual(state["quest_skips"], reopened["quest_skips"])

    def test_skipped_slot_waits_for_an_unplayed_candidate_without_awarding_a_completion(self):
        before = self.board()
        original = self.quests(before)
        stale = original[0]
        self.coach.catalog = [deepcopy(quest["map"]) for quest in original]
        self.stored(stale["map"])
        waiting = self.coach.state()
        board = waiting["quest_board"]
        skipped = next(q for q in self.quests(board) if q["id"] == stale["id"])
        self.assertEqual("skipped", skipped["status"])
        self.assertEqual(8, board["active_count"])
        self.assertEqual(1, board["skipped_count"])
        self.assertEqual(1, board["skipped_waiting_count"])
        self.assertEqual(0, board["waiting_count"])
        self.assertEqual(0, board["completed_count"])
        self.assertEqual(9, board["total_count"])
        self.assertEqual(0, waiting["quest_completions"]["total"])
        self.assertEqual(1, waiting["quest_skips"]["total"])
        self.assertEqual(board, self.coach.state()["quest_board"])
        candidate = deepcopy(stale["map"])
        candidate.update(key="new-library-song", id=99000, set_id=99000, title="A new song")
        self.coach.catalog.append(candidate)
        refreshed = self.coach.state()
        after = refreshed["quest_board"]
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(9, after["active_count"])
        self.assertEqual(0, after["skipped_waiting_count"])
        self.assertEqual(1, after["skipped_count"])
        self.assertEqual(10, after["total_count"])
        self.assertEqual(0, after["completed_count"])
        self.assertEqual(waiting["quest_skips"], refreshed["quest_skips"])
        current = {q["id"]: q for q in self.quests(after)}
        self.assertNotIn(stale["id"], current)
        self.assertIn(candidate["key"], {q["map"]["key"] for q in current.values()})
        for quest in original[1:]:
            self.assertEqual(quest, current[quest["id"]])

    def test_started_mission_keeps_its_attempt_and_goal_despite_older_play(self):
        before = self.board()
        quest = self.quests(before)[0]
        attempt = self.result(quest["map"], played_at=datetime.now(timezone.utc).isoformat(),
                              accuracy=80, misses=20, grade="B", max_combo=100)
        self.coach.add_play(attempt)
        started = self.coach.state()["quest_board"]
        current = next(q for q in self.quests(started) if q["id"] == quest["id"])
        self.assertEqual("in_progress", current["status"])
        self.assertEqual(1, current["attempt_count"])
        self.stored(quest["map"])
        self.assertEqual(started, self.coach.state()["quest_board"])
        self.reopen()
        self.assertEqual(started, self.coach.state()["quest_board"])

    def test_pending_scores_from_other_profiles_cannot_keep_a_stale_mission(self):
        before = self.board()
        stale = self.quests(before)[0]
        self.stored(stale["map"])
        for changes in ({"player": "Other player"}, {"client": "stable"},
                        {"mod_key": '{"mods":[{"acronym":"HD"}],"rate":1.0}'}):
            self.stored(stale["map"], status="pending", needs_confirmation=True,
                        played_at=datetime.now(timezone.utc).isoformat(), **changes)
        state = self.coach.state()
        self.assertNotIn(stale["id"], {q["id"] for q in self.quests(state["quest_board"])})
        self.assertEqual(1, state["quest_skips"]["total"])

    def test_active_game_and_its_pending_confirmation_preserve_the_assigned_goal(self):
        before = self.board()
        quest = self.quests(before)[0]
        self.stored(quest["map"])
        self.coach.last_snapshot = {"state": {"number": 2},
                                   "beatmap": {"checksum": quest["map"]["key"], "id": quest["map"]["id"]}}
        self.assertEqual(before, self.coach.state()["quest_board"])
        pending = self.result(quest["map"], played_at=datetime.now(timezone.utc).isoformat(),
                              needs_confirmation=True, accuracy=80, grade="B", misses=20, max_combo=100)
        self.coach.add_play(pending)
        self.coach.last_snapshot = {"state": {"number": 7}}
        # A large display queue from another profile cannot hide the score that
        # protects this still-unconfirmed goal.
        for index in range(505):
            self.stored(status="pending", needs_confirmation=True, player=f"Queued player {index}")
        self.assertEqual(before, self.coach.state()["quest_board"])
        self.coach.confirm(pending["id"], True)
        state = self.coach.state()
        current = next(q for q in self.quests(state["quest_board"]) if q["id"] == quest["id"])
        self.assertEqual("in_progress", current["status"])
        self.assertEqual(1, current["attempt_count"])
        self.assertEqual(quest["map"]["expectation"], current["map"]["expectation"])
        self.assertEqual(0, state["quest_skips"]["total"])

    def test_legacy_mission_with_unknown_assignment_time_is_preserved(self):
        before = self.board()
        self.stored(self.quests(before)[0]["map"])
        for invalid in (None, "not-a-date", "missing"):
            with self.subTest(created_at=invalid):
                legacy = deepcopy(before)
                quest = self.quests(legacy)[0]
                if invalid == "missing":
                    quest.pop("created_at")
                else:
                    quest["created_at"] = invalid
                with self.coach.db:
                    self.coach.db.execute("UPDATE quest_boards SET data=? WHERE id=?",
                                          (json.dumps(legacy, ensure_ascii=False), legacy["id"]))
                state = self.coach.state()
                self.assertEqual(legacy, state["quest_board"])
                self.assertEqual(0, state["quest_skips"]["total"])

    def test_new_board_and_completed_slot_never_fall_back_to_played_candidates(self):
        before = self.board()
        original = self.quests(before)
        target = original[0]
        visible = {quest["map"]["key"] for quest in original}
        hidden_played = [beatmap for beatmap in self.maps if beatmap["key"] not in visible][:5]
        for beatmap in hidden_played:
            self.stored(beatmap)
        success = self.result(target["map"], played_at=datetime.now(timezone.utc).isoformat(),
                              accuracy=100, max_combo=500, grade="SS")
        self.coach.add_play(success)
        refreshed = self.coach.state()
        forbidden = {target["map"]["key"]} | {beatmap["key"] for beatmap in hidden_played}
        self.assertTrue(forbidden.isdisjoint(q["map"]["key"] for q in self.quests(refreshed["quest_board"])))
        self.assertEqual(1, refreshed["quest_completions"]["total"])
        self.coach.new_quests(refreshed["quest_board"]["id"])
        new_state = self.coach.state()
        self.assertNotEqual(before["id"], new_state["quest_board"]["id"])
        suggested = self.recommended(new_state) + [q["map"] for q in self.quests(new_state["quest_board"])]
        self.assertTrue(forbidden.isdisjoint(m["key"] for m in suggested))

    def test_all_played_library_produces_no_recommendations_or_initial_missions(self):
        for beatmap in self.maps:
            self.stored(beatmap)
        state = self.coach.state()
        self.assertEqual([], self.recommended(state))
        self.assertIsNone(state["quest_board"])
        self.assertEqual(0, state["quest_completions"]["total"])
        self.assertTrue(all(group["empty_reason"] == "no_unplayed_maps_in_range"
                            for group in state["recommendations"]))
        self.assertEqual("unplayed", state["recommendation_policy"]["mode"])
        self.assertTrue(state["recommendation_policy"]["message"])
        self.assertEqual(65, state["recommendation_policy"]["history_plays"])

    def test_online_discovery_allows_another_difficulty_but_excludes_the_played_id(self):
        played = self.maps[0]
        self.stored(played)
        remote = deepcopy(played)
        remote.update(key=f"osu:{played['id']}", source="online", local=False)
        remote["popularity"] = {"scope": "beatmapset", "rating": 9.0, "rating_votes": 25, "votes": 25, "play_count": 25000}
        fresh = deepcopy(remote)
        fresh.update(key="osu:99002", id=99002, version="Another difficulty")
        self.coach.catalog = []
        self.discovery.stop()
        self.discovery = patch.object(self.coach.discovery_store, "candidates", return_value=[remote, fresh])
        self.discovery.start()
        state = self.coach.state()
        self.assertEqual([fresh["key"]], [m["key"] for m in self.recommended(state)])
        self.assertEqual([fresh["key"]], [q["map"]["key"] for q in self.quests(state["quest_board"])])

    def test_legacy_completion_without_a_play_row_stays_excluded_across_reset(self):
        legacy = self.board()
        legacy["created_at"] = (self.now - timedelta(days=90)).isoformat()
        for quest in self.quests(legacy):
            quest["created_at"] = legacy["created_at"]
        completed = self.quests(legacy)[0]
        completed.update(status="completed", attempt_count=1, completed_play_id="legacy-missing-play",
                         completed_at=(self.now - timedelta(days=60)).isoformat())
        with self.coach.db:
            self.coach.db.execute("UPDATE quest_boards SET data=? WHERE id=?",
                                  (json.dumps(legacy, ensure_ascii=False), legacy["id"]))
        self.reopen()
        state = self.coach.state()
        self.assertEqual(1, state["quest_completions"]["total"])
        self.assertIsNone(self.coach.db.execute("SELECT id FROM plays WHERE id='legacy-missing-play'").fetchone())
        self.coach.reset()
        self.reopen()
        state = self.coach.state()
        suggested = self.recommended(state) + [q["map"] for q in self.quests(state["quest_board"])]
        self.assertTrue(suggested)
        self.assertNotIn(completed["map"]["key"], {m["key"] for m in suggested})
        self.assertEqual(1, state["quest_completions"]["total"])


if __name__ == "__main__":
    unittest.main()
