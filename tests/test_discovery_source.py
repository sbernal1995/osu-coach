"""Offline parsing, qualification and bounded public discovery network tests."""

import copy
from email.message import Message
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import discovery_source as source


def beatmap(map_id=120, set_id=12, **changes):
    item = {"id": map_id, "beatmapset_id": set_id, "difficulty_rating": 4.5,
            "version": "Insane", "mode": "osu", "mode_int": 0, "convert": False,
            "status": "ranked", "bpm": 160, "ar": 8.5, "total_length": 150,
            "count_circles": 200, "count_sliders": 100, "count_spinners": 1,
            "max_combo": 450, "accuracy": 8, "cs": 4, "checksum": "a" * 32}
    item.update(changes)
    return item


def beatmapset(set_id=12, **changes):
    item = {"id": set_id, "title": "Song", "artist": "Artist", "creator": "Mapper",
            "rating": 9, "ratings": [0] * 9 + [10, 0], "favourite_count": 20, "play_count": 10_000,
            "status": "ranked", "beatmaps": [beatmap(set_id * 10, set_id)]}
    item.update(changes)
    return item


def page(payload):
    return '<script type="application/json" id="json-beatmapset">' + json.dumps(payload) + '</script>'


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, *, path="/beatmapsets/search", content_type="application/json", host="osu.ppy.sh"):
        body = data if isinstance(data, (str, bytes)) else json.dumps(data)
        super().__init__(body.encode() if isinstance(body, str) else body)
        self.url = f"https://{host}{path}"
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self):
        return self.url


def search_response(sets):
    return Response({"beatmapsets": sets, "search": {"sort": "ranked_desc"}, "error": None})


def set_response(item):
    return Response(page(item), path=f"/beatmapsets/{item['id']}", content_type="text/html")


