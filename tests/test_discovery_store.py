"""Discovery metadata cache, cadence and profile boundaries without a network."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from osu_coach.storage.discovery_store import DiscoveryStore, INTERVAL, RETRY_DELAY, ERROR_RETRY_DELAY, compatible


NOW = 2_000_000_000


def beatmap(identifier=10):
    return {"id": identifier, "set_id": 100, "key": f"online-{identifier}",
            "popularity": {"scope": "beatmapset", "rating": 9.0, "rating_votes": 25, "votes": 25, "play_count": 25000},
            "source": "online", "stars": 4.5, "title": f"Song {identifier}",
            "tags": [{"name": "skillset/jumps"}]}


class DiscoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fetch = Mock(return_value=[beatmap()])
        self.store = DiscoveryStore(self.temp.name, fetcher=self.fetch)

    def tearDown(self):
        self.store.stop.set()
        if self.store.thread:
            self.store.thread.join(2)
        self.temp.cleanup()

    def sync(self, baseline=4.5, sample=None, force=False, epoch=NOW):
        with patch("osu_coach.storage.discovery_store.time.time", return_value=epoch):
            started = self.store.sync(baseline, sample, force=force)
            if self.store.thread:
                self.store.thread.join(2)
                self.assertFalse(self.store.thread.is_alive())
            return started

    def test_fetch_range_and_persistence_survive_reopening(self):
        self.assertTrue(self.sync())
        self.fetch.assert_called_once_with(3.8, 4.9)
        restored = DiscoveryStore(self.temp.name, fetcher=self.fetch)
        self.assertEqual(restored.candidates([]), [beatmap()])
        self.assertEqual(restored.fetched_epoch, NOW)
        self.assertEqual(restored.baseline, 4.5)
        with patch("osu_coach.storage.discovery_store.time.time", return_value=NOW + 61):
            self.assertFalse(restored.sync(4.5))
        self.assertEqual(self.fetch.call_count, 1)
        persisted = json.loads(Path(self.temp.name, "discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["version"], 1)

    def test_daily_cache_refreshes_at_exact_24_hour_boundary(self):
        self.assertEqual(INTERVAL, 24 * 3600)
        self.assertTrue(self.sync())
        self.assertFalse(self.sync(epoch=NOW + INTERVAL - 1))
        self.assertTrue(self.sync(epoch=NOW + INTERVAL))
        self.assertEqual(self.fetch.call_count, 2)

    def test_force_refresh_still_respects_short_cooldown(self):
        self.assertTrue(self.sync())
        self.assertFalse(self.sync(force=True, epoch=NOW + RETRY_DELAY - 1))
        self.assertTrue(self.sync(force=True, epoch=NOW + RETRY_DELAY))
        self.assertEqual(self.fetch.call_count, 2)

    def test_level_change_waits_an_hour_and_requires_half_a_star(self):
        self.assertTrue(self.sync(baseline=3.6))
        self.assertFalse(self.sync(baseline=4.1, epoch=NOW + 3599))
        self.assertFalse(self.sync(baseline=4.09, epoch=NOW + 3600))
        self.assertTrue(self.sync(baseline=4.1, epoch=NOW + 3600))
        self.assertEqual(self.fetch.call_count, 2)

    def test_failure_preserves_last_results_and_enforces_retry_cooldown(self):
        self.assertTrue(self.sync())
        before = Path(self.temp.name, "discovery.json").read_text(encoding="utf-8")
        self.fetch.side_effect = RuntimeError("Offline")
        self.assertTrue(self.sync(epoch=NOW + INTERVAL))
        self.assertEqual(self.store.candidates([]), [beatmap()])
        self.assertEqual(self.store.status["state"], "error")
        self.assertEqual(before, Path(self.temp.name, "discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(ERROR_RETRY_DELAY, 15 * 60)
        self.assertIn("15 minutos", self.store.status["message"])
        self.assertFalse(self.sync(epoch=NOW + INTERVAL + ERROR_RETRY_DELAY - 1, force=True))
        self.fetch.side_effect = None
        self.assertTrue(self.sync(epoch=NOW + INTERVAL + ERROR_RETRY_DELAY))
        self.assertEqual(self.store.status["state"], "ready")

    def test_invalid_fetch_result_preserves_previous_cache(self):
        self.assertTrue(self.sync())
        self.fetch.return_value = {"maps": []}
        self.assertTrue(self.sync(force=True, epoch=NOW + RETRY_DELAY))
        self.assertEqual(self.store.maps, [beatmap()])
        self.assertEqual(self.store.status["state"], "error")

    def test_installed_ids_are_excluded_even_if_numeric_strings(self):
        self.store.maps = [beatmap(10), beatmap("11"), beatmap(12)]
        result = self.store.candidates([{"id": "10"}, {"id": 11}])
        self.assertEqual([item["id"] for item in result], [12])

    def test_candidates_return_deep_copies_without_mutating_catalog(self):
        self.store.maps = [beatmap()]
        local = [{"id": 999, "tags": [{"name": "skillset/streams"}]}]
        before = deepcopy(local)
        result = self.store.candidates(local)
        result[0]["tags"].append({"name": "changed"})
        self.assertEqual(self.store.maps, [beatmap()])
        self.assertEqual(local, before)

    def test_lazer_only_maps_are_not_mixed_into_stable_profile(self):
        lazer_map = dict(beatmap(10), lazer_only=True)
        universal_map = beatmap(11)
        self.store.maps = [lazer_map, universal_map]
        self.assertEqual(self.store.candidates([], {"client": "stable", "mods": []}), [universal_map])
        self.assertEqual(self.store.candidates([], {"client": "lazer", "mods": []}), [lazer_map, universal_map])
        self.assertEqual(self.store.candidates([]), [lazer_map, universal_map])

    def test_reads_do_not_fetch_and_snapshot_describes_daily_schedule(self):
        self.store.maps = [beatmap()]
        self.store.fetched_epoch = NOW
        state = self.store.snapshot([])
        self.store.candidates([])
        self.fetch.assert_not_called()
        self.assertEqual(state["interval_hours"], 24)
        self.assertEqual(state["candidate_count"], 1)
        self.assertTrue(state["last_updated"])
        self.assertTrue(state["next_update"])

    def test_mods_or_altered_rate_pause_sync_and_candidates(self):
        self.store.maps = [beatmap()]
        samples = [
            {"mods": [{"acronym": "HD"}], "mod_key": '{"mods":[{"acronym":"HD"}],"rate":1}'},
            {"mods": [], "mod_key": '{"mods":[],"rate":1.1}'},
            {"mods": [], "mod_key": '{"mods":[{"acronym":"HR"}],"rate":1}'},
            {"mods": [], "mod_key": "broken"},
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertFalse(compatible(sample))
                self.assertFalse(self.sync(sample=sample, force=True))
                self.assertEqual(self.store.candidates([], sample), [])
                self.assertEqual(self.store.snapshot([], sample)["state"], "paused")
        self.fetch.assert_not_called()
        self.assertTrue(compatible({"mods": [], "mod_key": '{"mods":[],"rate":1}'}))

    def test_stopped_or_busy_store_cannot_start_another_request(self):
        self.store.status["state"] = "loading"
        self.assertFalse(self.sync(force=True))
        self.store.status["state"] = "ready"
        self.store.stop.set()
        self.assertFalse(self.sync(force=True))
        self.fetch.assert_not_called()

    def test_stop_during_fetch_does_not_publish_results(self):
        started, release = threading.Event(), threading.Event()
        def fetch(lower, upper):
            started.set()
            self.assertTrue(release.wait(2))
            return [beatmap()]
        self.store.fetcher = fetch
        try:
            with patch("osu_coach.storage.discovery_store.time.time", return_value=NOW):
                self.assertTrue(self.store.sync(4.5))
                self.assertTrue(started.wait(2))
                self.store.stop.set()
                release.set()
                self.store.thread.join(2)
            self.assertFalse(self.store.thread.is_alive())
            self.assertEqual(self.store.maps, [])
            self.assertFalse(Path(self.temp.name, "discovery.json").exists())
        finally:
            release.set()

    def test_corrupt_cache_can_be_refreshed_without_crashing_snapshot(self):
        for bad in ("{broken", json.dumps({"version": 1, "maps": ["bad"]}),
                    json.dumps({"version": 1, "maps": [], "fetched_epoch": float("nan")})):
            with self.subTest(cache=bad):
                Path(self.temp.name, "discovery.json").write_text(bad, encoding="utf-8")
                restored = DiscoveryStore(self.temp.name, fetcher=self.fetch)
                self.assertEqual(restored.snapshot([])["state"], "error")
                with patch("osu_coach.storage.discovery_store.time.time", return_value=NOW):
                    self.assertTrue(restored.sync(4.5))
                    restored.thread.join(2)
                self.assertEqual(restored.status["state"], "ready")


    def test_cached_maps_need_rating_votes_and_play_count_together(self):
        good = beatmap(1)
        bad = []
        for i, changes in enumerate(({"rating": 7.99}, {"votes": 9, "rating_votes": 9},
                                     {"play_count": 9999}, {"play_count": None}), 2):
            item = beatmap(i)
            item["popularity"].update(changes, favourites=999999)
            bad.append(item)
        legacy = beatmap(9)
        del legacy["popularity"]
        self.store.maps = [good, *bad, legacy]
        self.assertEqual([good], self.store.candidates([]))
        self.assertEqual(1, self.store.snapshot([])["candidate_count"])

    def test_new_fetch_cannot_preserve_or_publish_unqualified_candidates(self):
        legacy = beatmap(20)
        legacy["popularity"].pop("play_count")
        low = beatmap(30)
        low["popularity"]["rating"] = 7
        self.store.maps = [legacy]
        self.fetch.return_value = [low, beatmap()]
        self.assertTrue(self.sync())
        self.assertEqual([beatmap()], self.store.maps)
        restored = DiscoveryStore(self.temp.name)
        self.assertEqual([beatmap()], restored.candidates([]))

    def test_policy_upgrade_revisits_legacy_cache_without_daily_delay(self):
        self.assertTrue(self.sync())
        path = Path(self.temp.name, "discovery.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        del value["quality_policy"]
        value["maps"][0]["popularity"].pop("play_count")
        value["search"] = {"cursor": {"page": 15}, "next_retry_at": NOW + 3600}
        path.write_text(json.dumps(value), encoding="utf-8")
        restored = DiscoveryStore(self.temp.name, fetcher=self.fetch)
        self.assertEqual([], restored.candidates([]))
        self.assertEqual({}, restored.search)
        self.assertEqual(0, restored.demand_retry_at)
        with patch("osu_coach.storage.discovery_store.time.time", return_value=NOW + 1):
            self.assertTrue(restored.sync(4.5))
            restored.thread.join(2)
        self.assertEqual([beatmap()], restored.candidates([]))

    def test_snapshot_explains_joint_thresholds_without_sharing_mutable_policy(self):
        policy = self.store.snapshot([])["quality_policy"]
        self.assertEqual({"min_rating": 8, "min_votes": 10, "min_play_count": 10000,
                          "scope": "beatmapset"}, policy)
        policy["min_play_count"] = 0
        self.assertEqual(10000, self.store.snapshot([])["quality_policy"]["min_play_count"])


if __name__ == "__main__":
    unittest.main()
