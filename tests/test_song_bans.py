"""Music preferences exclude whole songs without changing performance evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from osu_coach.app import Handler, scope_key
from osu_coach.core.song_identity import song_tokens, song_owner
from tests import test_played_recommendations as fixtures


class SongBanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlayedRecommendationsTests()
        self.fixture.setUp()
        self.coach = self.fixture.coach
        self.board = self.fixture.board()
        self.quest = self.fixture.quests(self.board)[0]

    def tearDown(self):
        self.fixture.tearDown()

    def ban(self):
        self.coach.ban_song(self.board["id"], self.quest["id"])
        return self.coach.state()

    def test_ban_replaces_song_and_all_difficulties_without_touching_other_missions_or_history(self):
        song = self.quest["map"]
        self.coach.catalog.append({**song, "key": "another-difficulty", "id": 700001, "version": "Hard"})
        self.coach.catalog.append({**song, "key": "another-mapper", "id": 700002, "set_id": 700002})
        before_plays = self.coach.db.execute("SELECT * FROM plays").fetchall()
        before_progress = self.coach.db.execute("SELECT * FROM coach_progress_points").fetchall()
        after = self.ban()
        blocked = song_tokens(song)
        self.assertEqual(1, after["song_bans"]["total"])
        self.assertFalse(any(song_tokens(q["map"]) & blocked for q in self.fixture.quests(after["quest_board"])))
        self.assertFalse(any(song_tokens(m) & blocked for g in after["recommendations"] for m in g["maps"]))
        current = {q["id"]: q for q in self.fixture.quests(after["quest_board"])}
        for q in self.fixture.quests(self.board)[1:]:
            self.assertEqual(q, current[q["id"]])
        self.assertEqual(9, len(current))
        self.assertEqual("song_banned", after["quest_skips"]["items"][0]["skipped_reason"])
        self.assertEqual(0, after["quest_completions"]["total"])
        self.assertEqual(before_plays, self.coach.db.execute("SELECT * FROM plays").fetchall())
        self.assertEqual(before_progress, self.coach.db.execute("SELECT * FROM coach_progress_points").fetchall())

    def test_preferences_survive_restart_reset_and_mod_changes_but_belong_to_player(self):
        state = self.ban()
        self.fixture.reopen()
        self.coach = self.fixture.coach
        self.assertEqual(state["song_bans"], self.coach.state()["song_bans"])
        self.coach.reset()
        self.assertEqual(state["song_bans"], self.coach.state()["song_bans"])
        original = self.coach.active
        identity = json.loads(original)
        identity[1] = "stable"
        identity[3] = '{"mods":["HD"]}'
        self.assertEqual(song_owner(original), song_owner(json.dumps(identity)))
        self.coach.add_play(self.fixture.result(player="Another player"))
        self.assertEqual(0, self.coach.state()["song_bans"]["total"])
        self.coach.unban_song(state["song_bans"]["items"][0]["id"])
        self.coach.active = original
        self.assertEqual(1, self.coach.state()["song_bans"]["total"])

    def test_unban_restores_eligibility_and_preserves_withdrawal_log(self):
        state = self.ban()
        identifier = state["song_bans"]["items"][0]["id"]
        self.coach.unban_song(identifier)
        self.coach.unban_song(identifier)
        after = self.coach.state()
        self.assertEqual(0, after["song_bans"]["total"])
        self.assertEqual(state["quest_skips"], after["quest_skips"])
        self.assertTrue(any(m["id"] == self.quest["map"]["id"] for g in after["recommendations"] for m in g["maps"]))

    def test_started_mission_can_be_banned_without_erasing_its_attempt(self):
        self.coach.add_play(self.fixture.result(self.quest["map"], accuracy=80, misses=20, grade="B", max_combo=100,
                                               played_at=datetime.now(timezone.utc).isoformat()))
        before = self.coach.db.execute("SELECT * FROM quest_attempts").fetchall()
        state = self.ban()
        retired = state["quest_skips"]["items"][0]
        self.assertEqual(1, retired["attempt_count"])
        self.assertEqual("song_banned", retired["skipped_reason"])
        self.assertEqual(before, self.coach.db.execute("SELECT * FROM quest_attempts").fetchall())

    def test_playing_map_and_unconfirmed_result_are_kept_until_safe_to_replace(self):
        self.coach.last_snapshot = {"state": {"name": "play"}, "beatmap": {"id": self.quest["map"]["id"]}}
        state = self.ban()
        self.assertTrue(any(q["id"] == self.quest["id"] for q in self.fixture.quests(state["quest_board"])))
        self.coach.last_snapshot = None
        pending = self.fixture.result(self.quest["map"], needs_confirmation=True,
                                      played_at=datetime.now(timezone.utc).isoformat())
        self.coach.add_play(pending)
        self.assertTrue(any(q["id"] == self.quest["id"] for q in self.fixture.quests(self.coach.state()["quest_board"])))
        self.coach.confirm(pending["id"], False)
        self.assertFalse(any(q["id"] == self.quest["id"] for q in self.fixture.quests(self.coach.state()["quest_board"])))

    def test_no_replacement_hides_retired_mission_and_requests_more_maps(self):
        self.coach.catalog = [self.quest["map"]]
        state = self.ban()
        retired = next(q for q in self.fixture.quests(state["quest_board"]) if q["id"] == self.quest["id"])
        self.assertEqual("skipped", retired["status"])
        self.assertTrue(state["discovery"]["needs"])

    def test_stale_mission_rejected_without_creating_an_unrelated_ban(self):
        for board, quest in (("stale", self.quest["id"]), (self.board["id"], "stale")):
            with self.assertRaises(ValueError):
                self.coach.ban_song(board, quest)
        self.assertEqual(0, self.coach.state()["song_bans"]["total"])

    def test_api_requires_token_origin_and_valid_mission_references(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.coach = self.coach
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def send(path, body, token=True, origin=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Coach-Token"] = self.coach.token
            if origin:
                headers["Origin"] = origin
            return urlopen(Request(f"http://127.0.0.1:{server.server_port}" + path,
                           data=json.dumps(body).encode(), headers=headers), timeout=3)
        body = {"board_id": self.board["id"], "quest_id": self.quest["id"]}
        try:
            for route, payload, token, origin, code in [
                ("ban", body, False, None, 403), ("ban", body, True, "https://outside.example", 403),
                ("ban", {"title": "arbitrary song"}, True, None, 400),
                ("unban", {"id": 2}, True, None, 400), ("unban", {"id": "x"}, False, None, 403)]:
                with self.subTest(route=route, code=code), self.assertRaises(HTTPError) as caught:
                    send("/api/songs/" + route, payload, token, origin)
                self.assertEqual(code, caught.exception.code)
            with send("/api/songs/ban", body) as response:
                self.assertTrue(json.load(response)["ok"])
            identifier = self.coach.state()["song_bans"]["items"][0]["id"]
            with send("/api/songs/unban", {"id": identifier}) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertEqual(0, self.coach.state()["song_bans"]["total"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class SongIdentityTests(unittest.TestCase):
    def test_japanese_and_romanized_aliases_match_across_sets_and_case(self):
        original = {"set_id": 10, "title": "春待ちクローバー", "title_romanized": "Harumachi Clover",
                    "artist": "花坂結衣", "artist_romanized": "Hanasaka Yui"}
        other = {"set_id": 11, "title": "  HARUMACHI  CLOVER ", "artist": "Hanasaka Yui"}
        self.assertTrue(song_tokens(original) & song_tokens(other))
        self.assertTrue(song_tokens({"set_id": 10}) & song_tokens(original))
        self.assertFalse(song_tokens(original) & song_tokens({**other, "artist": "Different artist"}))
        self.assertFalse(song_tokens(original) & song_tokens({**other, "title": "Harumachi Clover (Remix)"}))
        self.assertEqual(set(), song_tokens({"title": "Song", "set_id": -1}))
