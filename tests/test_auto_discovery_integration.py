"""Scarcity requests suitable online maps and refills frozen missions."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest.mock import Mock

from osu_coach.app import Coach, DEFAULT_MOD_KEY
from osu_coach.core.engine import recommend
from tests.test_discovery_integration import beatmap, profile


class AutomaticDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.coach = Coach(argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None,
                           no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2"))
        now = datetime.now(timezone.utc)
        self.coach.config["since"] = (now - timedelta(days=1)).isoformat()
        for i in range(5):
            self.coach.add_play({"id": f"seed-{i}", "beatmap_key": f"seed-map-{i}", "beatmap_id": 9000+i,
                "played_at": (now - timedelta(minutes=20-i)).isoformat(), "player": "Automatic fixture",
                "client": "lazer", "mode": 0, "mods": [], "mod_key": DEFAULT_MOD_KEY, "stars": 4.5,
                "accuracy": 98, "misses": 0, "passed": True, "completion": 1, "grade": "S",
                "max_combo": 400, "map_max_combo": 400, "object_count": 300, "judged_objects": 300,
                "bpm": 150, "ar": 8, "length": 120})
        self.coach.catalog = [beatmap(i+1, stars=4.15, bpm=150) for i in range(3)]
        self.remote = [beatmap(i+100, online=True, stars=4.6, bpm=150) for i in range(6)]
        self.fetch = Mock(return_value={"maps": self.remote, "next_cursor": {"page": 2}, "exhausted": False})
        self.coach.discovery_store.batch_fetcher = self.fetch

    def tearDown(self):
        self.coach.close()
        if self.coach.discovery_store.thread:
            self.coach.discovery_store.thread.join(3)
        self.coach.db.close()
        self.temp.cleanup()

    @staticmethod
    def quests(state):
        return [q for g in state["quest_board"]["groups"] for q in g["quests"]]

    def test_empty_stages_start_search_despite_daily_cache_and_refill_on_arrival(self):
        before = self.coach.state()
        self.assertEqual([3, 0, 0], [len(g["quests"]) for g in before["quest_board"]["groups"]])
        saved = deepcopy(self.quests(before))
        self.coach.discovery_store.fetched_epoch = datetime.now(timezone.utc).timestamp()
        self.coach.discovery_store.baseline = before["profile"]["baseline"]
        self.coach.background_enabled = True
        self.coach.state()
        self.coach.discovery_store.thread.join(3)
        self.assertFalse(self.coach.discovery_store.thread.is_alive())
        after = self.coach.state()
        self.assertEqual([3, 3, 3], [len(g["quests"]) for g in after["quest_board"]["groups"]])
        self.assertEqual(before["quest_board"]["id"], after["quest_board"]["id"])
        current = {q["id"]: q for q in self.quests(after)}
        for quest in saved:
            self.assertEqual(quest, current[quest["id"]])
        self.assertEqual(6, sum(q["map"]["source"] == "online" for q in current.values()))
        self.assertTrue(all(q["map"]["expectation"] for q in current.values()))
        self.assertEqual(0, after["quest_completions"]["total"])
        self.assertEqual([], after["discovery"]["needs"])
        self.assertEqual(1, self.fetch.call_count)
        args = self.fetch.call_args.kwargs
        self.assertTrue({1, 2, 3, *range(9000, 9005)} <= set(args["exclude_ids"]))
        self.assertEqual(2, len(args["requirements"]))
        for requirement in args["requirements"]:
            self.assertEqual(165, requirement["max_bpm"])
            self.assertEqual(8.7, requirement["max_ar"])
            self.assertEqual(164, requirement["max_length"])

    def test_unsuitable_online_results_leave_needs_and_respect_cooldown(self):
        self.fetch.return_value["maps"] = [dict(m, ar=11) for m in self.remote]
        self.coach.background_enabled = True
        self.coach.state()
        self.coach.discovery_store.thread.join(3)
        state = self.coach.state()
        self.assertEqual([3, 0, 0], [len(g["quests"]) for g in state["quest_board"]["groups"]])
        self.assertEqual(2, len(state["discovery"]["needs"]))
        self.assertTrue(state["discovery"]["next_retry"])
        self.assertEqual(1, self.fetch.call_count)

    def test_online_capacity_only_expands_when_local_options_are_missing(self):
        for count in (0, 1, 2, 3):
            with self.subTest(local=count):
                maps = [beatmap(i+1) for i in range(count)] + [beatmap(i+100, online=True) for i in range(5)]
                group = recommend(maps, profile(), stages={"practice"}, fill_online=True)[0]
                self.assertEqual(3, len(group["maps"]))
                online = sum(m["source"] == "online" for m in group["maps"])
                self.assertEqual(max(1, 3-count), online)
                self.assertEqual(max(1, 3-count), group["max_online"])
