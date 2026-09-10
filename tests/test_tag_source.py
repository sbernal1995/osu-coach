"""Public user-tag extraction and safe read-only HTTP behaviour (offline)."""

import copy
from email.message import Message
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import tag_source


PAYLOAD = {
    "id": 12,
    "tags": "rock anime jumps",  # Mapper metadata must never become user tags.
    "related_tags": [
        {"id": 19, "name": "skillset/jumps", "ruleset_id": 0, "description": "Jumps."},
        {"id": 25, "name": "streams/bursts", "ruleset_id": None, "description": "Bursts."},
    ],
    "beatmaps": [
        {"id": 100, "beatmapset_id": 12, "top_tag_ids": [{"tag_id": 19, "count": 12}]},
        {"id": 101, "beatmapset_id": 12, "top_tag_ids": [{"tag_id": 25, "count": 2}]},
        {"id": 102, "beatmapset_id": 12, "top_tag_ids": []},
    ],
}


def page(payload=None):
    return ('<html><script type="application/json" id="json-beatmapset">'
            + json.dumps(PAYLOAD if payload is None else payload) + '</script></html>')


class Response(io.BytesIO):
    status = 200

    def __init__(self, body=None, url="https://osu.ppy.sh/beatmapsets/12"):
        super().__init__((page() if body is None else body).encode() if not isinstance(body, bytes) else body)
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def geturl(self):
        return self.url


class TagParsingTests(unittest.TestCase):
    def test_tags_are_per_difficulty_and_preserve_vote_count_and_source(self):
        result = tag_source.parse_set_tags(page(), 12)
        self.assertEqual([t["name"] for t in result[100]], ["skillset/jumps"])
        self.assertEqual(result[100][0]["count"], 12)
        self.assertEqual(result[100][0]["source"], "community_user_tags")
        self.assertEqual(result[101][0]["name"], "streams/bursts")
        self.assertEqual(result[101][0]["count"], 2)
        self.assertEqual(result[102], [])

    def test_mapper_tags_never_used_as_fallback(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["related_tags"] = []
        for beatmap in payload["beatmaps"]:
            beatmap["top_tag_ids"] = []
        self.assertEqual(tag_source.parse_set_tags(page(payload), 12), {100: [], 101: [], 102: []})

    def test_missing_votes_is_unknown_not_empty_evidence(self):
        payload = copy.deepcopy(PAYLOAD)
        del payload["beatmaps"][0]["top_tag_ids"]
        with self.assertRaises(tag_source.TagSourceError):
            tag_source.parse_set_tags(page(payload), 12)

    def test_set_mismatch_rejected(self):
        with self.assertRaises(tag_source.TagSourceError):
            tag_source.parse_set_tags(page(), 13)
        payload = copy.deepcopy(PAYLOAD)
        payload["beatmaps"][0]["beatmapset_id"] = 13
        with self.assertRaises(tag_source.TagSourceError):
            tag_source.parse_set_tags(page(payload), 12)

    def test_unknown_definition_and_invalid_counts_rejected(self):
        for vote in [{"tag_id": 999, "count": 5}, {"tag_id": 19, "count": -1},
                     {"tag_id": 19, "count": True}, {"tag_id": 19, "count": "12"}]:
            with self.subTest(vote=vote):
                payload = copy.deepcopy(PAYLOAD)
                payload["beatmaps"][0]["top_tag_ids"] = [vote]
                with self.assertRaises(tag_source.TagSourceError):
                    tag_source.parse_set_tags(page(payload), 12)

    def test_no_json_duplicate_script_and_malformed_json_rejected(self):
        for content in ["<html>Login required</html>", page() + page(),
                        '<script id="json-beatmapset" type="application/json">{oops}</script>']:
            with self.subTest(content=content[:50]), self.assertRaises(tag_source.TagSourceError):
                tag_source.parse_set_tags(content, 12)

    def test_size_limit(self):
        with patch.object(tag_source, "MAX_RESPONSE_BYTES", 20):
            with self.assertRaises(tag_source.TagSourceError):
                tag_source.parse_set_tags(page(), 12)


class TagNetworkTests(unittest.TestCase):
    def client(self, responses):
        opener = Mock()
        opener.open.side_effect = responses
        clock = Mock(return_value=10.0)
        sleep = Mock()
        return tag_source.TagSourceClient(opener=opener, clock=clock, sleep=sleep), opener, sleep

    def test_get_official_page_without_credentials(self):
        client, opener, _ = self.client([Response()])
        self.assertIn(100, client.fetch_set_tags(12))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://osu.ppy.sh/beatmapsets/12")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertFalse(any(k.lower() in {"cookie", "authorization"} for k, _ in request.header_items()))

    def test_duplicate_sets_fetched_once_and_requests_spaced(self):
        second = copy.deepcopy(PAYLOAD)
        second["id"] = 13
        for beatmap in second["beatmaps"]:
            beatmap["beatmapset_id"] = 13
        client, opener, sleep = self.client([Response(), Response(page(second), "https://osu.ppy.sh/beatmapsets/13")])
        result = client.fetch_sets_tags([12, 12, 13])
        self.assertEqual(list(result), [12, 13])
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(tag_source.MIN_REQUEST_INTERVAL)

    def test_invalid_ids_never_hit_network(self):
        client, opener, _ = self.client([])
        for value in [0, -1, True, "12", "12/../../secrets", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                client.fetch_set_tags(value)
        with self.assertRaises(ValueError):
            client.fetch_sets_tags([1, True])
        opener.open.assert_not_called()

    def test_auth_error_is_explicit_and_no_retry(self):
        error = HTTPError("https://osu.ppy.sh/beatmapsets/12", 401, "Unauthorized", {}, None)
        client, opener, _ = self.client([error])
        with self.assertRaises(tag_source.TagSourceError) as raised:
            client.fetch_set_tags(12)
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(opener.open.call_count, 1)

    def test_transport_failure_is_explicit(self):
        client, _, _ = self.client([URLError("offline")])
        with self.assertRaises(tag_source.TagSourceError):
            client.fetch_set_tags(12)

    def test_unexpected_host_and_redirects_rejected(self):
        client, _, _ = self.client([Response(url="https://example.com/beatmapsets/12")])
        with self.assertRaises(tag_source.TagSourceError):
            client.fetch_set_tags(12)
        with self.assertRaises(tag_source.TagSourceError):
            tag_source._NoRedirect().redirect_request(None, None, 302, "", {}, "https://osu.ppy.sh/home")

    def test_large_response_and_wrong_content_type_rejected(self):
        response = Response()
        response.headers.replace_header("Content-Type", "application/octet-stream")
        client, _, _ = self.client([response])
        with self.assertRaises(tag_source.TagSourceError):
            client.fetch_set_tags(12)
        client, _, _ = self.client([Response(b"a" * 51)])
        with patch.object(tag_source, "MAX_RESPONSE_BYTES", 50):
            with self.assertRaises(tag_source.TagSourceError):
                client.fetch_set_tags(12)

    def test_rate_limit_cannot_be_disabled(self):
        for interval in [0, 0.5, float("nan"), float("inf")]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                tag_source.TagSourceClient(interval=interval)


if __name__ == "__main__":
    unittest.main()
