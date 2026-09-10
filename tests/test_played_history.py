"""Exact played difficulties without app, storage, network or clocks."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from osu_coach.core.played_history import PlayedHistory


def beatmap(index=1, **changes):
    row = {"id": index, "key": f"map-{index}", "set_id": 100,
           "artist": "Artist", "title": "Song", "version": "Insane"}
    row.update(changes)
    return row


def play(index=1, **changes):
    row = {"id": f"attempt-{index}", "beatmap_id": index, "beatmap_key": f"map-{index}",
           "artist": "Artist", "title": "Song", "version": "Insane",
           "played_at": "2020-01-01T00:00:00+00:00", "passed": True}
    row.update(changes)
    return row


class PlayedHistoryTests(unittest.TestCase):
    def test_old_accepted_results_count_without_time_window_or_quality_threshold(self):
        history = PlayedHistory([play(accuracy=10, misses=99, passed=False, completion=.01)])
        self.assertTrue(history.contains(beatmap()))

    def test_different_difficulty_of_played_song_is_allowed(self):
        history = PlayedHistory([play()])
        self.assertFalse(history.contains(beatmap(2, version="Expert", set_id=900)))
        self.assertFalse(history.contains(beatmap(2)))

    def test_same_title_with_a_different_artist_is_not_the_same_song(self):
        history = PlayedHistory([play()])
        self.assertFalse(history.contains(beatmap(2, artist="Other artist")))

    def test_title_changes_do_not_override_difficulty_identity(self):
        history = PlayedHistory([play()])
        for title in ("Song (Live)", "Song [Remix]", "Song!", "Song Part II"):
            with self.subTest(title=title):
                self.assertFalse(history.contains(beatmap(2, title=title)))
                self.assertTrue(history.contains(beatmap(1, title=title)))

    def test_same_set_alone_does_not_mark_another_song_played(self):
        history = PlayedHistory([play()], [beatmap(), beatmap(2, title="Medley part 2")])
        self.assertFalse(history.contains(beatmap(2, title="Medley part 2")))

    def test_same_id_matches_a_different_hash_and_renamed_metadata(self):
        history = PlayedHistory([play()])
        self.assertTrue(history.contains(beatmap(key="different-hash", artist="Renamed", title="Renamed")))

    def test_same_hash_matches_without_id_or_song_metadata(self):
        history = PlayedHistory([{"beatmap_key": "ABCD"}])
        self.assertTrue(history.contains({"key": "abcd"}))

    def test_remote_and_osu_keys_join_positive_online_ids(self):
        for source in ({"beatmap_id": 42}, {"beatmap_key": "remote:42"}, {"beatmap_key": "osu:42"}):
            history = PlayedHistory([source])
            for target in ({"id": 42}, {"key": "osu:42"}, {"key": "remote:42"}, {"beatmap_id": "42"}):
                with self.subTest(source=source, target=target):
                    self.assertTrue(history.contains(target))

    def test_numeric_attempt_id_is_never_a_beatmap_id(self):
        history = PlayedHistory([{"id": "42"}, {"id": 84}])
        self.assertFalse(history.contains({"id": 42}))
        self.assertFalse(history.contains({"id": 84}))

    def test_invalid_or_zero_ids_cannot_bridge_unrelated_maps(self):
        for value in (None, 0, -1, True, False, 1.5, "1.5", "invalid", float("nan"), float("inf")):
            with self.subTest(value=value):
                history = PlayedHistory([{"beatmap_id": value}])
                self.assertFalse(history.contains({"id": value}))

    def test_text_metadata_alone_never_proves_difficulty_identity(self):
        metadata = {"artist": "Artist", "title": "Song", "version": "Insane",
                    "artist_romanized": "Artist", "title_romanized": "Song",
                    "artistUnicode": "歌手", "titleUnicode": "曲"}
        history = PlayedHistory([metadata], [beatmap(1, **metadata)])
        self.assertFalse(history.contains(metadata))
        self.assertFalse(history.contains(beatmap(1, **metadata)))

    def test_title_without_artist_does_not_match_an_unrelated_identity(self):
        history = PlayedHistory([{"title": "Song", "beatmap_id": 1}])
        self.assertFalse(history.contains({"id": 2, "title": "Song"}))
        self.assertFalse(history.contains(beatmap(2)))

    def test_artist_without_title_does_not_match_an_unrelated_identity(self):
        history = PlayedHistory([{"artist": "Artist", "beatmap_id": 1}])
        self.assertFalse(history.contains({"id": 2, "artist": "Artist"}))

    def test_empty_metadata_and_malformed_rows_remain_unknown(self):
        history = PlayedHistory([None, {}, "ignored"], [None], [None])
        for candidate in (None, {}, "ignored", {"artist": " ", "title": "Song"}):
            with self.subTest(candidate=candidate):
                self.assertFalse(history.contains(candidate))

    def test_id_only_play_matches_catalog_key_but_not_other_difficulties(self):
        catalog = [beatmap(1, artist="歌手", title="曲", artist_romanized="Artist", title_romanized="Song")]
        history = PlayedHistory([{"beatmap_id": 1}], catalog)
        self.assertTrue(history.contains({"key": "map-1"}))
        for fields in ({"artist": "歌手", "title": "曲"},
                       {"artist": "Artist", "title": "Song"},
                       {"artist": "歌手", "title": "Song"}):
            with self.subTest(fields=fields):
                self.assertFalse(history.contains(beatmap(2, **fields)))

    def test_unicode_field_variants_do_not_join_different_difficulties(self):
        for title_field, artist_field in (("title_unicode", "artist_unicode"),
                                           ("titleUnicode", "artistUnicode")):
            with self.subTest(title_field=title_field):
                catalog = [beatmap(1, **{title_field: "曲", artist_field: "歌手"})]
                history = PlayedHistory([play()], catalog)
                self.assertFalse(history.contains(beatmap(2, artist="歌手", title="曲")))

    def test_catalog_identity_bridges_are_transitive_and_order_independent(self):
        catalog = [beatmap(1, key="old-key"),
                   beatmap(1, key="new-key", artist="歌手", title="曲"),
                   beatmap(2, key="second-difficulty", artist="歌手", title="曲")]
        for ordered in (catalog, list(reversed(catalog))):
            history = PlayedHistory([{"beatmap_key": "old-key"}], ordered)
            self.assertTrue(history.contains({"id": 1}))
            self.assertTrue(history.contains({"key": "new-key"}))
            self.assertTrue(history.contains({"key": "remote:1"}))
            self.assertFalse(history.contains({"id": 2}))
            self.assertFalse(history.contains({"key": "second-difficulty"}))

    def test_catalog_membership_alone_does_not_mean_played(self):
        history = PlayedHistory([], [beatmap(1), beatmap(2)])
        self.assertFalse(history.contains(beatmap(1)))
        self.assertFalse(history.contains(beatmap(2)))

    def test_pending_rejected_and_excluded_plays_do_not_mark_difficulties(self):
        for changes in ({"needs_confirmation": True}, {"excluded": True},
                        {"status": "pending"}, {"status": "rejected"}, {"status": "excluded"}):
            with self.subTest(changes=changes):
                history = PlayedHistory([play(**changes)], [beatmap()])
                self.assertFalse(history.contains(beatmap()))

    def test_completed_map_and_quest_history_only_mark_that_difficulty(self):
        for completed in ([beatmap()], [{"status": "completed", "map": beatmap()}]):
            with self.subTest(completed=completed):
                history = PlayedHistory([], completed=completed)
                self.assertTrue(history.contains(beatmap(1)))
                self.assertFalse(history.contains(beatmap(2)))

    def test_pending_quest_is_not_a_completion(self):
        history = PlayedHistory([], completed=[{"status": "pending", "map": beatmap()}])
        self.assertFalse(history.contains(beatmap()))

    def test_completed_map_can_bridge_online_id_to_catalog_key(self):
        catalog = [beatmap(1, artist="歌手", title="曲", artist_romanized="Artist", title_romanized="Song")]
        history = PlayedHistory([], catalog, completed=[{"map": {"key": "remote:1"}, "status": "completed"}])
        self.assertTrue(history.contains({"key": "map-1"}))
        self.assertFalse(history.contains(beatmap(2, artist="歌手", title="曲")))

    def test_monochrome_butterfly_older_plays_exclude_exact_goal_but_allow_other_difficulty(self):
        song = {"id": 1621175, "key": "4f5a2606a6dd1753235b4be248586a63",
                "artist": "Fixture artist", "title": "Monochrome Butterfly", "version": "Insane"}
        older = [{"id": f"older-{index}", "beatmap_id": song["id"], "beatmap_key": song["key"],
                  "accuracy": accuracy, "played_at": "2026-09-08T00:00:00+00:00"}
                 for index, accuracy in enumerate((98.3, 98.8))]
        history = PlayedHistory(older, [song])
        self.assertTrue(history.contains(song))
        self.assertFalse(history.contains({**song, "id": 9999, "key": "another-difficulty", "version": "Expert"}))
        self.assertFalse(history.contains({**song, "id": 9999, "key": "another-difficulty"}))

    def test_duplicate_attempts_and_repeated_queries_do_not_mutate_inputs_or_index(self):
        plays, catalog = [play(), play()], [beatmap()]
        before = deepcopy((plays, catalog))
        history = PlayedHistory(plays, catalog)
        index = deepcopy(history.__dict__)
        for _ in range(3):
            self.assertTrue(history.contains(beatmap(1)))
            self.assertFalse(history.contains(beatmap(2)))
            self.assertFalse(history.contains(beatmap(3, title="Other song")))
        self.assertEqual(before, (plays, catalog))
        self.assertEqual(index, history.__dict__)

    def test_before_includes_exact_boundary_and_excludes_later_attempt(self):
        history = PlayedHistory([play(played_at="2026-09-09T04:11:00Z")])
        self.assertTrue(history.contains(beatmap(), before="2026-09-09T04:11:00+00:00"))
        self.assertFalse(history.contains(beatmap(), before="2026-09-09T04:10:59.999999Z"))
        self.assertTrue(history.contains(beatmap(), before=datetime(2026, 9, 9, 5, tzinfo=timezone.utc)))

    def test_before_does_not_borrow_earlier_date_from_another_difficulty(self):
        catalog = [beatmap(1, artist="歌手", title="曲", artist_romanized="Artist", title_romanized="Song"),
                   beatmap(2, artist="歌手", title="曲")]
        rows = [play(2, artist="歌手", title="曲", played_at="2026-09-09T05:00:00Z"),
                play(1, played_at="2026-09-08T01:00:00Z")]
        for ordered in (rows, list(reversed(rows))):
            history = PlayedHistory(ordered, catalog)
            self.assertTrue(history.contains({"id": 1}, before="2026-09-09T04:11:00Z"))
            self.assertFalse(history.contains({"id": 2}, before="2026-09-09T04:11:00Z"))

    def test_before_uses_earliest_play_across_revisions_of_same_id(self):
        catalog = [beatmap(1, key="old-key"), beatmap(1, key="new-key")]
        rows = [{"beatmap_key": "old-key", "played_at": "2026-09-08T01:00:00Z"},
                {"beatmap_key": "new-key", "played_at": "2026-09-09T05:00:00Z"}]
        for ordered in (rows, list(reversed(rows))):
            history = PlayedHistory(ordered, catalog)
            self.assertTrue(history.contains({"key": "new-key"}, before="2026-09-09T04:11:00Z"))

    def test_before_compares_instants_across_timezones(self):
        history = PlayedHistory([play(played_at="2026-09-09T01:11:00-03:00")])
        self.assertTrue(history.contains(beatmap(), before="2026-09-09T04:11:00Z"))
        self.assertFalse(history.contains(beatmap(), before="2026-09-09T04:10:00Z"))

    def test_undated_or_invalid_dates_only_match_unrestricted_history(self):
        for date in (None, "bad date", "2026-09-09T04:00:00", 1, True):
            with self.subTest(date=date):
                history = PlayedHistory([play(played_at=date)])
                self.assertTrue(history.contains(beatmap()))
                self.assertFalse(history.contains(beatmap(), before="2026-09-09T04:11:00Z"))

    def test_invalid_cutoff_never_matches(self):
        history = PlayedHistory([play()])
        for cutoff in ("bad date", "2026-09-09T04:00:00", datetime(2026, 9, 9), 1, True):
            with self.subTest(cutoff=cutoff):
                self.assertFalse(history.contains(beatmap(), before=cutoff))

    def test_completed_history_uses_completion_time_not_assignment_time(self):
        completed = [{"map": beatmap(), "status": "completed", "created_at": "2026-09-01T00:00:00Z",
                      "completed_at": "2026-09-09T05:00:00Z"}]
        history = PlayedHistory([], completed=completed)
        self.assertFalse(history.contains(beatmap(), before="2026-09-09T04:11:00Z"))
        self.assertTrue(history.contains(beatmap(), before="2026-09-09T05:00:00Z"))

    def test_completed_history_can_use_last_attempt_when_completion_time_missing(self):
        completed = [{"map": beatmap(), "status": "completed",
                      "last_attempt": {"played_at": "2026-09-08T01:00:00Z"}}]
        history = PlayedHistory([], completed=completed)
        self.assertTrue(history.contains(beatmap(1), before="2026-09-09T04:11:00Z"))
        self.assertFalse(history.contains(beatmap(2), before="2026-09-09T04:11:00Z"))

    def test_undated_completed_maps_do_not_establish_prior_play(self):
        for completed in ([beatmap()], [{"map": beatmap(), "status": "completed",
                                         "created_at": "2020-01-01T00:00:00Z"}]):
            with self.subTest(completed=completed):
                history = PlayedHistory([], completed=completed)
                self.assertTrue(history.contains(beatmap()))
                self.assertFalse(history.contains(beatmap(), before="2026-09-09T04:11:00Z"))

    def test_undated_entry_does_not_hide_a_later_dated_observation(self):
        history = PlayedHistory([play(played_at=None), play(played_at="2026-09-08T01:00:00Z")])
        self.assertTrue(history.contains(beatmap(), before="2026-09-09T04:11:00Z"))


if __name__ == "__main__":
    unittest.main()
