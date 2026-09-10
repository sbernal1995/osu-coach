import argparse
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from osu_coach.app import Coach, Handler, ThreadingHTTPServer, profile_key
from osu_coach.demo import demo_data


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None,
                                       no_tosu=True, tosu_url="http://127.0.0.1:24050/json/v2")
        self.coach = Coach(self.args)
        self.maps, self.samples = demo_data()
        self.coach.catalog = self.maps
        self.coach.config["since"] = self.samples[0]["played_at"]

    def tearDown(self):
        self.coach.close()
        self.coach.db.close()
        self.temp.cleanup()

    def test_duplicates_pending_and_confirmation(self):
        score = self.samples[0]
        score["needs_confirmation"] = True
        self.coach.add_play(score)
        self.coach.add_play(score)
        self.assertEqual(1, len(self.coach.plays("pending")))
        self.assertEqual(0, self.coach.state()["profile"]["attempts"])
        self.coach.confirm(score["id"], True)
        self.assertEqual(1, self.coach.state()["profile"]["attempts"])
        self.assertEqual([], self.coach.plays("pending"))

    def test_reset_preserves_log_but_excludes_prior_scores(self):
        for sample in self.samples:
            self.coach.add_play(sample)
        self.assertEqual(5, self.coach.state()["profile"]["attempts"])
        self.coach.reset()
        self.assertEqual(5, len(self.coach.plays()))
        self.assertEqual(0, self.coach.state()["profile"]["attempts"])

    def test_profiles_separate_players_clients_and_mods(self):
        a, b = self.samples[:2]
        b["player"] = "Otra persona"
        self.coach.add_play(a)
        self.coach.add_play(b)
        self.assertEqual(1, self.coach.state()["profile"]["attempts"])
        original = profile_key(a)
        a["client"] = "stable"
        self.assertNotEqual(original, profile_key(a))
        a["client"] = "lazer"
        a["mod_key"] = '{"mods":[{"acronym":"DT"}],"rate":1.2}'
        self.assertNotEqual(original, profile_key(a))

    def test_existing_scores_gain_exact_tags_without_changing_calibration(self):
        for m in self.maps:
            m["tags"] = []
        self.maps[12]["id"] = 123
        score = self.samples[0]
        score["beatmap_id"] = 123
        self.coach.add_play(score)
        before = self.coach.state()
        self.assertEqual(0, before["tag_analysis"]["tagged_plays"])
        self.coach.tag_store.sets = {"1": {"maps": {"123": [
            {"id": 19, "name": "skillset/jumps", "count": 20},
            {"id": 2, "name": "expression/simple", "count": 50}]}, "fetched_at": score["played_at"]}}
        after = self.coach.state()
        self.assertEqual({key: value for key, value in before["profile"].items() if key != "evaluated_at"},
                         {key: value for key, value in after["profile"].items() if key != "evaluated_at"})
        self.assertEqual(1, after["tag_analysis"]["tagged_plays"])
        item = next(i for i in after["tag_analysis"]["items"] if i["tag"] == "skillset/jumps")
        self.assertEqual("learning", item["status"])
        self.assertEqual(1, item["plays"])
        self.assertTrue(all(t["name"] != "expression/simple" for g in after["recommendations"]
                            for m in g["maps"] for t in m.get("tags", [])))
        self.assertEqual(1, len(self.coach.plays()))

    def test_http_blocks_forged_posts_and_can_confirm(self):
        score = self.samples[0]
        score["needs_confirmation"] = True
        self.coach.add_play(score)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.coach = self.coach
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(url + "/api/state") as response:
                self.assertEqual("osu-coach", json.load(response)["app"])
            payload = json.dumps({"id": score["id"], "accept": True}).encode()
            request = Request(url + "/api/confirm", data=payload, headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(403, caught.exception.code)
            request.add_header("X-Coach-Token", self.coach.token)
            request.add_header("Origin", "https://unrelated.example")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(403, caught.exception.code)
            request.remove_header("Origin")
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertEqual(1, self.coach.state()["profile"]["attempts"])
            with patch.object(self.coach, "sync_tags") as sync:
                tag_request = Request(url + "/api/tags/sync", data=b"{}", headers={"X-Coach-Token": self.coach.token})
                with urlopen(tag_request) as response:
                    self.assertTrue(json.load(response)["ok"])
                sync.assert_called_once_with(force=True)
            stop_request = Request(url + "/api/stop", data=b"{}", headers={"X-Coach-Token": self.coach.token})
            with urlopen(stop_request) as response:
                self.assertTrue(json.load(response)["ok"])
            thread.join(2)
            self.assertFalse(thread.is_alive())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