class DiscoveryParsingTests(unittest.TestCase):
    def test_unicode_display_keeps_original_romanized_names_for_search(self):
        item = beatmapset(title="Harumachi Clover (Swing Arrangement) [Dictate Edit]",
                          title_unicode="春待ちクローバー (Swing Arrangement) [Dictate Edit]",
                          artist="Romanized Artist", artist_unicode="歌手")
        result = source.parse_candidates(item, 4, 5)[0]
        self.assertEqual(item["title_unicode"], result["title"])
        self.assertEqual(item["title"], result["title_romanized"])
        self.assertEqual(item["artist_unicode"], result["artist"])
        self.assertEqual(item["artist"], result["artist_romanized"])
        self.assertEqual("Mapper", result["creator"])

    def test_normalizes_physical_data_and_official_links(self):
        result = source.parse_candidates(beatmapset(), 4, 5)[0]
        self.assertEqual("Song", result["title_romanized"])
        self.assertEqual("Artist", result["artist_romanized"])
        self.assertEqual(result["key"], "remote:120")
        self.assertEqual(result["object_count"], 301)
        self.assertEqual(result["max_combo"], 450)
        self.assertEqual(result["od"], 8)
        self.assertEqual(result["source"], "online")
        self.assertFalse(result["local"])
        self.assertEqual(result["url"], "https://osu.ppy.sh/beatmapsets/12#osu/120")
        self.assertEqual(result["download_url"], "https://osu.ppy.sh/beatmapsets/12/download")
        self.assertEqual(result["popularity"]["scope"], "beatmapset")

    def test_guest_filter_ignored_still_filters_each_difficulty_locally(self):
        item = beatmapset(beatmaps=[beatmap(1, difficulty_rating=3.99), beatmap(2, difficulty_rating=4),
                                  beatmap(3, difficulty_rating=5), beatmap(4, difficulty_rating=5.01),
                                  beatmap(5, mode="mania", mode_int=3), beatmap(6, convert=True)])
        sets = source.parse_search_page({"beatmapsets": [item], "search": {"sort": "ranked_desc"}})
        self.assertEqual([m["id"] for m in source.parse_candidates(sets[0], 4, 5)], [2, 3])

    def test_rating_requires_actual_vote_count(self):
        for ratings in (None, [], [0] * 10 + [9], [0] * 11, [0] * 10 + [True]):
            with self.subTest(ratings=ratings):
                item = beatmapset(rating=10, ratings=ratings, favourite_count=49)
                self.assertEqual(source.parse_candidates(item, 4, 5), [])
        item = beatmapset(ratings=[0] * 8 + [10, 0, 0])
        result = source.parse_candidates(item, 4, 5)[0]
        self.assertEqual(result["popularity"]["qualification"], "rated")
        self.assertEqual(result["popularity"]["rating"], 8)
        self.assertEqual(result["popularity"]["rating_votes"], 10)
        self.assertEqual(result["popularity"]["votes"], 10)

    def test_favourites_never_substitute_rating_votes_or_play_count(self):
        cases = [{"ratings": None, "play_count": 1_000_000},
                 {"ratings": [0] * 10 + [9], "play_count": 1_000_000},
                 {"ratings": [0] * 7 + [10, 0, 0, 0], "play_count": 1_000_000},
                 {"play_count": 9999}, {"play_count": None}]
        for changes in cases:
            with self.subTest(changes=changes):
                item = beatmapset(favourite_count=1_000_000, **changes)
                evidence = source.popularity_evidence(item)
                self.assertIsNone(evidence["qualification"])
                self.assertFalse(source.candidate_quality_ok({"popularity": evidence}))
                self.assertEqual(source.parse_candidates(item, 4, 5), [])
                self.assertNotIn("popular:", evidence["label"].lower())

    def test_play_count_exact_threshold_and_set_scope_are_explicit(self):
        item = beatmapset(ratings=[0] * 8 + [10, 0, 0], play_count=10_000, favourite_count=0)
        candidate = source.parse_candidates(item, 4, 5)[0]
        evidence = candidate["popularity"]
        self.assertTrue(source.candidate_quality_ok(candidate))
        self.assertEqual(evidence["play_count"], 10_000)
        self.assertEqual(evidence["scope"], "beatmapset")
        self.assertEqual(evidence["qualification"], "rated")
        self.assertEqual(evidence["label"], "Set: 8,00/10 (10 votos) · 10.000 reproducciones")

    def test_play_count_requires_an_explicit_nonnegative_integer(self):
        for value in (None, True, False, "10000", 10_000.0, -1, 0, 9999, 2**63, float("inf"), float("nan")):
            with self.subTest(value=value):
                item = beatmapset(play_count=value)
                self.assertEqual(source.parse_candidates(item, 4, 5), [])
        item = beatmapset()
        del item["play_count"]
        item["beatmaps"][0]["playcount"] = 1_000_000
        self.assertEqual(source.parse_candidates(item, 4, 5), [])
        self.assertIsNone(source.popularity_evidence(item)["play_count"])

    def test_rating_threshold_uses_unrounded_vote_average(self):
        item = beatmapset(ratings=[0] * 7 + [1, 999, 0, 0])
        evidence = source.popularity_evidence(item)
        self.assertEqual(evidence["rating"], 7.999)
        self.assertIsNone(evidence["qualification"])
        self.assertFalse(source.candidate_quality_ok({"popularity": evidence}))

    def test_cached_quality_validates_numbers_scope_and_consistent_vote_alias(self):
        base = {"scope": "beatmapset", "rating": 8, "votes": 10, "play_count": 10_000}
        self.assertTrue(source.candidate_quality_ok({"popularity": base}))
        self.assertTrue(source.candidate_quality_ok({"popularity": {**base, "rating_votes": 10}}))
        cases = [("scope", "beatmap"), ("scope", None), ("rating_votes", 11), ("rating_votes", True)]
        for field, invalid_values in (
            ("rating", (None, True, "9", 7.999, 10.001, float("nan"), float("inf"))),
            ("votes", (None, True, "10", 9, 10.0, -1, 2**63)),
            ("play_count", (None, True, "10000", 9999, 10_000.0, -1, 2**63)),
        ):
            cases.extend((field, value) for value in invalid_values)
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assertFalse(source.candidate_quality_ok({"popularity": {**base, field: value}}))
        for missing in base:
            value = dict(base)
            del value[missing]
            self.assertFalse(source.candidate_quality_ok({"popularity": value}))
        for value in (None, [], {}, {"popularity": None}, {"popularity": []},
                      {"popularity": {"qualification": "popular", "favourites": 1_000_000}},
                      {"popularity": {"qualification": "rated", "label": "Excellent"}}):
            with self.subTest(value=value):
                self.assertFalse(source.candidate_quality_ok(value))

    def test_ranked_status_alone_never_qualifies(self):
        item = beatmapset(ratings=None, favourite_count=0, rating=10)
        self.assertEqual(source.parse_candidates(item, 4, 5), [])
        self.assertEqual(len(source.parse_candidates(item, 4, 5, require_qualification=False)), 1)

    def test_rating_is_calculated_from_votes_not_unverified_average(self):
        evidence = source.popularity_evidence(beatmapset(rating=10, ratings=[0] * 7 + [10, 0, 0, 0]))
        self.assertEqual(evidence["rating"], 7)
        self.assertIsNone(evidence["qualification"])

    def test_incomplete_or_invalid_physical_data_are_rejected(self):
        cases = [{"bpm": None}, {"ar": 0}, {"total_length": 0}, {"count_circles": None},
                 {"count_circles": 0, "count_sliders": 0, "count_spinners": 0},
                 {"bpm": float("nan")}, {"bpm": 10**1000}, {"mode_int": False},
                 {"convert": None}, {"beatmapset_id": 999}, {"status": "pending"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(source.parse_candidates(beatmapset(beatmaps=[beatmap(**changes)]), 4, 5), [])

    def test_missing_combo_stays_unknown_and_external_urls_are_ignored(self):
        item = beatmapset(beatmaps=[beatmap(max_combo=None, url="https://example.com/a")])
        result = source.parse_candidates(item, 4, 5)[0]
        self.assertNotIn("max_combo", result)
        self.assertTrue(result["url"].startswith("https://osu.ppy.sh/"))

    def test_download_disabled_and_duplicate_maps(self):
        self.assertEqual(source.parse_candidates(beatmapset(availability={"download_disabled": True}), 4, 5), [])
        self.assertEqual(len(source.parse_candidates(beatmapset(beatmaps=[beatmap(), beatmap()]), 4, 5)), 1)

    def test_set_html_rejects_login_schema_changes_and_identity_mismatch(self):
        self.assertEqual(source.parse_set_page(page(beatmapset()), 12)["id"], 12)
        for body in ("Login", page(beatmapset(13)), page(beatmapset()) * 2,
                     '<script type="application/json" id="json-beatmapset">{bad}</script>'):
            with self.subTest(body=body[:30]), self.assertRaises(source.DiscoverySourceError):
                source.parse_set_page(body, 12)

    def test_search_envelope_errors_are_not_empty_success(self):
        for payload in ({}, [], {"beatmapsets": [], "error": "login"}, {"beatmapsets": [None]}):
            with self.subTest(payload=payload), self.assertRaises(source.DiscoverySourceError):
                source.parse_search_page(payload)


class DiscoveryNetworkTests(unittest.TestCase):
    def client(self, responses):
        opener = Mock()
        opener.open.side_effect = responses
        sleep = Mock()
        return source.DiscoverySourceClient(opener=opener, clock=lambda: 10.0, sleep=sleep), opener, sleep

    def test_public_feed_plus_verified_set_without_credentials(self):
        item = beatmapset()
        client, opener, sleep = self.client([search_response([item]), search_response([]), set_response(item)])
        self.assertEqual(len(client.fetch_candidates(4, 5)), 1)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(requests[0].full_url, "https://osu.ppy.sh/beatmapsets/search?page=1")
        self.assertEqual(requests[-1].full_url, "https://osu.ppy.sh/beatmapsets/12")
        for request in requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertFalse(any(key.lower() in {"authorization", "cookie"} for key, _ in request.header_items()))
        self.assertEqual(sleep.call_count, 2)

    def test_bounds_pages_and_verified_sets(self):
        groups = [[beatmapset(i, favourite_count=i) for i in range(1 + page * 10, 11 + page * 10)] for page in range(3)]
        selected = [beatmapset(i, favourite_count=i) for i in range(30, 22, -1)]
        client, opener, _ = self.client([search_response(group) for group in groups] + [set_response(item) for item in selected])
        self.assertEqual(len(client.fetch_candidates(4, 5)), 8)
        self.assertEqual(opener.open.call_count, 11)

    def test_duplicate_public_page_stops_and_duplicate_set_is_fetched_once(self):
        item = beatmapset()
        client, opener, _ = self.client([search_response([item]), search_response([item]), set_response(item)])
        self.assertEqual(len(client.fetch_candidates(4, 5)), 1)
        self.assertEqual(opener.open.call_count, 3)

    def test_verification_rechecks_stars_changed_after_listing(self):
        listed = beatmapset()
        changed = beatmapset(beatmaps=[beatmap(difficulty_rating=6)])
        client, _, _ = self.client([search_response([listed]), search_response([]), set_response(changed)])
        self.assertEqual(client.fetch_candidates(4, 5), [])

    def test_reuses_verified_page_for_per_difficulty_tags(self):
        item = beatmapset(related_tags=[{"id": 1, "name": "skillset/jumps"}, {"id": 2, "name": "skillset/streams"}])
        item["beatmaps"][0]["top_tag_ids"] = [{"tag_id": 1, "count": 5}, {"tag_id": 2, "count": 4}]
        client, opener, _ = self.client([search_response([item]), search_response([]), set_response(item)])
        result = client.fetch_candidates(4, 5)[0]
        self.assertEqual([tag["name"] for tag in result["tags"]], ["skillset/jumps"])
        self.assertEqual(result["tags"][0]["source"], "community")
        self.assertEqual(opener.open.call_count, 3)

    def test_invalid_range_never_calls_network(self):
        client, opener, _ = self.client([])
        for lower, upper in [(5, 4), (None, 5), (True, 5), (float("nan"), 5), (0, 5), (4, 100)]:
            with self.subTest(lower=lower, upper=upper), self.assertRaises(ValueError):
                client.fetch_candidates(lower, upper)
        opener.open.assert_not_called()

    def test_http_auth_rate_limit_and_transport_failures_are_explicit_without_retry(self):
        for error in [HTTPError("https://osu.ppy.sh/beatmapsets/search", 401, "Unauthorized", {}, None),
                      HTTPError("https://osu.ppy.sh/beatmapsets/search", 429, "Slow down", {}, None), URLError("offline")]:
            client, opener, _ = self.client([error])
            with self.subTest(error=error), self.assertRaises(source.DiscoverySourceError):
                client.fetch_candidates(4, 5)
            self.assertEqual(opener.open.call_count, 1)

    def test_oversize_wrong_type_wrong_host_and_compression_rejected(self):
        responses = [Response(b"a" * 51), Response({}, content_type="text/html"),
                     Response({}, host="example.com")]
        compressed = Response({})
        compressed.headers["Content-Encoding"] = "gzip"
        responses.append(compressed)
        for response in responses:
            client, _, _ = self.client([response])
            with self.subTest(response=response), patch.object(source, "MAX_RESPONSE_BYTES", 50):
                with self.assertRaises(source.DiscoverySourceError):
                    client.fetch_candidates(4, 5)

    def test_redirect_and_rate_limit_cannot_be_disabled(self):
        with self.assertRaises(source.DiscoverySourceError):
            source._NoRedirect().redirect_request(None, None, 302, "", {}, "https://osu.ppy.sh/home")
        for interval in [0, .5, float("nan")]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                source.DiscoverySourceClient(interval=interval)


def cursor_response(sets, token, *, date=1000, set_id=100):
    return Response({"beatmapsets": sets, "search": {"sort": "ranked_desc"}, "error": None,
                     "cursor_string": token,
                     "cursor": {"approved_date": date, "id": set_id} if token is not None else None})


class DiscoveryBatchTests(unittest.TestCase):
    client = DiscoveryNetworkTests.client

    def test_outgoing_requests_only_contain_public_pagination_and_set_ids(self):
        # All values are invented. The actual request builder runs, while the
        # injected opener captures requests and returns only in-memory fixtures.
        private_context = {"player_name": "SYNTHETIC_PRIVATE_PLAYER",
                           "accuracy": 98.7654321, "score": 765432109, "misses": 314159}
        excluded = {120, 987654321}
        requirements = [{"min_stars": 4.1234, "max_stars": 4.9876, "max_bpm": 178.987,
                         "max_ar": 9.876, "max_length": 171.123,
                         "private_context": private_context}]
        item = beatmapset(beatmaps=[beatmap(120), beatmap(121)])
        client, opener, _ = self.client([
            cursor_response([item], "public_cursor_fixture_1"),
            cursor_response([], None),
            set_response(item),
        ])
        with patch("socket.create_connection", side_effect=AssertionError("Real network is forbidden")):
            result = client.fetch_candidate_batch(4.0123, 5.0987, exclude_ids=excluded,
                                                   requirements=requirements)
        self.assertEqual([entry["id"] for entry in result["maps"]], [121])
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual([request.full_url for request in requests], [
            "https://osu.ppy.sh/beatmapsets/search?page=1",
            "https://osu.ppy.sh/beatmapsets/search?cursor_string=public_cursor_fixture_1",
            "https://osu.ppy.sh/beatmapsets/12",
        ])
        for index, request in enumerate(requests):
            with self.subTest(request=index):
                self.assertEqual(request.get_method(), "GET")
                self.assertIsNone(request.data)
                headers = {key.lower(): value for key, value in request.header_items()}
                self.assertEqual(headers, {
                    "user-agent": "osu-coach-local/1.0 (public map discovery)",
                    "accept": "application/json" if index < 2 else "text/html",
                    "accept-encoding": "identity",
                })
                self.assertNotIn("authorization", headers)
                self.assertNotIn("cookie", headers)
                outgoing = request.full_url + json.dumps(headers) + repr(request.data)
                private_markers = [*map(str, private_context.values()), *map(str, excluded),
                                   *map(str, requirements[0].values()), "4.0123", "5.0987",
                                   "requirements", "exclude_ids", "player_name", "accuracy", "score",
                                   "min_stars", "max_stars", "max_bpm", "max_ar", "max_length"]
                for marker in private_markers:
                    self.assertNotIn(marker, outgoing)

    def test_low_or_missing_listed_play_count_does_not_consume_set_verifications(self):
        unsuitable = [beatmapset(i, favourite_count=1_000_000, play_count=count)
                      for i, count in enumerate((None, 9999, "10000", True), 1)]
        wanted = beatmapset(10, favourite_count=0)
        client, opener, _ = self.client([cursor_response(unsuitable + [wanted], None), set_response(wanted)])
        batch = client.fetch_candidate_batch(4, 5)
        self.assertEqual([item["id"] for item in batch["maps"]], [100])
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("/beatmapsets/10"))

    def test_full_set_verification_rechecks_rating_votes_and_play_count(self):
        for change in ({"play_count": 9999}, {"play_count": None}, {"ratings": None},
                       {"ratings": [0] * 10 + [9]}, {"ratings": [0] * 7 + [10, 0, 0, 0]}):
            with self.subTest(change=change):
                listed = beatmapset()
                changed = beatmapset(favourite_count=1_000_000, **change)
                client, opener, _ = self.client([cursor_response([listed], None), set_response(changed)])
                self.assertEqual(client.fetch_candidate_batch(4, 5)["maps"], [])
                self.assertEqual(opener.open.call_count, 2)

    def test_empty_eligible_batch_advances_and_next_call_resumes_cursor(self):
        outside = [beatmapset(i, beatmaps=[beatmap(i * 10, i, difficulty_rating=6)]) for i in range(1, 4)]
        wanted = beatmapset(4)
        responses = [cursor_response([item], f"cursor_{index}", date=1000 - index)
                     for index, item in enumerate(outside, 1)]
        client, opener, _ = self.client(responses + [cursor_response([wanted], None), set_response(wanted)])
        first = client.fetch_candidate_batch(4, 5)
        self.assertEqual(first["maps"], [])
        self.assertFalse(first["exhausted"])
        self.assertEqual(first["next_cursor"]["cursor_string"], "cursor_3")
        persisted = json.loads(json.dumps(first["next_cursor"]))
        before = copy.deepcopy(persisted)
        second = client.fetch_candidate_batch(4, 5, cursor=persisted)
        self.assertEqual(persisted, before)
        self.assertEqual([item["id"] for item in second["maps"]], [40])
        self.assertTrue(second["exhausted"])
        self.assertIsNone(second["next_cursor"])
        requests = [call.args[0].full_url for call in opener.open.call_args_list]
        self.assertEqual(requests[3], "https://osu.ppy.sh/beatmapsets/search?cursor_string=cursor_3")
        self.assertEqual(requests.count("https://osu.ppy.sh/beatmapsets/search?page=1"), 1)

    def test_legacy_page_envelope_resumes_page_four(self):
        groups = [[beatmapset(i, beatmaps=[])] for i in range(1, 4)]
        client, opener, _ = self.client([search_response(group) for group in groups] + [search_response([])])
        first = client.fetch_candidate_batch(4, 5)
        self.assertFalse(first["exhausted"])
        self.assertEqual(first["next_cursor"]["page"], 4)
        second = client.fetch_candidate_batch(4, 5, cursor=first["next_cursor"])
        self.assertTrue(second["exhausted"])
        self.assertEqual(opener.open.call_args.args[0].full_url,
                         "https://osu.ppy.sh/beatmapsets/search?page=4")

    def test_excluded_eligible_maps_do_not_consume_verification_budget(self):
        excluded_sets = [beatmapset(i, favourite_count=1000) for i in range(1, 10)]
        wanted = beatmapset(10)
        client, opener, _ = self.client([cursor_response(excluded_sets + [wanted], None), set_response(wanted)])
        batch = client.fetch_candidate_batch(4, 5, exclude_ids=range(10, 100, 10))
        self.assertEqual([item["id"] for item in batch["maps"]], [100])
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("/beatmapsets/10"))

    def test_partial_set_exclusions_and_verified_ids_are_rechecked(self):
        listed = beatmapset(beatmaps=[beatmap(120), beatmap(121)])
        verified = beatmapset(beatmaps=[beatmap(120), beatmap(121), beatmap(122)])
        client, opener, _ = self.client([cursor_response([listed], None), set_response(verified)])
        batch = client.fetch_candidate_batch(4, 5, exclude_ids={120, 122})
        self.assertEqual([item["id"] for item in batch["maps"]], [121])
        self.assertEqual(opener.open.call_count, 2)

    def test_stage_requirements_filter_before_verification_priority(self):
        unsuitable = [beatmapset(i, favourite_count=1000, beatmaps=[beatmap(i * 10, i, ar=9)])
                      for i in range(1, 10)]
        wanted = beatmapset(10, beatmaps=[beatmap(100, 10, ar=8)])
        requirements = [{"min_stars": 4.5, "max_stars": 4.5,
                         "max_bpm": 160, "max_ar": 8, "max_length": 150}]
        unchanged = copy.deepcopy(requirements)
        client, opener, _ = self.client([cursor_response(unsuitable + [wanted], None), set_response(wanted)])
        batch = client.fetch_candidate_batch(4, 5, requirements=requirements)
        self.assertEqual([item["id"] for item in batch["maps"]], [100])
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(requirements, unchanged)

    def test_each_stage_is_an_alternative_and_absent_or_none_caps_are_unlimited(self):
        item = beatmapset(beatmaps=[beatmap(120, difficulty_rating=4.2, ar=8),
                                   beatmap(121, difficulty_rating=4.8, ar=9.5),
                                   beatmap(122, difficulty_rating=4.6, ar=10)])
        requirements = [{"min_stars": 4, "max_stars": 4.3, "max_ar": 8},
                        {"min_stars": 4.7, "max_stars": 4.9, "max_bpm": None, "max_length": None}]
        client, _, _ = self.client([cursor_response([item], None), set_response(item)])
        batch = client.fetch_candidate_batch(4, 5, requirements=requirements)
        self.assertEqual([entry["id"] for entry in batch["maps"]], [120, 121])

    def test_all_limits_must_hold_in_one_stage_not_combined_across_stages(self):
        item = beatmapset(beatmaps=[beatmap(120, bpm=180, ar=9)])
        requirements = [{"min_stars": 4, "max_stars": 5, "max_bpm": 170, "max_ar": 10},
                        {"min_stars": 4, "max_stars": 5, "max_bpm": 190, "max_ar": 8}]
        client, opener, _ = self.client([cursor_response([item], None)])
        self.assertEqual(client.fetch_candidate_batch(4, 5, requirements=requirements)["maps"], [])
        self.assertEqual(opener.open.call_count, 1)

    def test_verification_rechecks_every_stage_limit(self):
        requirement = {"min_stars": 4, "max_stars": 5, "max_bpm": 160, "max_ar": 8.5, "max_length": 150}
        for change in ({"bpm": 160.001}, {"ar": 8.501}, {"total_length": 151},
                       {"difficulty_rating": 5.001}):
            with self.subTest(change=change):
                verified = beatmapset(beatmaps=[beatmap(**change)])
                client, _, _ = self.client([cursor_response([beatmapset()], None), set_response(verified)])
                self.assertEqual(client.fetch_candidate_batch(3, 6, requirements=[requirement])["maps"], [])

    def test_invalid_requirements_fail_before_network(self):
        client, opener, _ = self.client([])
        cases = [None, {}, [None], [{}], [{"min_stars": 5, "max_stars": 4}],
                 [{"min_stars": 4, "max_stars": 5, key: value}
                  for key in ("max_bpm", "max_ar", "max_length")
                  for value in (True, "160", 0, -1, float("nan"), float("inf"))]]
        flat_cases = cases[:-1] + cases[-1]
        for requirements in flat_cases:
            with self.subTest(requirements=requirements), self.assertRaises(ValueError):
                client.fetch_candidate_batch(4, 5, requirements=requirements)
        opener.open.assert_not_called()

    def test_invalid_cursor_fails_before_network(self):
        client, opener, _ = self.client([])
        cases = ["cursor", {}, {"version": 2, "page": 4}, {"version": True, "page": 4},
                 {"version": 1, "page": -1}, {"version": 1, "page": 4, "cursor_string": "x"},
                 {"version": 1, "page": 4, "cursor_string": "https://example.com",
                  "position": {"approved_date": 100, "id": 1}}]
        for cursor in cases:
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                client.fetch_candidate_batch(4, 5, cursor=cursor)
        opener.open.assert_not_called()

    def test_repeated_or_backward_server_cursor_stops_without_loop(self):
        cursor = {"version": 1, "page": 4, "cursor_string": "previous",
                  "position": {"approved_date": 1000, "id": 100}, "last_page": None}
        for token, date, set_id in [("previous", 999, 99), ("new", 1000, 100),
                                     ("new", 1001, 1), ("new", 1000, 101)]:
            with self.subTest(token=token, date=date, set_id=set_id):
                client, opener, _ = self.client([cursor_response([beatmapset(beatmaps=[])], token,
                                                                  date=date, set_id=set_id)])
                batch = client.fetch_candidate_batch(4, 5, cursor=cursor)
                self.assertTrue(batch["exhausted"])
                self.assertIsNone(batch["next_cursor"])
                self.assertEqual(opener.open.call_count, 1)

    def test_id_breaks_date_ties_in_descending_public_cursor(self):
        cursor = {"version": 1, "page": 4, "cursor_string": "previous",
                  "position": {"approved_date": 1000, "id": 100}, "last_page": None}
        client, opener, _ = self.client([cursor_response([beatmapset(beatmaps=[])], "next",
                                                         date=1000, set_id=99),
                                          cursor_response([], None)])
        batch = client.fetch_candidate_batch(4, 5, cursor=cursor)
        self.assertTrue(batch["exhausted"])
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("cursor_string=next"))

    def test_repeated_page_across_batches_is_detected_even_with_new_cursor(self):
        groups = [[beatmapset(i, beatmaps=[])] for i in range(1, 4)]
        first_responses = [cursor_response(group, f"cursor_{i}", date=1000 - i)
                           for i, group in enumerate(groups, 1)]
        client, opener, _ = self.client(first_responses + [cursor_response(groups[-1], "four", date=996)])
        first = client.fetch_candidate_batch(4, 5)
        second = client.fetch_candidate_batch(4, 5, cursor=first["next_cursor"])
        self.assertTrue(second["exhausted"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(opener.open.call_count, 4)

    def test_malformed_server_cursor_is_an_explicit_error(self):
        payload = {"beatmapsets": [beatmapset(beatmaps=[])], "cursor_string": "next",
                   "cursor": {"approved_date": "unknown", "id": 100}}
        client, opener, _ = self.client([Response(payload)])
        with self.assertRaises(source.DiscoverySourceError):
            client.fetch_candidate_batch(4, 5)
        self.assertEqual(opener.open.call_count, 1)

    def test_public_wrapper_passes_batch_options_and_legacy_wrapper_stays_list(self):
        expected = {"maps": [{"id": 120}], "next_cursor": None, "exhausted": True}
        options = {"cursor": None, "exclude_ids": {120}, "requirements": [{"min_stars": 4, "max_stars": 5}]}
        with patch.object(source._default_client, "fetch_candidate_batch", return_value=expected) as fetch:
            self.assertEqual(source.fetch_candidate_batch(4, 5, **options), expected)
            fetch.assert_called_once_with(4, 5, **options)
            self.assertEqual(source.fetch_candidates(4, 5), expected["maps"])


if __name__ == "__main__":
    unittest.main()
