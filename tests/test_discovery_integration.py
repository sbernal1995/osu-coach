"""Online metadata joins local recommendations without bypassing fit filters."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from osu_coach.app import Coach, Handler, ThreadingHTTPServer
from osu_coach.core import engine


def beatmap(index, *, online=False, stars=3.0, **changes):
    result = {"key": f"map-{index}", "id": index, "set_id": index + 1000,
              "title": f"Song {index}", "artist": "Artist", "version": "Insane",
              "mode": 0, "stars": stars, "bpm": 170, "ar": 8, "length": 100,
              "object_count": 300, "max_combo": 400, "tags": [],
              "source": "online" if online else "local", "local": not online}
    if online:
        result["popularity"] = {"scope": "beatmapset", "rating": 9.0, "rating_votes": 25, "votes": 25, "play_count": 25000}
    result.update(changes)
    return result


def profile():
    return {"baseline": 3, "phase": "calibrating", "challenge_unlocked": False,
            "window": [{"id": "recent", "beatmap_key": "played", "stars": 3,
                        "played_at": datetime.now(timezone.utc).isoformat(),
                        "accuracy": 97, "passed": True, "completion": 1,
                        "misses": 1, "object_count": 300, "max_combo": 390,
                        "map_max_combo": 400, "client": "lazer", "mods": [],
                        "bpm": 170, "ar": 8, "length": 100}]}


def candidates():
    return ([beatmap(i + 1, stars=2.5 + (i % 10) * .08) for i in range(30)]
            + [beatmap(i + 101, online=True, stars=2.5 + (i % 10) * .08) for i in range(30)])


def flattened(groups):
    return [beatmap for group in groups for beatmap in group["maps"]]


class DiscoveryRecommendationTests(unittest.TestCase):
    def test_each_stage_mixes_two_local_maps_and_at_most_one_online_map(self):
        groups = engine.recommend(candidates(), profile())
        for group in groups:
            with self.subTest(stage=group["stage"]):
                self.assertEqual(len(group["maps"]), 3)
                self.assertEqual(sum(m["source"] == "online" for m in group["maps"]), 1)
                self.assertEqual(sum(m["source"] == "local" for m in group["maps"]), 2)
                self.assertTrue(all("expectation" in m for m in group["maps"]))

    def test_only_online_maps_still_obey_the_one_per_stage_limit(self):
        groups = engine.recommend([m for m in candidates() if m["source"] == "online"], profile())
        self.assertTrue(all(len(group["maps"]) == 1 for group in groups))

    def test_no_online_fit_fills_all_three_slots_from_local_library(self):
        groups = engine.recommend([m for m in candidates() if m["source"] == "local"], profile())
        self.assertTrue(all(len(group["maps"]) == 3 for group in groups))
        self.assertTrue(all(m["source"] == "local" for m in flattened(groups)))

    def test_online_slot_never_bypasses_stars_bpm_or_ar_limits(self):
        local = [m for m in candidates() if m["source"] == "local"]
        invalid = [beatmap(900, online=True, stars=8), beatmap(901, online=True, bpm=185.01),
                   beatmap(902, online=True, ar=8.701),
                   beatmap(904, online=True, mode=3)]
        groups = engine.recommend(local + invalid, profile())
        self.assertTrue(all(m["source"] == "local" for m in flattened(groups)))
        self.assertTrue(all(len(group["maps"]) == 3 for group in groups))

    def test_metadata_marked_nonlocal_cannot_take_multiple_online_slots(self):
        maps = candidates()
        for beatmap in maps:
            if beatmap["source"] == "online":
                beatmap["source"] = "discovery"
        groups = engine.recommend(maps, profile())
        self.assertTrue(all(sum(m["local"] is False for m in group["maps"]) == 1 for group in groups))

    def test_mixing_does_not_mutate_either_catalog_or_profile(self):
        maps, active = candidates(), profile()
        before_maps, before_profile = deepcopy(maps), deepcopy(active)
        engine.recommend(maps, active)
        self.assertEqual(maps, before_maps)
        self.assertEqual(active, before_profile)


class DiscoveryAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None,
                                       no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = Coach(self.args)
        self.coach.config["initial_stars"] = 3
        self.coach.catalog = [m for m in candidates() if m["source"] == "local"]
        self.fetch = Mock(return_value=[beatmap(700, online=True)])
        self.coach.discovery_store.fetcher = self.fetch

    def tearDown(self):
        self.coach.close()
        if self.coach.discovery_store.thread:
            self.coach.discovery_store.thread.join(2)
        self.coach.db.close()
        self.temp.cleanup()

    def test_app_state_excludes_remote_copy_of_installed_map(self):
        local = self.coach.catalog[0]
        duplicate = beatmap(900, online=True, id=str(local["id"]), stars=local["stars"])
        self.coach.discovery_store.maps = [duplicate, beatmap(700, online=True)]
        state = self.coach.state()
        self.assertEqual(state["discovery"]["candidate_count"], 1)
        self.assertFalse(any(m["key"] == duplicate["key"] for m in flattened(state["recommendations"])))
        self.fetch.assert_not_called()

    def test_active_mod_profile_does_not_receive_online_candidates(self):
        sample = dict(profile()["window"][0], player="Player", mod_key='{"mods":[{"acronym":"HD"}],"rate":1}')
        sample["mods"] = [{"acronym": "HD"}]
        self.coach.config["since"] = sample["played_at"]
        self.coach.add_play(sample)
        self.coach.discovery_store.maps = [beatmap(700, online=True)]
        with patch.object(self.coach, "catalog_for", return_value=(self.coach.catalog, "")):
            state = self.coach.state()
        self.assertEqual(state["discovery"]["state"], "paused")
        self.assertFalse(any(m["source"] == "online" for m in flattened(state["recommendations"])))
        self.coach.sync_discovery(force=True)
        self.fetch.assert_not_called()

    def test_manual_sync_uses_injected_fetcher_and_close_stops_future_syncs(self):
        self.coach.sync_discovery(force=True)
        self.coach.discovery_store.thread.join(2)
        self.fetch.assert_called_once_with(2.3, 3.4)
        self.assertEqual(self.coach.state()["discovery"]["candidate_count"], 1)
        self.coach.close()
        self.coach.sync_discovery(force=True)
        self.assertEqual(self.fetch.call_count, 1)

    def test_demo_sync_and_state_do_not_request_external_metadata(self):
        args = argparse.Namespace(**vars(self.args))
        args.demo = True
        args.data_dir = str(Path(self.temp.name, "demo"))
        demo = Coach(args)
        demo.discovery_store.fetcher = self.fetch
        try:
            demo.sync_discovery(force=True)
            self.assertTrue(demo.state()["demo"])
            self.fetch.assert_not_called()
        finally:
            demo.close()
            demo.db.close()

    def test_sync_endpoint_requires_token_and_valid_origin(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.coach = self.coach
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/discovery/sync"
        try:
            request = Request(url, data=b"{}", headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            self.fetch.assert_not_called()
            request.add_header("X-Coach-Token", self.coach.token)
            request.add_header("Origin", "https://unrelated.example")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            self.fetch.assert_not_called()
            request.remove_header("Origin")
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["ok"])
            self.coach.discovery_store.thread.join(2)
            self.fetch.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
