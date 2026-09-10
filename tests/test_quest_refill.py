"""Missing mission capacity fills automatically without changing assigned work."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from quest_store import QuestStore, scope_key, map_tokens


class QuestRefillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "quests.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.store = QuestStore(self.db)
        self.now = datetime.now(timezone.utc)
        self.assigned = (self.now - timedelta(seconds=10)).isoformat()
        self.scope = scope_key("refill-profile", self.assigned)
        self.serial = 0

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def beatmap(self, identifier, *, online=False, **changes):
        result = {"key": f"refill-{identifier}", "id": identifier, "set_id": identifier,
                  "title": f"Refill song {identifier}", "artist": "Fixture",
                  "version": "Insane", "mode": 0, "stars": 4.5, "bpm": 150,
                  "source": "online" if online else "local", "local": not online,
                  "expectation": {"grade_min": "A", "accuracy_min": 95,
                                  "misses_max": 3, "combo_min": 100}}
        result.update(changes)
        return result

    @staticmethod
    def group(stage, maps=(), **changes):
        return {"stage": stage, "label": stage, "description": "A prepared stage",
                "target": 4.5, "maps": list(maps), **changes}

    def groups(self, warmup=(), practice=(), challenge=()):
        return [self.group("warmup", warmup), self.group("practice", practice),
                self.group("challenge", challenge)]

    @staticmethod
    def quests(board):
        return [q for group in board["groups"] for q in group["quests"]]

    def ensure(self, groups, provider=None, skip=None):
        with self.db:
            return self.store.ensure(self.scope, "Refill fixture", groups,
                                     replacement_provider=provider, skip_predicate=skip)

    def create(self, groups):
        with patch("quest_store.utcnow", return_value=self.assigned):
            return self.ensure(groups)

    def result(self, quest, **changes):
        self.serial += 1
        return {"id": f"refill-play-{self.serial}", "beatmap_key": quest["map"]["key"],
                "beatmap_id": quest["map"]["id"], "mode": 0, "client": "lazer",
                "played_at": datetime.now(timezone.utc).isoformat(),
                "accuracy": 100, "misses": 0, "max_combo": 500, "grade": "SS",
                "passed": True, "completion": 1, **changes}

    def record(self, play):
        with self.db:
            self.store.record_play(self.scope, play)

    def reopen(self):
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.store = QuestStore(self.db)

    def test_no_initial_candidates_stays_empty_until_downloads_become_available(self):
        self.assertIsNone(self.create(self.groups()))
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM quest_boards").fetchone()[0])
        fresh = self.groups()
        fresh[1] = self.group("practice", [self.beatmap(i, online=True) for i in range(10, 13)],
                              max_online=3)
        board = self.ensure(fresh)
        self.assertEqual(3, len(board["groups"][1]["quests"]))
        self.assertEqual(3, board["active_count"])
        self.assertEqual(3, board["total_count"])
        self.assertEqual(0, board["completed_count"])
        self.assertEqual(0, self.store.completions(self.scope)["total"])
        self.assertTrue(all(q["status"] == "pending" and q["attempt_count"] == 0
                            for q in self.quests(board)))

    def test_multiple_empty_stages_fill_later_preserving_attempts_and_exact_identities(self):
        groups = self.groups(warmup=[self.beatmap(i) for i in range(1, 4)])
        before = self.create(groups)
        self.record(self.result(before["groups"][0]["quests"][0], accuracy=80, grade="B", misses=20))
        before = self.store.current(self.scope)
        empty_provider = Mock(return_value=self.groups())
        self.assertEqual(before, self.ensure(groups, empty_provider))
        empty_provider.assert_called_once()
        duplicate = self.beatmap(1, online=True, key="remote:1")
        practice = [self.beatmap(i, online=True) for i in range(10, 13)]
        challenge = [self.beatmap(i, online=True) for i in range(20, 23)]
        providers = [self.group("practice", [duplicate] + practice, max_online=3),
                     self.group("challenge", [deepcopy(practice[0])] + challenge, max_online=3)]
        after = self.ensure(groups, Mock(return_value=providers))
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(before["groups"][0], after["groups"][0])
        self.assertEqual([3, 3, 3], [len(group["quests"]) for group in after["groups"]])
        occupied = set()
        old_ids = {q["id"] for q in self.quests(before)}
        for quest in self.quests(after):
            self.assertFalse(occupied & map_tokens(quest["map"]))
            occupied.update(map_tokens(quest["map"]))
            if quest["id"] not in old_ids:
                self.assertEqual("pending", quest["status"])
                self.assertEqual(0, quest["attempt_count"])
                self.assertIsNone(quest["last_attempt"])
                self.assertNotIn("replaces_quest_id", quest)
                self.assertGreater(datetime.fromisoformat(quest["created_at"]),
                                   datetime.fromisoformat(self.assigned))
        self.assertEqual(9, after["active_count"])
        self.assertEqual(9, after["total_count"])
        self.assertEqual(0, after["retired_count"])
        self.assertEqual(0, after["completed_count"])
        self.reopen()
        unused = Mock(side_effect=AssertionError("A full board should not request a refill"))
        self.assertEqual(after, self.ensure(groups, unused))
        self.assertEqual(0, self.store.completions(self.scope)["total"])
        self.assertEqual(0, self.store.skips(self.scope)["total"])

    def test_waiting_completion_is_replaced_and_remaining_capacity_filled_once(self):
        groups = self.groups(warmup=[self.beatmap(1)])
        before = self.create(groups)
        old = before["groups"][0]["quests"][0]
        success = self.result(old)
        self.record(success)
        log = self.store.completions(self.scope)
        waiting = self.ensure(groups, Mock(return_value=self.groups()))
        self.assertEqual(1, waiting["waiting_count"])
        self.assertEqual(1, waiting["completed_count"])
        fresh = self.group("warmup", [self.beatmap(i, online=True) for i in range(10, 13)],
                           max_online=3)
        after = self.ensure(groups, Mock(return_value=[fresh]))
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(3, len(after["groups"][0]["quests"]))
        self.assertEqual(3, after["active_count"])
        self.assertEqual(0, after["waiting_count"])
        self.assertEqual(1, after["retired_count"])
        self.assertEqual(1, after["completed_count"])
        self.assertEqual(4, after["total_count"])
        replacements = [q for q in self.quests(after) if q.get("replaces_quest_id") == old["id"]]
        self.assertEqual(1, len(replacements))
        self.assertEqual(log, self.store.completions(self.scope))
        self.record(success)
        self.assertEqual(after, self.store.current(self.scope))
        self.assertEqual(log, self.store.completions(self.scope))

    def test_online_cap_can_expand_without_replacing_existing_online_mission(self):
        groups = self.groups(practice=[self.beatmap(1, online=True)])
        before = self.create(groups)
        candidates = [self.beatmap(i, online=True) for i in range(2, 5)]
        capped = self.group("practice", candidates)
        self.assertEqual(before, self.ensure(groups, Mock(return_value=[capped])))
        expanded = self.group("practice", candidates, max_online=3)
        after = self.ensure(groups, Mock(return_value=[expanded]))
        self.assertEqual(before["groups"][1]["quests"][0], after["groups"][1]["quests"][0])
        self.assertEqual(3, len(after["groups"][1]["quests"]))
        self.assertEqual(3, after["active_count"])
        self.assertEqual(0, after["completed_count"])
        self.assertEqual(after, self.ensure(groups, Mock(return_value=[capped])))

    def test_creation_preserves_prepared_order_and_deduplicates_ids_across_stages(self):
        remote = self.beatmap(1, online=True)
        local = [self.beatmap(i) for i in range(2, 5)]
        duplicate = self.beatmap(1, key="downloaded-checksum")
        other_difficulty = self.beatmap(5, set_id=remote["set_id"], title=remote["title"])
        groups = self.groups()
        groups[0] = self.group("warmup", [remote] + local, max_online=3)
        groups[1] = self.group("practice", [duplicate, other_difficulty])
        groups[2] = self.group("challenge", [self.beatmap(i, online=True) for i in range(10, 13)])
        board = self.create(groups)
        self.assertEqual([1, 2, 3], [q["map"]["id"] for q in board["groups"][0]["quests"]])
        self.assertEqual([5], [q["map"]["id"] for q in board["groups"][1]["quests"]])
        self.assertEqual([10], [q["map"]["id"] for q in board["groups"][2]["quests"]])
        self.assertEqual(5, board["total_count"])
        self.assertEqual(0, board["completed_count"])

    def test_skipped_slot_and_new_capacity_keep_skip_log_separate_from_completions(self):
        groups = self.groups(warmup=[self.beatmap(1)])
        before = self.create(groups)
        waiting = self.ensure(groups, Mock(return_value=self.groups()), skip=lambda quest: True)
        self.assertEqual(1, waiting["skipped_waiting_count"])
        log = self.store.skips(self.scope)
        fresh = self.group("warmup", [self.beatmap(i) for i in range(2, 5)])
        after = self.ensure(groups, Mock(return_value=[fresh]))
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(3, after["active_count"])
        self.assertEqual(0, after["skipped_waiting_count"])
        self.assertEqual(1, after["skipped_count"])
        self.assertEqual(4, after["total_count"])
        self.assertEqual(0, after["completed_count"])
        self.assertEqual(0, after["retired_count"])
        self.assertEqual(log, self.store.skips(self.scope))
        self.assertEqual(0, self.store.completions(self.scope)["total"])


if __name__ == "__main__":
    unittest.main()
