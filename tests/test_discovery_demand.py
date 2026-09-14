"""Demand discovery uses asynchronous, resumable batches and bounded retries."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from osu_coach.storage.discovery_store import DiscoveryStore, ERROR_RETRY_DELAY, EXHAUSTED_RETRY_DELAY
from osu_coach.settings import DEFAULTS


NOW = 2_000_000_000


def beatmap(identifier):
    return {"id": identifier, "key": f"remote:{identifier}", "set_id": identifier,
            "popularity": {"scope": "beatmapset", "rating": 9.0, "rating_votes": 25, "votes": 25, "play_count": 25000},
            "source": "online", "stars": 4.5, "title": f"Demand map {identifier}"}


def need(stage="practice", **changes):
    return {"stage": stage, "label": "Practice", "target": 4.5, "missing": 3,
            "min_stars": 4.2, "max_stars": 4.68, "max_bpm": 165,
            "max_ar": 8.7, "max_length": 164, **changes}


class DiscoveryDemandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fetch = Mock(return_value={"maps": [beatmap(10)], "next_cursor": {"page": 2},
                                       "exhausted": False})
        self.daily = Mock(side_effect=AssertionError("Demand must use the batch fetcher"))
        self.store = DiscoveryStore(self.temp.name, fetcher=self.daily, batch_fetcher=self.fetch,
                                    settings={**DEFAULTS, "discovery_batches_per_pass": 1})
        self.sample = {"player": "Demand fixture", "client": "lazer", "mode": 0,
                       "mods": [], "mod_key": '{"mods":[],"rate":1.0}'}
        self.needs = [need()]

    def tearDown(self):
        self.store.stop.set()
        if self.store.thread:
            self.store.thread.join(2)
        self.temp.cleanup()

    def sync(self, *, epoch=NOW, needs=None, sample=None, exclude_ids=(), force=False):
        with patch("osu_coach.storage.discovery_store.time.time", return_value=epoch):
            started = self.store.sync(4.5, self.sample if sample is None else sample,
                                      force=force, needs=self.needs if needs is None else needs,
                                      exclude_ids=exclude_ids)
            if self.store.thread:
                self.store.thread.join(2)
                self.assertFalse(self.store.thread.is_alive())
            return started

    def snapshot(self, epoch=NOW, needs=None, sample=None):
        with patch("osu_coach.storage.discovery_store.time.time", return_value=epoch):
            return self.store.snapshot([], self.sample if sample is None else sample,
                                       needs=self.needs if needs is None else needs)

    def reopen(self):
        self.store.stop.set()
        if self.store.thread:
            self.store.thread.join(2)
        self.store = DiscoveryStore(self.temp.name, fetcher=self.daily, batch_fetcher=self.fetch,
                                    settings={**DEFAULTS, "discovery_batches_per_pass": 1})

    @staticmethod
    def retry_epoch(snapshot):
        return datetime.fromisoformat(snapshot["next_retry"]).timestamp()

    def test_demand_bypasses_daily_cache_and_forwards_all_limits_and_exclusions(self):
        self.store.fetched_epoch = NOW - 1
        self.store.baseline = 4.5
        self.store.maps = [beatmap(5)]
        before = deepcopy(self.needs)
        self.assertTrue(self.sync(exclude_ids=(80, 81, 80)))
        args, kwargs = self.fetch.call_args
        self.assertEqual((3.8, 4.9), args)
        self.assertIsNone(kwargs["cursor"])
        self.assertEqual({5, 80, 81}, set(kwargs["exclude_ids"]))
        self.assertEqual([{"min_stars": 4.2, "max_stars": 4.68, "max_bpm": 165,
                           "max_ar": 8.7, "max_length": 164}], kwargs["requirements"])
        self.assertEqual(before, self.needs)
        self.assertEqual([5, 10], [m["id"] for m in self.store.candidates([])])
        state = self.snapshot()
        self.assertTrue(state["automatic"])
        self.assertEqual(before, state["needs"])
        state["needs"][0]["missing"] = 0
        self.assertEqual(before, self.needs)
        self.assertEqual(NOW + 60, self.retry_epoch(self.snapshot()))
        self.daily.assert_not_called()

    def test_request_returns_while_fetch_is_blocked_and_no_second_worker_can_start(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def fetch(*args, **kwargs):
            calls.append((args, kwargs))
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Test did not release the fetcher")
            return {"maps": [beatmap(10)], "next_cursor": {"page": 2}, "exhausted": False}

        self.store.batch_fetcher = fetch
        try:
            with patch("osu_coach.storage.discovery_store.time.time", return_value=NOW):
                self.assertTrue(self.store.sync(4.5, self.sample, needs=self.needs))
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.store.thread.is_alive())
                self.assertEqual("loading", self.store.snapshot([], needs=self.needs)["state"])
                self.assertFalse(self.store.sync(4.5, self.sample, force=True, needs=self.needs))
            with patch("osu_coach.storage.discovery_store.time.time", return_value=NOW + 61):
                self.assertFalse(self.store.sync(4.5, self.sample, force=True, needs=self.needs))
                release.set()
                self.store.thread.join(2)
            self.assertFalse(self.store.thread.is_alive())
            self.assertEqual(1, len(calls))
            self.assertEqual([beatmap(10)], self.store.candidates([]))
        finally:
            release.set()
            if self.store.thread:
                self.store.thread.join(2)

    def test_opaque_cursor_merge_and_batch_cooldown_survive_reopening(self):
        first = {"next": "opaque/+value==", "page": [2, {"marker": "exact"}]}
        second = {"next": "another-token", "page": 3}
        self.fetch.side_effect = [
            {"maps": [beatmap(10)], "next_cursor": first, "exhausted": False},
            {"maps": [beatmap(11)], "next_cursor": second, "exhausted": False},
            {"maps": [beatmap(12)], "next_cursor": None, "exhausted": True},
        ]
        self.assertTrue(self.sync())
        self.assertFalse(self.sync(epoch=NOW + 59, force=True))
        self.reopen()
        self.assertFalse(self.sync(epoch=NOW + 59, force=True))
        changed_count = [need(missing=1, label="Still practice")]
        self.assertTrue(self.sync(epoch=NOW + 60, needs=changed_count, exclude_ids=(99,)))
        self.assertEqual(first, self.fetch.call_args.kwargs["cursor"])
        self.assertEqual({10, 99}, set(self.fetch.call_args.kwargs["exclude_ids"]))
        self.assertEqual([10, 11], [m["id"] for m in self.store.candidates([])])
        persisted = json.loads(Path(self.temp.name, "discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(second, persisted["search"]["cursor"])
        self.reopen()
        self.assertFalse(self.sync(epoch=NOW + 119, force=True))
        self.assertTrue(self.sync(epoch=NOW + 120))
        self.assertEqual(second, self.fetch.call_args.kwargs["cursor"])
        self.assertEqual([10, 11, 12], [m["id"] for m in self.store.candidates([])])

    def test_exhausted_feed_waits_an_hour_after_reopening_then_restarts_from_beginning(self):
        self.fetch.return_value = {"maps": [], "next_cursor": {"last": True}, "exhausted": True}
        self.assertTrue(self.sync())
        state = self.snapshot()
        self.assertTrue(state["exhausted"])
        self.assertEqual(self.needs, state["needs"])
        self.assertEqual(NOW + EXHAUSTED_RETRY_DELAY, self.retry_epoch(state))
        self.reopen()
        self.assertFalse(self.sync(epoch=NOW + EXHAUSTED_RETRY_DELAY - 1, force=True))
        self.assertTrue(self.sync(epoch=NOW + EXHAUSTED_RETRY_DELAY))
        self.assertIsNone(self.fetch.call_args.kwargs["cursor"])
        self.assertEqual(2, self.fetch.call_count)

    def test_failed_batch_keeps_maps_and_cursor_and_persists_fifteen_minute_retry(self):
        self.assertTrue(self.sync())
        maps = deepcopy(self.store.maps)
        cursor = deepcopy(self.store.search["cursor"])
        self.fetch.side_effect = RuntimeError("Offline fixture")
        failed_at = NOW + 60
        self.assertTrue(self.sync(epoch=failed_at))
        self.assertEqual("error", self.store.status["state"])
        self.assertEqual(maps, self.store.maps)
        self.assertEqual(cursor, self.store.search["cursor"])
        persisted = json.loads(Path(self.temp.name, "discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(maps, persisted["maps"])
        self.assertEqual(cursor, persisted["search"]["cursor"])
        self.assertEqual(failed_at + ERROR_RETRY_DELAY, self.retry_epoch(self.snapshot(failed_at)))
        self.assertFalse(self.sync(epoch=failed_at + ERROR_RETRY_DELAY - 1, force=True))
        self.reopen()
        self.assertFalse(self.sync(epoch=failed_at + 1, force=True))
        self.assertEqual(failed_at + ERROR_RETRY_DELAY, self.retry_epoch(self.snapshot(failed_at + 1)))
        self.fetch.side_effect = None
        self.assertTrue(self.sync(epoch=failed_at + ERROR_RETRY_DELAY))
        self.assertEqual(cursor, self.fetch.call_args.kwargs["cursor"])
        self.assertEqual(maps, self.store.maps)

    def test_removing_duration_ceiling_revisits_previously_rejected_maps_after_restart(self):
        self.assertTrue(self.sync())
        self.reopen()
        expanded = [{key: value for key, value in item.items() if key != "max_length"} for item in self.needs]
        self.assertTrue(self.sync(epoch=NOW + 60, needs=expanded))
        self.assertIsNone(self.fetch.call_args.kwargs["cursor"])
        self.assertNotIn("max_length", self.fetch.call_args.kwargs["requirements"][0])
        self.assertEqual(165, self.fetch.call_args.kwargs["requirements"][0]["max_bpm"])

    def test_new_star_envelope_restarts_cursor_to_revisit_previously_rejected_maps(self):
        narrow = [need("warmup", target=4.2, min_stars=4.0, max_stars=4.3)]
        self.assertTrue(self.sync(needs=narrow))
        expanded = narrow + [need("challenge", target=4.6, min_stars=4.4, max_stars=4.78)]
        self.assertTrue(self.sync(epoch=NOW + 60, needs=expanded))
        self.assertIsNone(self.fetch.call_args.kwargs["cursor"])
        limits = self.fetch.call_args.kwargs["requirements"]
        self.assertEqual([(4.0, 4.3), (4.4, 4.78)],
                         [(item["min_stars"], item["max_stars"]) for item in limits])
        self.assertEqual(expanded, self.snapshot(NOW + 60, needs=expanded)["needs"])

    def test_small_star_changes_resume_until_their_accumulated_expansion_requires_a_restart(self):
        self.fetch.side_effect = [
            {"maps": [], "next_cursor": {"page": 2}, "exhausted": False},
            {"maps": [], "next_cursor": {"page": 3}, "exhausted": False},
            {"maps": [], "next_cursor": {"page": 4}, "exhausted": False},
        ]
        self.assertTrue(self.sync(needs=[need(max_stars=4.5)]))
        self.assertTrue(self.sync(epoch=NOW + 60, needs=[need(max_stars=4.56)]))
        self.assertEqual({"page": 2}, self.fetch.call_args.kwargs["cursor"])
        self.reopen()
        self.assertTrue(self.sync(epoch=NOW + 120, needs=[need(max_stars=4.62)]))
        self.assertIsNone(self.fetch.call_args.kwargs["cursor"])

    def test_profile_change_restarts_cursor_after_shared_rate_limit(self):
        self.assertTrue(self.sync())
        another = {**self.sample, "player": "Another fixture"}
        self.assertFalse(self.sync(epoch=NOW + 59, sample=another))
        self.assertTrue(self.sync(epoch=NOW + 60, sample=another))
        self.assertIsNone(self.fetch.call_args.kwargs["cursor"])

    def test_mods_pause_demand_without_losing_requirements_or_cached_state(self):
        self.assertTrue(self.sync())
        maps, search = deepcopy(self.store.maps), deepcopy(self.store.search)
        for sample in ({**self.sample, "mods": [{"acronym": "HD"}]},
                       {**self.sample, "mod_key": '{"mods":[],"rate":1.1}'}):
            with self.subTest(sample=sample):
                self.assertFalse(self.sync(epoch=NOW + 60, sample=sample, force=True))
                state = self.snapshot(NOW + 60, sample=sample)
                self.assertEqual("paused", state["state"])
                self.assertTrue(state["automatic"])
                self.assertEqual(self.needs, state["needs"])
                self.assertIsNone(state["next_retry"])
                self.assertEqual(0, state["candidate_count"])
                self.assertEqual(maps, self.store.maps)
                self.assertEqual(search, self.store.search)
        self.assertEqual(1, self.fetch.call_count)


    def test_unverified_legacy_maps_can_be_rediscovered_with_play_counts(self):
        legacy = beatmap(10)
        legacy["popularity"].pop("play_count")
        self.store.maps = [legacy, beatmap(5)]
        self.assertTrue(self.sync(exclude_ids=(80,)))
        self.assertEqual({5, 80}, set(self.fetch.call_args.kwargs["exclude_ids"]))
        self.assertEqual([5, 10], [m["id"] for m in self.store.candidates([])])


if __name__ == "__main__":
    unittest.main()
