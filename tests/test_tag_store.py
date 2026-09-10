"""Offline tests for tag evidence, cache lifetime and bounded synchronization."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tag_store import MAX_SETS_PER_SYNC, TTL_SECONDS, TagStore


NOW = 2_000_000_000


def beatmap(identifier, set_id, stars=4.5):
    return {"id": identifier, "set_id": set_id, "key": f"hash-{identifier}",
            "stars": stars, "title": f"Map {identifier}", "version": "Insane"}


def votes(name="skillset/jumps", count=8):
    return [{"id": 19, "name": name, "count": count, "source": "community_user_tags"}]


def record(maps, epoch=NOW):
    return {"fetched_at": "2033-05-18T03:33:20+00:00", "fetched_epoch": epoch,
            "maps": {str(identifier): tags for identifier, tags in maps.items()}}


class TagStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fetch = Mock(side_effect=AssertionError("Unexpected request"))
        self.store = TagStore(self.temp.name, fetcher=self.fetch)
        self.maps = [beatmap(100, 12), beatmap(101, 12, 4.8), beatmap(200, 20, 4.6)]

    def tearDown(self):
        self.store.stop.set()
        if self.store.thread:
            self.store.thread.join(2)
        self.temp.cleanup()

    def sync(self, maps=None, plays=None, force=False):
        with patch("tag_store.time.time", return_value=NOW), patch.object(self.store.stop, "wait", return_value=False):
            started = self.store.sync(self.maps if maps is None else maps, plays or [], 4.5, force=force)
            if self.store.thread:
                self.store.thread.join(2)
                self.assertFalse(self.store.thread.is_alive(), "Fake tag sync did not finish")
            return started

    def test_cache_survives_reload_and_avoids_fresh_network_requests(self):
        self.fetch.side_effect = lambda sid: {100: votes()} if sid == 12 else {200: []}
        self.assertTrue(self.sync())
        self.assertEqual([12, 20], [call.args[0] for call in self.fetch.call_args_list])
        restored = TagStore(self.temp.name, fetcher=self.fetch)
        self.assertEqual(restored.enrich([self.maps[0]])[0]["tags"][0]["name"], "skillset/jumps")
        with patch("tag_store.time.time", return_value=NOW + 60):
            self.assertFalse(restored.sync(self.maps, [], 4.5))
        self.assertEqual(self.fetch.call_count, 2)
        content = json.loads(Path(self.temp.name, "community-tags.json").read_text(encoding="utf-8"))
        self.assertEqual(content["version"], 1)
        self.assertEqual(set(content["sets"]), {"12", "20"})

    def test_tags_belong_to_exact_difficulty(self):
        self.store.sets = {"12": record({100: votes(), 101: []})}
        first, sibling, unknown = self.store.enrich(self.maps)
        self.assertEqual(first["tag_status"], "known")
        self.assertEqual(first["tags"][0]["provenance"], "community_user_tags")
        self.assertEqual(sibling["tags"], [])
        self.assertEqual(sibling["tag_status"], "none")
        self.assertEqual(unknown["tag_status"], "missing")

    def test_play_uuid_is_not_used_as_beatmap_id(self):
        self.store.sets = {"12": record({100: votes()})}
        play = {"id": "score-uuid", "beatmap_id": 100, "beatmap_key": "hash-100"}
        enriched = self.store.enrich([play])[0]
        self.assertEqual(enriched["id"], "score-uuid")
        self.assertEqual(enriched["tag_status"], "known")
        self.assertEqual(enriched["tags"][0]["name"], "skillset/jumps")

    def test_discovery_tag_check_is_preserved_without_a_local_cache_entry(self):
        maps = [{**self.maps[0], "source": "online", "tags": [], "tag_status": state}
                for state in ("none", "weak")]
        self.assertEqual([m["tag_status"] for m in self.store.enrich(maps)], ["none", "weak"])

    def test_votes_below_five_are_weak_and_excluded_from_training_tags(self):
        self.store.sets = {"12": record({100: votes(count=4), 101: votes(count=5)})}
        weak, accepted = self.store.enrich(self.maps[:2])
        self.assertEqual(weak["tags"], [])
        self.assertEqual(weak["tag_status"], "weak")
        self.assertEqual(accepted["tag_status"], "known")
        self.assertEqual(accepted["tags"][0]["count"], 5)
        self.assertEqual(self.store.sets["12"]["maps"]["100"][0]["count"], 4)

    def test_mixed_votes_keep_only_supported_tags_and_do_not_mutate_input(self):
        tags = votes(count=12) + [{"id": 25, "name": "streams/bursts", "count": 1}]
        self.store.sets = {"12": record({100: tags})}
        original = copy.deepcopy(self.maps)
        enriched = self.store.enrich(self.maps)
        self.assertEqual([tag["name"] for tag in enriched[0]["tags"]], ["skillset/jumps"])
        self.assertEqual(self.maps, original)
        self.assertEqual(self.store.sets["12"]["maps"]["100"], tags)

    def test_recent_sets_take_priority_in_descending_play_order(self):
        maps = [beatmap(1, 1, 4.5), beatmap(2, 2, 8.0), beatmap(3, 3, 2.0)]
        plays = [
            {"id": "old-score", "beatmap_id": 2, "played_at": "2026-09-08T12:00:00Z"},
            {"id": "new-score", "beatmap_id": 3, "played_at": "2026-09-08T12:05:00Z"},
        ]
        with patch("tag_store.time.time", return_value=NOW):
            self.assertEqual(self.store.plan(maps, plays, 4.5), [3, 2, 1])

    def test_plan_resolves_checksum_and_deduplicates_sibling_difficulties(self):
        plays = [{"beatmap_key": "hash-101", "played_at": "2026-09-08T12:00:00Z"}]
        with patch("tag_store.time.time", return_value=NOW):
            self.assertEqual(self.store.plan(self.maps, plays, 4.5), [12, 20])

    def test_ttl_boundary_and_force_refresh(self):
        self.store.sets = {"12": record({100: []}), "20": record({200: []}, NOW - TTL_SECONDS)}
        with patch("tag_store.time.time", return_value=NOW):
            self.assertEqual(self.store.plan(self.maps, [], 4.5), [20])
            self.assertEqual(self.store.plan(self.maps, [], 4.5, force=True), [12, 20])
        with patch("tag_store.time.time", return_value=NOW + TTL_SECONDS):
            self.assertEqual(self.store.plan(self.maps, [], 4.5), [12, 20])

    def test_single_sync_caps_requests_at_forty_sets(self):
        maps = [beatmap(i, i) for i in range(1, 70)]
        self.fetch.side_effect = lambda sid: {sid: []}
        self.assertTrue(self.sync(maps=maps))
        self.assertEqual(self.fetch.call_count, MAX_SETS_PER_SYNC)
        self.assertEqual(MAX_SETS_PER_SYNC, 40)
        self.assertEqual(self.store.status["processed"], 40)
        self.assertEqual(len(self.store.sets), 40)

    def test_invalid_and_out_of_range_maps_are_not_requested(self):
        maps = [beatmap(1, 0), beatmap(2, -1), beatmap(3, "12"), beatmap(4, 4, 7)]
        self.assertFalse(self.sync(maps=maps))
        self.fetch.assert_not_called()

    def test_network_failure_preserves_stale_cache_and_other_successes(self):
        before = record({100: votes()}, NOW - TTL_SECONDS - 1)
        self.store.sets = {"12": copy.deepcopy(before)}
        self.store._save()
        self.fetch.side_effect = [RuntimeError("Network unavailable"), {200: votes("streams/bursts")}]
        self.assertTrue(self.sync())
        self.assertEqual(self.store.status["state"], "error")
        self.assertEqual(self.store.sets["12"], before)
        self.assertEqual(self.store.enrich([self.maps[0]])[0]["tags"][0]["name"], "skillset/jumps")
        self.assertIn("20", self.store.sets)
        restored = TagStore(self.temp.name, fetcher=self.fetch)
        self.assertEqual(restored.sets["12"], before)

    def test_three_upstream_errors_stop_the_batch(self):
        maps = [beatmap(i, i) for i in range(1, 15)]
        self.fetch.side_effect = RuntimeError("Network unavailable")
        self.assertTrue(self.sync(maps=maps))
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(self.store.status["state"], "error")
        self.assertEqual(self.store.status["processed"], 3)

    def test_enrich_and_snapshot_reads_do_not_trigger_network(self):
        self.store.sets = {"12": record({100: votes()})}
        for _ in range(10):
            self.store.enrich(self.maps)
            self.store.snapshot(self.maps)
        self.fetch.assert_not_called()

    def test_snapshot_distinguishes_skill_evidence_weak_votes_and_empty_maps(self):
        maps = self.maps + [beatmap(300, 30), beatmap(400, 40)]
        self.store.sets = {"12": record({100: votes(), 101: votes(count=1)}),
                           "20": record({200: []}),
                           "30": record({300: votes("expression/simple")})}
        state = self.store.snapshot(maps)
        self.assertEqual(state["catalog_maps"], 5)
        self.assertEqual(state["known_maps"], 4)
        self.assertEqual(state["tagged_maps"], 1)
        self.assertEqual(state["weak_votes_maps"], 1)

    def test_failure_cooldown_blocks_normal_and_forced_repeat_requests(self):
        self.fetch.side_effect = RuntimeError("Network unavailable")
        self.assertTrue(self.sync(maps=self.maps[:1]))
        self.assertEqual(self.store.next_retry_at, NOW + 60)
        with patch("tag_store.time.time", return_value=NOW + 59):
            self.assertFalse(self.store.sync(self.maps[:1], [], 4.5))
            self.assertFalse(self.store.sync(self.maps[:1], [], 4.5, force=True))
        self.assertEqual(self.fetch.call_count, 1)
        self.fetch.side_effect = lambda sid: {100: votes()}
        with patch("tag_store.time.time", return_value=NOW + 60):
            self.assertTrue(self.store.sync(self.maps[:1], [], 4.5))
            self.store.thread.join(2)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.store.status["state"], "ready")

    def test_sync_is_not_started_while_loading_or_after_stop(self):
        self.store.status["state"] = "loading"
        self.assertFalse(self.sync(force=True))
        self.store.status["state"] = "ready"
        self.store.stop.set()
        self.assertFalse(self.sync(force=True))
        self.fetch.assert_not_called()

    def test_broken_json_cache_can_be_refreshed(self):
        self.store.path.write_text("{broken", encoding="utf-8")
        restored = TagStore(self.temp.name, fetcher=self.fetch)
        self.assertEqual(restored.status["state"], "error")
        self.assertEqual(restored.enrich(self.maps)[0]["tag_status"], "missing")
        self.store = restored
        self.fetch.side_effect = lambda sid: {100 if sid == 12 else 200: votes()}
        self.assertTrue(self.sync())
        self.assertEqual(self.store.status["state"], "ready")


if __name__ == "__main__":
    unittest.main()
