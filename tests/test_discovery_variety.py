"""Discovery traverses older pages and keeps the unverified remainder of each batch."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from osu_coach.integrations.discovery_source import DiscoverySourceClient, parse_candidates
from osu_coach.storage.discovery_store import DiscoveryStore
from osu_coach.core.song_identity import song_tokens
from tests.test_discovery_source import beatmapset, cursor_response, set_response, search_response
from tests.test_discovery_demand import beatmap, need, NOW
from tests import test_auto_discovery_integration as fixtures


class DiscoveryVarietyTests(unittest.TestCase):
    def client(self, responses):
        opener = Mock()
        opener.open.side_effect = responses
        return DiscoverySourceClient(opener=opener, clock=lambda: 10, sleep=Mock()), opener

    def test_unverified_remainder_survives_roundtrip_and_drains_after_feed_end(self):
        sets = [beatmapset(i) for i in range(1, 21)]
        client, opener = self.client([cursor_response(sets, None)] + [set_response(s) for s in sets])
        first = client.fetch_candidate_batch(4, 5)
        self.assertEqual(8, len(first["maps"]))
        self.assertFalse(first["exhausted"])
        self.assertEqual(12, len(first["next_cursor"]["pending"]))
        second = client.fetch_candidate_batch(4, 5, cursor=json.loads(json.dumps(first["next_cursor"])))
        third = client.fetch_candidate_batch(4, 5, cursor=second["next_cursor"])
        self.assertEqual(list(range(10, 201, 10)), [m["id"] for batch in (first, second, third) for m in batch["maps"]])
        self.assertTrue(third["exhausted"])
        self.assertIsNone(third["next_cursor"])
        self.assertEqual(21, opener.open.call_count)

    def test_draining_backlog_preserves_cursor_into_older_pages_and_rechecks_quality(self):
        sets = [beatmapset(i) for i in range(1, 11)]
        old = beatmapset(11, ranked_date="2011-01-01T00:00:00Z")
        client, opener = self.client([search_response(sets), search_response([beatmapset(100, play_count=0)]),
            search_response([beatmapset(101, play_count=0)])] + [set_response(s) for s in sets[:8]] +
            [set_response({**sets[8], "play_count": 1}), set_response(sets[9]), cursor_response([old], None), set_response(old)])
        first = client.fetch_candidate_batch(4, 5)
        second = client.fetch_candidate_batch(4, 5, cursor=first["next_cursor"])
        self.assertEqual([100], [m["id"] for m in second["maps"]])
        self.assertEqual(4, second["next_cursor"]["page"])
        third = client.fetch_candidate_batch(4, 5, cursor=second["next_cursor"])
        self.assertEqual([110], [m["id"] for m in third["maps"]])
        self.assertIn("page=4", opener.open.call_args_list[-2].args[0].full_url)

    def test_banned_song_does_not_spend_verification_read(self):
        excluded, allowed = beatmapset(1), beatmapset(2, title="Different song")
        tokens = song_tokens(parse_candidates(excluded, 4, 5)[0])
        client, opener = self.client([cursor_response([excluded, allowed], None), set_response(allowed)])
        result = client.fetch_candidate_batch(4, 5, excluded_songs=tokens)
        self.assertEqual([20], [m["id"] for m in result["maps"]])
        self.assertEqual(2, opener.open.call_count)

    def test_invalid_backlogs_are_rejected_before_network(self):
        for pending in ([True], ["12"], [1, 1], list(range(1, 302)), "1"):
            client, opener = self.client([])
            with self.assertRaises(ValueError):
                client.fetch_candidate_batch(4, 5, cursor={"version": 2, "pending": pending, "feed_exhausted": True, "feed": None})
            opener.open.assert_not_called()

    def test_periodic_and_manual_search_resume_persisted_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            fetch = Mock(side_effect=[{"maps": [beatmap(1)], "next_cursor": {"page": 4}, "exhausted": False},
                                      {"maps": [beatmap(2)], "next_cursor": {"page": 7}, "exhausted": False},
                                      {"maps": [beatmap(3)], "next_cursor": {"page": 10}, "exhausted": False}])
            for i in range(3):
                store = DiscoveryStore(directory, batch_fetcher=fetch)
                with patch("osu_coach.storage.discovery_store.time.time", return_value=NOW + i * 86400):
                    self.assertTrue(store.sync(4.5, force=i == 2))
                    store.thread.join(3)
                self.assertFalse(store.thread.is_alive())
                self.assertEqual(i + 1, len(store.maps))
                self.assertEqual(None if i == 0 else {"page": 1 + i * 3}, fetch.call_args.kwargs["cursor"])


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AutomaticDiscoveryTests()
        self.fixture.setUp()
        self.coach = self.fixture.coach
        self.coach.discovery_store.maps = self.fixture.remote

    def tearDown(self):
        self.fixture.tearDown()

    def test_full_board_keeps_searching_for_online_reserve_and_setting_zero_stops_it(self):
        state = self.coach.state()
        self.assertEqual([], state["discovery"]["needs"])
        self.assertTrue(state["discovery"]["search_needs"])
        self.assertTrue(all(item["target"] == 6 for item in state["discovery"]["reserve"]))
        self.coach.background_enabled = True
        self.coach.state()
        self.coach.discovery_store.thread.join(3)
        self.fixture.fetch.assert_called_once()
        self.coach.update_settings({"discovery_reserve_per_stage": 0})
        state = self.coach.state()
        self.assertEqual([], state["discovery"]["search_needs"])
        self.assertEqual([], state["discovery"]["reserve"])
