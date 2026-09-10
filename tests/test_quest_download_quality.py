"""Download-quality migration preserves installed maps, active work and achievements."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from tests import test_played_recommendations as fixtures
from osu_coach import app


def quality(**changes):
    return {"scope": "beatmapset", "rating": 8.5, "votes": 20, "rating_votes": 20,
            "play_count": 20000, **changes}


class QuestDownloadQualityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlayedRecommendationsTests()
        self.fixture.setUp()
        self.coach = self.fixture.coach
        self.before = self.fixture.board()

    def tearDown(self):
        self.fixture.tearDown()

    def remote(self, board, index=0, popularity=None, *, installed=False):
        quest = self.fixture.quests(board)[index]
        identifier = quest["map"]["id"]
        if not installed:
            self.coach.catalog = [m for m in self.coach.catalog if m["id"] != identifier]
        quest["map"].update(key=f"remote:{identifier}", source="online", local=False)
        if popularity is None:
            quest["map"].pop("popularity", None)
        else:
            quest["map"]["popularity"] = deepcopy(popularity)
        return quest

    def persist(self, board):
        data = json.dumps(board, ensure_ascii=False)
        with self.coach.db:
            self.coach.db.execute("UPDATE quest_boards SET data=? WHERE id=?", (data, board["id"]))
        return data

    def reopen(self):
        self.fixture.reopen()
        self.coach = self.fixture.coach

    def test_unknown_legacy_download_is_retired_without_fake_achievement_and_log_survives(self):
        old = self.remote(self.before)
        self.persist(self.before)
        plays = self.coach.db.execute("SELECT * FROM plays ORDER BY rowid").fetchall()
        state = self.coach.state()
        current = {q["id"]: q for q in self.fixture.quests(state["quest_board"])}
        self.assertEqual(self.before["id"], state["quest_board"]["id"])
        self.assertNotIn(old["id"], current)
        for quest in self.fixture.quests(self.before)[1:]:
            self.assertEqual(quest, current[quest["id"]])
        self.assertEqual(1, state["quest_skips"]["total"])
        retired = state["quest_skips"]["items"][0]
        self.assertEqual(old["id"], retired["id"])
        self.assertEqual("download_quality", retired["skipped_reason"])
        self.assertEqual(old["map"], retired["map"])
        self.assertEqual(0, retired["attempt_count"])
        self.assertIsNone(retired["last_attempt"])
        self.assertIsNone(retired["completed_play_id"])
        self.assertEqual(0, state["quest_completions"]["total"])
        self.assertEqual(0, state["quest_board"]["completed_count"])
        self.assertEqual(0, state["quest_board"]["retired_count"])
        self.assertEqual(1, state["quest_board"]["skipped_count"])
        self.assertEqual(plays, self.coach.db.execute("SELECT * FROM plays ORDER BY rowid").fetchall())
        self.assertEqual(state["quest_board"], self.coach.state()["quest_board"])
        self.reopen()
        self.assertEqual(state["quest_board"], self.coach.state()["quest_board"])
        self.assertEqual(state["quest_skips"], self.coach.state()["quest_skips"])
        self.coach.reset()
        self.assertEqual(state["quest_skips"], self.coach.state()["quest_skips"])
        self.coach.add_play(self.fixture.result(player="Another player"))
        self.assertEqual(0, self.coach.state()["quest_skips"]["total"])

    def test_current_valid_evidence_keeps_legacy_goal_and_updates_only_availability(self):
        quest = self.remote(self.before)
        frozen = self.persist(self.before)
        current = deepcopy(quest["map"])
        current.update(key="current-discovery-key", popularity=quality())
        self.coach.discovery_store.maps = [current]
        app.save_json(self.coach.discovery_store.path, {"version": 1, "fetched_epoch": 1,
                      "baseline": 4.5, "maps": [current]})
        state = self.coach.state()
        self.assertEqual(self.before, state["quest_board"])
        self.assertEqual(quality(), state["quest_availability"][quest["id"]]["popularity"])
        self.assertFalse(state["quest_availability"][quest["id"]]["installed"])
        self.assertEqual(frozen, self.coach.db.execute(
            "SELECT data FROM quest_boards WHERE id=?", (self.before["id"],)).fetchone()[0])
        self.assertEqual(0, state["quest_skips"]["total"])
        state["quest_availability"][quest["id"]]["popularity"]["rating"] = 1
        self.assertEqual(quality(), self.coach.discovery_store.maps[0]["popularity"])
        self.reopen()
        restored = self.coach.state()
        self.assertEqual(self.before, restored["quest_board"])
        self.assertEqual(quality(), restored["quest_availability"][quest["id"]]["popularity"])

    def test_valid_frozen_evidence_is_used_when_no_new_evidence_exists(self):
        quest = self.remote(self.before, popularity=quality(rating=8, play_count=10000))
        self.persist(self.before)
        state = self.coach.state()
        self.assertEqual(self.before, state["quest_board"])
        self.assertEqual(quest["map"]["popularity"], state["quest_availability"][quest["id"]]["popularity"])
        self.assertEqual(0, state["quest_skips"]["total"])

    def test_updated_low_play_count_overrides_old_evidence_and_other_difficulty_cannot_rescue_it(self):
        quest = self.remote(self.before, popularity=quality())
        self.persist(self.before)
        latest = deepcopy(quest["map"])
        latest.update(key="updated-exact-difficulty", popularity=quality(play_count=9999))
        other = deepcopy(latest)
        other.update(key="other-difficulty", id=999999, popularity=quality(play_count=100000))
        self.coach.discovery_store.maps = [latest, other]
        state = self.coach.state()
        self.assertEqual(1, state["quest_skips"]["total"])
        self.assertEqual(quest["id"], state["quest_skips"]["items"][0]["id"])
        self.assertEqual("download_quality", state["quest_skips"]["items"][0]["skipped_reason"])
        self.assertEqual(0, state["quest_completions"]["total"])

    def test_downloaded_online_mission_is_kept_by_local_id_even_without_quality(self):
        quest = self.remote(self.before, installed=True)
        self.persist(self.before)
        self.assertNotIn(quest["map"]["key"], {m["key"] for m in self.coach.catalog})
        state = self.coach.state()
        self.assertEqual(self.before, state["quest_board"])
        self.assertTrue(state["quest_availability"][quest["id"]]["installed"])
        self.assertEqual(0, state["quest_skips"]["total"])
        self.assertEqual(0, state["quest_completions"]["total"])

    def test_started_and_completed_downloads_are_not_reclassified_as_quality_skips(self):
        started = self.remote(self.before, 0, quality(rating=7))
        completed = self.remote(self.before, 1, quality(play_count=100))
        self.persist(self.before)
        self.coach.add_play(self.fixture.result(started["map"], played_at=datetime.now(timezone.utc).isoformat(),
                                               accuracy=80, grade="B", misses=20, max_combo=100))
        self.coach.add_play(self.fixture.result(completed["map"], played_at=datetime.now(timezone.utc).isoformat(),
                                               accuracy=100, grade="SS", max_combo=500))
        scope = app.scope_key(self.coach.active, self.coach.config["since"])
        before_refresh = self.coach.quest_store.current(scope)
        saved_started = next(q for q in self.fixture.quests(before_refresh) if q["id"] == started["id"])
        log = self.coach.quest_store.completions(scope)
        state = self.coach.state()
        actual = next(q for q in self.fixture.quests(state["quest_board"]) if q["id"] == started["id"])
        self.assertEqual(saved_started, actual)
        self.assertEqual("in_progress", actual["status"])
        self.assertEqual(1, actual["attempt_count"])
        self.assertEqual(log, state["quest_completions"])
        self.assertEqual(1, log["total"])
        self.assertEqual(completed["id"], log["items"][0]["id"])
        self.assertEqual(completed["map"], log["items"][0]["map"])
        self.assertEqual(0, state["quest_skips"]["total"])

    def test_current_play_and_its_pending_confirmation_protect_unknown_download(self):
        quest = self.remote(self.before)
        self.persist(self.before)
        self.coach.last_snapshot = {"state": {"number": 2}, "beatmap": {"id": quest["map"]["id"],
                                                                            "checksum": "downloaded-checksum"}}
        self.assertEqual(self.before, self.coach.state()["quest_board"])
        pending = self.fixture.result(quest["map"], played_at=datetime.now(timezone.utc).isoformat(),
                                      needs_confirmation=True, accuracy=80, grade="B", misses=20, max_combo=100)
        self.coach.add_play(pending)
        self.coach.last_snapshot = {"state": {"number": 7}}
        self.assertEqual(self.before, self.coach.state()["quest_board"])
        self.coach.confirm(pending["id"], True)
        state = self.coach.state()
        current = next(q for q in self.fixture.quests(state["quest_board"]) if q["id"] == quest["id"])
        self.assertEqual("in_progress", current["status"])
        self.assertEqual(quest["map"], current["map"])
        self.assertEqual(0, state["quest_skips"]["total"])

    def test_missing_alternative_waits_then_refills_only_with_quality_download(self):
        quest = self.remote(self.before)
        self.persist(self.before)
        self.coach.catalog = [deepcopy(q["map"]) for q in self.fixture.quests(self.before)[1:]]
        waiting = self.coach.state()
        self.assertEqual(1, waiting["quest_board"]["skipped_waiting_count"])
        self.assertEqual(8, waiting["quest_board"]["active_count"])
        fresh = deepcopy(quest["map"])
        fresh.update(key="remote:999998", id=999998, set_id=999998, title="New suitable download",
                     popularity=quality())
        self.coach.discovery_store.maps = [fresh]
        self.coach.discovery_store.candidates.return_value = [fresh]
        state = self.coach.state()
        self.assertEqual(self.before["id"], state["quest_board"]["id"])
        self.assertEqual(0, state["quest_board"]["skipped_waiting_count"])
        self.assertEqual(9, state["quest_board"]["active_count"])
        new_quest = next(q for q in self.fixture.quests(state["quest_board"]) if q["map"]["id"] == fresh["id"])
        self.assertEqual(quality(), state["quest_availability"][new_quest["id"]]["popularity"])
        self.assertEqual(waiting["quest_skips"], state["quest_skips"])
        self.assertEqual(0, state["quest_completions"]["total"])


if __name__ == "__main__":
    unittest.main()
