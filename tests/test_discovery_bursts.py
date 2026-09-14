"""Rapid discovery resumes between batches, respects backoffs and stops on actual coverage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from osu_coach.settings import DEFAULTS
from osu_coach.storage.discovery_store import DiscoveryStore, ERROR_RETRY_DELAY, EXHAUSTED_RETRY_DELAY
from osu_coach.integrations.discovery_source import MIN_REQUEST_INTERVAL
from tests.test_discovery_demand import beatmap, need, NOW
from tests.test_discovery_integration import beatmap as playable_map
from tests import test_auto_discovery_integration as fixtures


class DiscoveryBurstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = {**DEFAULTS, "discovery_batches_per_pass": 3, "discovery_retry_minutes": 4}
        self.fetch = Mock(side_effect=lambda *args, **kwargs: {
            "maps": [], "next_cursor": {"page": self.fetch.call_count + 1}, "exhausted": False})
        self.store = DiscoveryStore(self.temp.name, batch_fetcher=self.fetch, settings=self.settings)

    def tearDown(self):
        self.store.stop.set()
        if self.store.thread:
            self.store.thread.join(2)
        self.temp.cleanup()

    def sync(self, epoch, **kwargs):
        with patch("osu_coach.storage.discovery_store.time.time", return_value=epoch):
            started = self.store.sync(4.5, **{"needs": [need()], **kwargs})
            if self.store.thread:
                self.store.thread.join(2)
                self.assertFalse(self.store.thread.is_alive())
            return started

    def reopen(self):
        self.store = DiscoveryStore(self.temp.name, batch_fetcher=self.fetch, settings=self.settings)

    def test_empty_batches_continue_quickly_and_group_pause_survives_restart(self):
        self.assertTrue(self.sync(NOW))
        self.assertEqual(NOW + MIN_REQUEST_INTERVAL, self.store.demand_retry_at)
        self.assertTrue(self.store.snapshot([], needs=[need()])["continuing"])
        self.assertFalse(self.sync(NOW + 1))
        self.assertTrue(self.sync(NOW + 2))
        self.assertEqual({"page": 2}, self.fetch.call_args.kwargs["cursor"])
        self.reopen()
        self.assertTrue(self.sync(NOW + 4))
        self.assertEqual({"page": 3}, self.fetch.call_args.kwargs["cursor"])
        self.assertEqual(3, self.store.snapshot([], needs=[need()])["batches_completed"])
        self.assertFalse(self.store.snapshot([], needs=[need()])["continuing"])
        self.assertEqual(NOW + 244, self.store.demand_retry_at)
        self.reopen()
        self.assertFalse(self.sync(NOW + 243, force=True))
        self.assertTrue(self.sync(NOW + 244))
        self.assertEqual({"page": 4}, self.fetch.call_args.kwargs["cursor"])
        self.assertEqual(1, self.store.search["pass_batches"])
        self.assertEqual(NOW + 244 + MIN_REQUEST_INTERVAL, self.store.demand_retry_at)

    def test_default_group_runs_ten_batches_but_coverage_returns_to_daily_schedule(self):
        self.store.set_settings(dict(DEFAULTS))
        for index in range(10):
            self.assertTrue(self.sync(NOW + index * 2))
        self.assertEqual(10, self.fetch.call_count)
        self.assertEqual(NOW + 18 + 60, self.store.demand_retry_at)
        self.assertFalse(self.sync(NOW + 80, needs=[]))
        self.assertFalse(self.store.snapshot([], needs=[])["continuing"])
        self.assertTrue(self.sync(NOW + 18 + 86400, needs=[]))
        self.assertEqual(0, self.store.search["pass_batches"])
        self.assertFalse(self.store.search["fast_continue"])

    def test_failure_after_partial_success_preserves_cursor_and_backoff(self):
        self.fetch.side_effect = [
            {"maps": [beatmap(10)], "next_cursor": {"page": 2}, "exhausted": False},
            RuntimeError("network fixture"),
            {"maps": [beatmap(20)], "next_cursor": {"page": 3}, "exhausted": False},
        ]
        self.assertTrue(self.sync(NOW))
        saved = Path(self.temp.name, "discovery.json").read_bytes()
        self.assertTrue(self.sync(NOW + 2))
        self.assertEqual(saved, Path(self.temp.name, "discovery.json").read_bytes())
        self.assertEqual("error", self.store.snapshot([], needs=[need()])["state"])
        self.assertFalse(self.store.snapshot([], needs=[need()])["continuing"])
        self.reopen()
        self.assertFalse(self.sync(NOW + 2 + ERROR_RETRY_DELAY - 1, force=True))
        self.assertTrue(self.sync(NOW + 2 + ERROR_RETRY_DELAY))
        self.assertEqual({"page": 2}, self.fetch.call_args.kwargs["cursor"])
        self.assertEqual([10, 20], [item["id"] for item in self.store.maps])

    def test_exhaustion_ends_fast_group_before_limit_and_honors_hour_pause(self):
        self.fetch.side_effect = [
            {"maps": [], "next_cursor": {"page": 2}, "exhausted": False},
            {"maps": [], "next_cursor": None, "exhausted": True},
        ]
        self.assertTrue(self.sync(NOW))
        self.assertTrue(self.sync(NOW + 2))
        self.assertTrue(self.store.search["exhausted"])
        self.assertFalse(self.store.search["fast_continue"])
        self.reopen()
        self.assertFalse(self.sync(NOW + 2 + EXHAUSTED_RETRY_DELAY - 1, force=True))
        self.assertEqual(2, self.fetch.call_count)

    def test_settings_change_during_batch_cancels_old_results_and_wakes_scheduler(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test fetch was not released")
            return {"maps": [beatmap(10)], "next_cursor": {"page": 2}, "exhausted": False}
        self.store.batch_fetcher = fetch
        try:
            self.assertTrue(self.store.sync(4.5, needs=[need()]))
            self.assertTrue(entered.wait(2))
            self.store.set_settings({**self.settings, "discovery_enabled": False})
            self.assertTrue(self.store.changed.is_set())
            release.set()
            self.store.thread.join(2)
            self.assertEqual([], self.store.maps)
            self.assertFalse(self.store.sync(4.5, needs=[need()]))
            self.assertEqual("paused", self.store.snapshot([], needs=[need()])["state"])
        finally:
            release.set()

    def test_single_batch_setting_preserves_configured_pause(self):
        self.store.set_settings({**self.settings, "discovery_batches_per_pass": 1})
        self.assertTrue(self.sync(NOW))
        self.assertEqual(NOW + 240, self.store.demand_retry_at)
        self.assertFalse(self.sync(NOW + 239, force=True))


class DiscoverySchedulerTests(unittest.TestCase):
    def test_closed_panel_fills_missions_then_reserve_and_stops_fast_search(self):
        fixture = fixtures.AutomaticDiscoveryTests()
        fixture.setUp()
        coach = fixture.coach
        scheduler = None
        finished = threading.Event()
        errors = []
        try:
            initial = coach.state()
            frozen = deepcopy(fixture.quests(initial))
            reserve = ([playable_map(i + 300, online=True, stars=4.15, bpm=150) for i in range(6)]
                       + [playable_map(i + 400, online=True, stars=4.6, bpm=150) for i in range(12)])
            fixture.fetch.side_effect = [
                {"maps": fixture.remote, "next_cursor": {"page": 4}, "exhausted": False},
                {"maps": reserve, "next_cursor": {"page": 7}, "exhausted": False},
            ]
            real_state = coach.state
            observations = []
            def observe():
                state = real_state()
                observations.append(deepcopy(state["discovery"]))
                if not state["discovery"]["search_needs"]:
                    finished.set()
                return state
            coach.state = observe
            coach.background_enabled = True
            def schedule():
                try:
                    coach.discover_periodically()
                except BaseException as exc:
                    errors.append(exc)
                    finished.set()
            with patch("osu_coach.storage.discovery_store.MIN_REQUEST_INTERVAL", .01):
                scheduler = threading.Thread(target=schedule)
                scheduler.start()
                self.assertTrue(finished.wait(5), "Scheduler should continue without browser requests")
                self.assertEqual([], errors)
                # A filled board with an empty reserve must not stop the burst.
                self.assertTrue(any(not d["needs"] and d["search_needs"] for d in observations))
                coach.sync_discovery()
                state = coach.state()
                self.assertEqual([], state["discovery"]["search_needs"])
                self.assertIn("frecuencia habitual", state["discovery"]["message"])
                self.assertEqual([6, 6, 6], [item["available"] for item in state["discovery"]["reserve"]])
                self.assertEqual(2, fixture.fetch.call_count)
                self.assertEqual({"page": 4}, fixture.fetch.call_args.kwargs["cursor"])
                current = {q["id"]: q for q in fixture.quests(state)}
                for quest in frozen:
                    self.assertEqual(quest, current[quest["id"]])
                self.assertEqual(9, len(current))
                self.assertEqual(0, state["quest_completions"]["total"])
                coach.close()
                scheduler.join(2)
                self.assertFalse(scheduler.is_alive(), "Shutdown should wake an idle scheduler")
        finally:
            coach.close()
            if scheduler:
                scheduler.join(2)
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
