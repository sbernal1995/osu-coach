"""Favorite songs power a dedicated repeat-until-FC board with difficulty ladders."""
from copy import deepcopy
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from osu_coach.app import Handler
from osu_coach.core.song_identity import song_tokens
from osu_coach.storage.quest_store import scope_key
from tests import test_played_recommendations as fixtures


def quests(board):
    return fixtures.PlayedRecommendationsTests.quests(board)


def in_favorites_mode(state):
    return state["recommendation_policy"]["mode"] == "favorites"


class FavoriteStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlayedRecommendationsTests()
        self.fixture.setUp()
        self.coach = self.fixture.coach
        self.board = self.fixture.board()
        self.quest = self.fixture.quests(self.board)[0]

    def tearDown(self):
        self.fixture.tearDown()

    def favorite(self):
        self.coach.favorite_song(self.board["id"], self.quest["id"])
        return self.coach.state()

    def activate(self):
        self.coach.update_settings({"favorites_enabled": True})
        return self.coach.state()

    def deactivate(self):
        self.coach.update_settings({"favorites_enabled": False})
        return self.coach.state()

    def shelf(self):
        board = self.coach.quest_store.current(scope_key(self.coach.active, self.coach.config["since"]))
        return list(board.get("shelf") or []) if board else []

    def test_mark_keeps_normal_board_until_mode_is_toggled(self):
        before_plays = self.coach.db.execute("SELECT * FROM plays").fetchall()
        state = self.favorite()
        self.assertEqual(1, state["favorites"]["total"])
        self.assertFalse(in_favorites_mode(state))
        self.assertEqual(self.board, state["quest_board"])
        self.assertEqual(before_plays, self.coach.db.execute("SELECT * FROM plays").fetchall())
        availability = state["quest_availability"][self.quest["id"]]
        self.assertTrue(availability["favorite"])
        self.assertTrue(availability["favorite_id"])

    def test_favorites_survive_restart_and_reset_but_belong_to_player(self):
        state = self.favorite()
        self.fixture.reopen()
        self.coach = self.fixture.coach
        self.assertEqual(state["favorites"], self.coach.state()["favorites"])
        self.coach.reset()
        self.assertEqual(state["favorites"], self.coach.state()["favorites"])
        self.coach.add_play(self.fixture.result(player="Another player"))
        self.assertEqual(0, self.coach.state()["favorites"]["total"])
        self.coach.unfavorite_song(state["favorites"]["items"][0]["id"])
        self.coach.unfavorite_song(state["favorites"]["items"][0]["id"])
        self.coach.active = self.fixture.active
        self.assertEqual(1, self.coach.state()["favorites"]["total"])

    def test_favorites_mode_board_only_uses_favorite_song(self):
        self.favorite()
        state = self.activate()
        self.assertTrue(in_favorites_mode(state))
        tokens = song_tokens(self.quest["map"])
        active = [q for q in quests(state["quest_board"])
                  if q["status"] in {"pending", "in_progress"}]
        self.assertTrue(active)
        self.assertTrue(all(song_tokens(q["map"]) & tokens for q in active))
        self.assertTrue(all(song_tokens(m) & tokens
                            for g in state["recommendations"] for m in g["maps"]))
        self.assertEqual([], state["discovery"]["needs"])
        self.assertEqual([], state["discovery"]["search_needs"])

    def test_favorites_mode_without_favorites_pauses_everything(self):
        state = self.activate()
        self.assertTrue(in_favorites_mode(state))
        self.assertEqual(0, state["favorites"]["total"])
        board = state["quest_board"]
        self.assertIsNotNone(board)
        self.assertEqual("favorites", board["mode"])
        self.assertEqual(0, sum(q["status"] in {"pending", "in_progress"} for q in quests(board)))
        self.assertEqual(0, state["quest_skips"]["total"])
        shelved = self.shelf()
        self.assertTrue(shelved)
        self.assertTrue(all(q["status"] in {"pending", "in_progress"} for q in shelved))
        self.assertEqual([], state["discovery"]["needs"])
        self.assertEqual([], state["discovery"]["search_needs"])
        self.assertEqual([], fixtures.PlayedRecommendationsTests.recommended(state))
        resumed = self.deactivate()
        self.assertFalse(in_favorites_mode(resumed))
        back = [q for q in quests(resumed["quest_board"]) if q["status"] in {"pending", "in_progress"}]
        self.assertTrue(back)
        self.assertEqual([], self.shelf())
        self.assertEqual(0, resumed["quest_skips"]["total"])

    def test_existing_missions_pause_and_resume_when_mode_toggles(self):
        state = self.favorite()
        skips_before = state["quest_skips"]["total"]
        other = next(q for q in quests(state["quest_board"]) if q["id"] != self.quest["id"])
        state = self.activate()
        self.assertEqual(skips_before, state["quest_skips"]["total"])
        board = state["quest_board"]
        self.assertEqual("favorites", board["mode"])
        self.assertEqual(1, state["favorites"]["total"])
        active = [q for q in quests(board) if q["status"] in {"pending", "in_progress"}]
        self.assertTrue(active)
        self.assertTrue(all(song_tokens(q["map"]) & song_tokens(self.quest["map"]) for q in active))
        shelved = {q["id"]: q for q in self.shelf()}
        self.assertIn(other["id"], shelved)
        self.assertEqual(other, {k: v for k, v in shelved[other["id"]].items() if k != "shelved_from_stage"})
        resumed = self.deactivate()
        self.assertFalse(in_favorites_mode(resumed))
        self.assertEqual("unplayed", resumed["quest_board"]["mode"])
        back = {q["id"]: q for q in quests(resumed["quest_board"])}
        self.assertIn(other["id"], back)
        self.assertEqual(other, back[other["id"]])
        self.assertEqual(skips_before, resumed["quest_skips"]["total"])

    def test_started_mission_pauses_with_attempts_and_resumes(self):
        quest = self.quest
        attempt = self.fixture.result(quest["map"], played_at=datetime.now(timezone.utc).isoformat(),
                                      accuracy=80, misses=20, grade="B", max_combo=100)
        self.coach.add_play(attempt)
        started = next(q for q in quests(self.coach.state()["quest_board"]) if q["id"] == quest["id"])
        self.assertEqual("in_progress", started["status"])
        self.assertEqual(1, started["attempt_count"])
        self.assertIsNotNone(started["last_attempt"])
        skips_before = self.coach.state()["quest_skips"]["total"]
        self.favorite()
        state = self.activate()
        visible = {q["id"] for q in quests(state["quest_board"])}
        self.assertNotIn(quest["id"], visible)
        shelved = {q["id"]: q for q in self.shelf()}
        self.assertIn(quest["id"], shelved)
        paused = shelved[quest["id"]]
        self.assertEqual("in_progress", paused["status"])
        self.assertEqual(1, paused["attempt_count"])
        self.assertEqual(started["last_attempt"], paused["last_attempt"])
        self.assertEqual(skips_before, state["quest_skips"]["total"])
        resumed = self.deactivate()
        back = {q["id"]: q for q in quests(resumed["quest_board"])}
        self.assertIn(quest["id"], back)
        self.assertEqual("in_progress", back[quest["id"]]["status"])
        self.assertEqual(1, back[quest["id"]]["attempt_count"])
        self.assertEqual(started["last_attempt"], back[quest["id"]]["last_attempt"])
        paused = self.shelf()
        self.assertTrue(paused)
        self.assertNotIn(quest["id"], {q["id"] for q in paused})
        self.assertTrue(all(q["map"].get("favorite") is not None for q in paused))
        self.assertEqual(skips_before, resumed["quest_skips"]["total"])

    def test_mode_toggle_round_trips_missions_without_duplicates(self):
        self.favorite()
        before = {q["id"] for q in quests(self.coach.state()["quest_board"])
                  if q["status"] in {"pending", "in_progress"}}
        self.assertTrue(before)
        skips_before = self.coach.state()["quest_skips"]["total"]
        state = self.activate()
        during = {q["id"] for q in quests(state["quest_board"])
                  if q["status"] in {"pending", "in_progress"}}
        self.assertTrue(during)
        self.assertTrue(during.isdisjoint(before))
        self.assertEqual(before, {q["id"] for q in self.shelf()})
        self.assertEqual(skips_before, state["quest_skips"]["total"])
        resumed = self.deactivate()
        back = {q["id"] for q in quests(resumed["quest_board"])
                if q["status"] in {"pending", "in_progress"}}
        self.assertEqual(before, back)
        self.assertEqual(during, {q["id"] for q in self.shelf()})
        self.assertEqual(skips_before, resumed["quest_skips"]["total"])
        reco_keys = {m["key"] for g in resumed["recommendations"] for m in g["maps"]}
        shelved_keys = {q["map"]["key"] for q in self.shelf()}
        self.assertTrue(reco_keys.isdisjoint(shelved_keys))

    def test_manual_renewal_keeps_paused_missions(self):
        self.favorite()
        state = self.activate()
        paused = {q["id"] for q in self.shelf()}
        self.assertTrue(paused)
        renewed = self.coach.new_quests(state["quest_board"]["id"])
        self.assertNotEqual(state["quest_board"]["id"], renewed["id"])
        self.assertEqual(paused, {q["id"] for q in self.shelf()})
        self.assertEqual("favorites", renewed["mode"])

    def test_import_favorites_from_links_and_ids(self):
        maps = self.fixture.maps[:3]
        text = (
            f"https://osu.ppy.sh/beatmapsets/{maps[0]['set_id']}#osu/{maps[0]['id']}\n"
            f"https://osu.ppy.sh/b/{maps[1]['id']}\n"
            f"{maps[2]['id']}"
        )
        result = self.coach.import_favorites(text)
        self.assertEqual({"added": 3, "already": 0, "missing": []}, result)
        self.assertEqual(3, self.coach.state()["favorites"]["total"])
        again = self.coach.import_favorites(text)
        self.assertEqual({"added": 0, "already": 3, "missing": []}, again)
        self.assertEqual(3, self.coach.state()["favorites"]["total"])

    def test_import_favorite_set_marks_the_whole_song_once(self):
        extra = dict(self.fixture.maps[0], key="played-map-extra", id=61000, version="Hard")
        self.coach.catalog.append(extra)
        result = self.coach.import_favorites(f"https://osu.ppy.sh/beatmapsets/{self.fixture.maps[0]['set_id']}")
        self.assertEqual(1, result["added"])
        self.assertEqual([], result["missing"])
        self.assertEqual(1, self.coach.state()["favorites"]["total"])

    def test_import_reports_ids_missing_from_the_library(self):
        result = self.coach.import_favorites("https://osu.ppy.sh/beatmapsets/424242#osu/99999999\n424243")
        self.assertEqual(0, result["added"])
        self.assertEqual(0, result["already"])
        self.assertEqual(["99999999", "424243"], result["missing"])
        self.assertEqual(0, self.coach.state()["favorites"]["total"])

    def test_import_rejects_empty_and_oversized_text(self):
        for text in ("", "   ", "hola mundo sin numeros largos"):
            with self.assertRaises(ValueError):
                self.coach.import_favorites(text)
        with self.assertRaises(ValueError):
            self.coach.import_favorites("x" * 30001)

    def test_imported_songs_appear_in_favorites_mode(self):
        maps = self.fixture.maps[:2]
        self.coach.import_favorites(f"https://osu.ppy.sh/b/{maps[0]['id']}\n{maps[1]['id']}")
        state = self.activate()
        active = [q for q in quests(state["quest_board"]) if q["status"] in {"pending", "in_progress"}]
        self.assertTrue(active)
        wanted = set().union(*(song_tokens(m) for m in maps))
        self.assertTrue(all(song_tokens(q["map"]) & wanted for q in active))

    def test_favorite_candidates_offer_every_unpeaked_difficulty(self):
        from osu_coach.core.training import favorite_candidates
        low = dict(self.fixture.maps[0], stars=2.0)
        high = dict(low, key="played-map-high", id=61001, version="Hard", stars=4.8)
        offered = favorite_candidates([low, high], song_tokens(low), [])
        self.assertEqual({"played-map-0", "played-map-high"}, {m["key"] for m in offered})
        self.assertTrue(all(m["favorite"] is not None for m in offered))

    def test_favorite_candidates_replay_peaked_difficulties(self):
        from osu_coach.core.training import favorite_candidates
        low = dict(self.fixture.maps[0], stars=2.0)
        high = dict(low, key="played-map-high", id=61001, version="Hard", stars=4.8)
        peaked = self.fixture.result(low, accuracy=100, misses=0,
                                     max_combo=low["max_combo"], grade="SS")
        offered = favorite_candidates([low, high], song_tokens(low), [peaked])
        self.assertEqual({"played-map-0", "played-map-high"}, {m["key"] for m in offered})
        replayed = next(m for m in offered if m["key"] == "played-map-0")
        self.assertIn("reference", replayed["favorite"])
        lifted = next(m for m in offered if m["key"] == "played-map-high")
        self.assertIn("escalation", lifted["favorite"])

    def test_favorites_mode_assigns_level_appropriate_difficulties(self):
        high = dict(self.fixture.maps[0], key="played-map-high", id=61001,
                    version="Hard", stars=4.8)
        self.coach.catalog.append(high)
        self.coach.favorite_song(self.board["id"], self.quest["id"])
        state = self.activate()
        keys = {q["map"]["key"] for q in quests(state["quest_board"])
                if q["status"] in {"pending", "in_progress"}}
        self.assertIn("played-map-high", keys)

    def test_improvement_mission_targets_your_best_until_fc(self):
        played = deepcopy(self.quest["map"])
        self.fixture.stored(played, accuracy=98, misses=0, max_combo=470)
        self.favorite()
        state = self.activate()
        target = next(q for q in quests(state["quest_board"])
                      if q["map"]["key"] == played["key"])
        expectation = target["map"]["expectation"]
        self.assertTrue(expectation["favorite_reference"])
        self.assertEqual(470, expectation["favorite_reference"]["max_combo"])
        self.assertEqual(["complete", "accuracy"], expectation["required_keys"])
        self.assertGreater(expectation["accuracy_min"], 98)
        self.assertIn("mejorá tu marca", expectation["basis"])

    def test_unpeaked_finish_renews_the_same_difficulty_without_cooldown(self):
        played = deepcopy(self.quest["map"])
        self.fixture.stored(played, accuracy=98, misses=0, max_combo=470)
        self.favorite()
        state = self.activate()
        before_skips = state["quest_skips"]["total"]
        target = next(q for q in quests(state["quest_board"]) if q["map"]["key"] == played["key"])
        self.coach.add_play(self.fixture.result(target["map"], accuracy=99.5, misses=0,
                                                max_combo=480, grade="S",
                                                played_at=datetime.now(timezone.utc).isoformat()))
        continued = self.coach.state()
        self.assertEqual(1, continued["quest_completions"]["total"])
        self.assertEqual(before_skips, continued["quest_skips"]["total"])
        renewed = next(q for q in quests(continued["quest_board"])
                       if q["map"]["key"] == played["key"])
        self.assertEqual("pending", renewed["status"])
        self.assertEqual(0, renewed["attempt_count"])
        self.assertEqual(480, renewed["map"]["expectation"]["favorite_reference"]["max_combo"])
        self.assertEqual(target["id"], renewed["replaces_quest_id"])

    def test_peaked_favorite_escalates_to_the_next_difficulty_of_the_song(self):
        low = deepcopy(self.quest["map"])
        high = deepcopy(low)
        high.update(key="favorite-high-difficulty", id=99040, version="Heavy", stars=4.8)
        self.coach.catalog = [low]
        self.favorite()
        state = self.activate()
        first = next(q for q in quests(state["quest_board"]) if q["map"]["key"] == low["key"])
        self.coach.add_play(self.fixture.result(first["map"], accuracy=100, misses=0,
                                                max_combo=low["max_combo"], grade="SS",
                                                played_at=datetime.now(timezone.utc).isoformat()))
        escalated = self.coach.state()
        self.assertEqual(1, escalated["quest_completions"]["total"])
        self.coach.catalog.append(high)
        escalated = self.coach.state()
        offered = {q["map"]["key"] for q in quests(escalated["quest_board"])
                   if q["status"] in {"pending", "in_progress"}}
        self.assertIn(high["key"], offered)
        self.assertIn(low["key"], offered)
        lifted = next(q for q in quests(escalated["quest_board"]) if q["map"]["key"] == high["key"])
        self.assertIn("subís de dificultad", lifted["map"]["expectation"]["basis"])
        replay = next(q for q in quests(escalated["quest_board"]) if q["map"]["key"] == low["key"])
        self.assertIn("mantené tu FC", replay["map"]["expectation"]["basis"])

    def test_peaked_favorite_returns_as_maintenance_replay(self):
        low = deepcopy(self.quest["map"])
        self.coach.catalog = [low]
        self.favorite()
        state = self.activate()
        first = next(q for q in quests(state["quest_board"]) if q["map"]["key"] == low["key"])
        self.coach.add_play(self.fixture.result(first["map"], accuracy=100, misses=0,
                                                max_combo=low["max_combo"], grade="SS",
                                                played_at=datetime.now(timezone.utc).isoformat()))
        renewed = self.coach.state()
        replay = next(q for q in quests(renewed["quest_board"]) if q["map"]["key"] == low["key"])
        self.assertEqual("pending", replay["status"])
        self.assertIn("mantené tu FC", replay["map"]["expectation"]["basis"])

    def test_favorites_do_not_replace_normal_pool_and_cannot_override_a_ban(self):
        self.favorite()
        banned = self.quest["map"]
        self.coach.catalog = deepcopy(self.fixture.maps)
        self.coach.ban_song(self.board["id"], self.quest["id"])
        self.coach.update_settings({"favorites_enabled": True})
        state = self.coach.state()
        blocked = song_tokens(banned)
        self.assertTrue(all(not song_tokens(q["map"]) & blocked for q in quests(state["quest_board"])))
        self.assertFalse(any(song_tokens(m) & blocked
                             for g in state["recommendations"] for m in g["maps"]))
        self.assertEqual(1, state["favorites"]["total"])

    def test_favorite_repeats_stay_eligible_for_practice_like_other_missions(self):
        practice = next(m for m in self.fixture.maps if m["stars"] == 4.65)
        from osu_coach.core.song_identity import song_owner
        with self.coach.db:
            self.coach.favorites.favorite(song_owner(self.coach.active), practice)
        self.coach.update_settings({"favorites_enabled": True})
        state = self.coach.state()
        board = state["quest_board"]
        self.assertIsNotNone(board)
        active = [q for q in quests(board) if q["status"] in {"pending", "in_progress"}]
        self.assertTrue(active)
        placed = next(q for q in active if q["map"]["key"] == practice["key"])
        self.assertEqual("favorite", placed["map"]["training_role"])
        self.assertTrue(placed["map"]["training_progress"]["eligible"])
        self.assertEqual(True, placed["map"]["expectation"].get("complete_required"))

    def test_stale_mission_rejected_without_creating_an_unrelated_favorite(self):
        for board, quest in (("stale", self.quest["id"]), (self.board["id"], "stale")):
            with self.assertRaises(ValueError):
                self.coach.favorite_song(board, quest)
        self.assertEqual(0, self.coach.state()["favorites"]["total"])

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
                ("add", body, False, None, 403),
                ("add", body, True, "https://outside.example", 403),
                ("add", {"title": "arbitrary song"}, True, None, 400),
                ("remove", {"id": 2}, True, None, 400),
                ("remove", {"id": "x"}, False, None, 403)]:
                with self.subTest(route=route, code=code), self.assertRaises(HTTPError) as caught:
                    send("/api/favorites/" + route, payload, token, origin)
                self.assertEqual(code, caught.exception.code)
            with send("/api/favorites/add", body) as response:
                self.assertTrue(json.load(response)["ok"])
            identifier = self.coach.state()["favorites"]["items"][0]["id"]
            with send("/api/favorites/remove", {"id": identifier}) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertEqual(0, self.coach.state()["favorites"]["total"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()