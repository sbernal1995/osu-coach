"""Synthetic score values using the current upstream tosu v2 field layout.

Source of field names/types (not a captured personal score):
https://github.com/tosuapp/tosu/blob/master/packages/tosu/src/api/utils/buildResultV2.ts
"""

import copy
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import patch

from telemetry import MAX_RESPONSE_BYTES, TosuTracker, read_snapshot


NOW = 1_788_890_400.0


def timestamp(offset=0):
    return datetime.fromtimestamp(NOW + offset, timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot(state=2, hits=60, failed=False, date=None):
    """A small faithful subset of buildResultV2's object structure."""
    mods = {"checksum": "NM", "number": 0, "name": "NM", "array": [], "rate": 1}
    counters = {"300": hits - 3 if hits else 0, "100": 2 if hits else 0,
                "50": 0, "0": 1 if hits else 0, "geki": 0, "katu": 0,
                "sliderEndHits": 20 if hits else 0, "smallTickHits": 0,
                "largeTickHits": 30 if hits else 0}
    return {
        "client": "lazer", "state": {"number": state, "name": "play" if state == 2 else "resultScreen"},
        "game": {"focused": True, "paused": False},
        "profile": {"name": "Jugador de prueba", "banchoStatus": {"number": 2, "name": "playing"},
                    "globalRank": 1, "pp": 999999},
        "settings": {"replayUIVisible": True},
        "beatmap": {
            "checksum": "a" * 32, "id": 12345, "set": 1234,
            "mode": {"number": 0, "name": "osu"},
            "artist": "Example Artist", "title": "Example Song", "version": "Normal",
            "time": {"live": 60000 if hits else 0, "firstObject": 1000, "lastObject": 101000},
            "stats": {
                "stars": {"total": 3.25, "live": 2.3, "aim": 1.4, "speed": 1.1},
                "ar": {"original": 7, "converted": 7}, "od": {"original": 6, "converted": 6},
                "cs": {"original": 4, "converted": 4}, "bpm": {"common": 150},
                "objects": {"circles": 60, "sliders": 40, "spinners": 0, "holds": 0, "total": 100},
                "maxCombo": 160,
            },
        },
        "play": {"playerName": "Jugador de prueba", "mode": {"number": 0, "name": "osu"},
                 "score": hits * 300, "accuracy": 96.3, "failed": failed,
                 "hits": {**counters, "sliderBreaks": 1}, "combo": {"current": 10, "max": hits},
                 "mods": copy.deepcopy(mods), "unstableRate": 120},
        "resultsScreen": {
            "scoreId": 45678, "playerName": "Jugador de prueba", "name": "Jugador de prueba",
            "mode": {"number": 0, "name": "osu"}, "score": hits * 300,
            "accuracy": 97.5, "hits": counters, "mods": copy.deepcopy(mods),
            "maxCombo": 125, "rank": "A", "pp": {"current": 20, "fc": 25},
            "createdAt": date if date is not None else timestamp(60),
        },
    }


class TrackerTests(unittest.TestCase):
    def test_historical_screen_at_start_never_imported(self):
        tracker = TosuTracker()
        result = snapshot(7, 100, date=timestamp(-86400))
        self.assertIsNone(tracker.feed(result, NOW))
        self.assertIsNone(tracker.feed(result, NOW + 1))

    def test_new_play_emits_once_with_result_stats(self):
        tracker = TosuTracker()
        self.assertIsNone(tracker.feed(snapshot(), NOW))
        result = snapshot(7, 100)
        event = tracker.feed(result, NOW + 60)
        self.assertEqual(event["accuracy"], 97.5)
        self.assertEqual(event["misses"], 1)
        self.assertEqual(event["max_combo"], 125)
        self.assertEqual(event["stars"], 3.25)
        self.assertEqual(event["completion"], 1)
        self.assertTrue(event["passed"])
        self.assertFalse(event["needs_confirmation"])
        self.assertNotIn("globalRank", event)
        self.assertIsNone(tracker.feed(result, NOW + 61))

    def test_two_identical_actual_plays_get_different_ids(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        first = tracker.feed(snapshot(7, 100), NOW + 60)
        tracker.feed(snapshot(), NOW + 120)
        second = tracker.feed(snapshot(7, 100, date=timestamp(180)), NOW + 180)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["accuracy"], second["accuracy"])

    def test_old_score_after_live_is_ignored_then_new_date_can_arrive(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        self.assertIsNone(tracker.feed(snapshot(7, 100, date=timestamp(-100)), NOW + 60))
        self.assertIsNotNone(tracker.feed(snapshot(7, 100), NOW + 61))

    def test_two_second_date_tolerance_not_unbounded(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        self.assertIsNone(tracker.feed(snapshot(7, 100, date=timestamp(-3)), NOW + 60))
        self.assertIsNotNone(tracker.feed(snapshot(7, 100, date=timestamp(-2)), NOW + 60))

    def test_missing_date_is_explicit_confirmation(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        event = tracker.feed(snapshot(7, 100, date=""), NOW + 60)
        self.assertTrue(event["needs_confirmation"])
        self.assertEqual(event["evidence"], "uncertain")
        self.assertTrue(event["uncertain_reason"])

    def test_failure_on_abandon_is_saved_for_confirmation(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(failed=True), NOW)
        event = tracker.feed({"state": {"number": 5}}, NOW + 1)
        self.assertFalse(event["passed"])
        self.assertAlmostEqual(event["completion"], 0.6)
        self.assertTrue(event["needs_confirmation"])
        self.assertIsNone(tracker.feed({"state": {"number": 5}}, NOW + 2))

    def test_abort_and_zero_object_failure_are_not_scores(self):
        for play in (snapshot(), snapshot(hits=0, failed=True)):
            with self.subTest(play=play["play"]["failed"]):
                tracker = TosuTracker()
                tracker.feed(play, NOW)
                self.assertIsNone(tracker.feed({"state": {"number": 5}}, NOW + 1))

    def test_no_fail_low_health_that_completes_is_completion(self):
        tracker = TosuTracker()
        live = snapshot(failed=True)
        result = snapshot(7, 100, failed=True)
        for item in (live, result):
            for field in ("play", "resultsScreen"):
                item[field]["mods"] = {"number": 1, "array": [{"acronym": "NF"}], "rate": 1}
        tracker.feed(live, NOW)
        self.assertTrue(tracker.feed(result, NOW + 60)["passed"])

    def test_failed_quick_retry_saved_and_retry_gets_new_identity(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(failed=True), NOW)
        failed = tracker.feed(snapshot(hits=0), NOW + 1)
        self.assertFalse(failed["passed"])
        tracker.feed(snapshot(), NOW + 2)
        passed = tracker.feed(snapshot(7, 100), NOW + 60)
        self.assertTrue(passed["passed"])
        self.assertNotEqual(failed["id"], passed["id"])

    def test_replay_flags_watching_player_and_auto_are_rejected(self):
        variants = []
        explicit = snapshot()
        explicit["game"]["isWatchingReplay"] = True
        variants.append(explicit)
        watching = snapshot()
        watching["profile"]["banchoStatus"] = {"number": 6, "name": "watching"}
        variants.append(watching)
        other_player = snapshot()
        other_player["play"]["playerName"] = "Other player"
        variants.append(other_player)
        auto = snapshot()
        auto["play"]["mods"] = {"number": 2048, "array": [], "rate": 1}
        variants.append(auto)
        for live in variants:
            with self.subTest(play=live):
                tracker = TosuTracker()
                tracker.feed(live, NOW)
                self.assertIsNone(tracker.feed(snapshot(7, 100), NOW + 60))

    def test_ui_visibility_is_not_a_replay_flag(self):
        tracker = TosuTracker()
        live = snapshot()
        live["settings"]["replayUIVisible"] = True
        tracker.feed(live, NOW)
        self.assertIsNotNone(tracker.feed(snapshot(7, 100), NOW + 60))

    def test_missing_or_invalid_essential_data_is_not_fabricated(self):
        for stars in (None, 0, float("nan"), float("inf")):
            with self.subTest(stars=stars):
                tracker = TosuTracker()
                live = snapshot()
                live["beatmap"]["stats"]["stars"]["total"] = stars
                tracker.feed(live, NOW)
                self.assertIsNone(tracker.feed(snapshot(7, 100), NOW + 60))

    def test_partial_result_counters_wait_for_final_data(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        self.assertIsNone(tracker.feed(snapshot(7, 20), NOW + 60))
        self.assertIsNone(tracker.feed(snapshot(7, 80), NOW + 60))
        self.assertTrue(tracker.feed(snapshot(7, 100), NOW + 61)["passed"])

    def test_failure_between_polls_emits_after_partial_result_settles(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(failed=False), NOW)
        result = snapshot(7, 80, failed=False)
        self.assertIsNone(tracker.feed(result, NOW + 60))
        self.assertIsNone(tracker.feed(result, NOW + 60.5))
        event = tracker.feed(result, NOW + 60.8)
        self.assertIsNotNone(event)
        self.assertFalse(event["passed"])
        self.assertEqual(event["completion"], 0.8)
        self.assertFalse(event["needs_confirmation"])
        self.assertTrue(event["failure_inferred_from_result"])
        self.assertIsNone(tracker.feed(result, NOW + 62))

    def test_changing_partial_counters_restart_settling_interval(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        self.assertIsNone(tracker.feed(snapshot(7, 70), NOW + 60))
        self.assertIsNone(tracker.feed(snapshot(7, 80), NOW + 60.8))
        self.assertIsNone(tracker.feed(snapshot(7, 80), NOW + 61.2))
        event = tracker.feed(snapshot(7, 100), NOW + 61.3)
        self.assertTrue(event["passed"])
        self.assertFalse(event["failure_inferred_from_result"])

    def test_stale_or_undated_partial_result_never_infers_failure(self):
        for date in (timestamp(-100), ""):
            with self.subTest(date=date):
                tracker = TosuTracker()
                tracker.feed(snapshot(), NOW)
                result = snapshot(7, 80, date=date)
                self.assertIsNone(tracker.feed(result, NOW + 60))
                self.assertIsNone(tracker.feed(result, NOW + 62))

    def test_lazer_rate_and_settings_are_part_of_key(self):
        keys = []
        for rate in (1.2, 1.5):
            tracker = TosuTracker()
            live = snapshot()
            result = snapshot(7, 100)
            for item in (live, result):
                for field in ("play", "resultsScreen"):
                    item[field]["mods"] = {"number": 64, "rate": rate,
                        "array": [{"acronym": "DT", "settings": {"speed_change": rate}}]}
            tracker.feed(live, NOW)
            keys.append(tracker.feed(result, NOW + 60)["mod_key"])
        self.assertNotEqual(keys[0], keys[1])

    def test_input_snapshot_is_copied(self):
        tracker = TosuTracker()
        live = snapshot()
        tracker.feed(live, NOW)
        live["beatmap"]["stats"]["stars"]["total"] = 9
        self.assertEqual(tracker.feed(snapshot(7, 100), NOW + 60)["stars"], 3.25)


class ReadSnapshotTests(unittest.TestCase):
    def test_external_urls_rejected_without_network(self):
        for url in ("http://example.org/json/v2", "http://127.0.0.1.evil.test/", "file:///tmp/x", "http://user@127.0.0.1/"):
            with self.subTest(url=url), patch("telemetry.build_opener") as opener:
                with self.assertRaises(ValueError):
                    read_snapshot(url)
                opener.assert_not_called()

    def test_reads_json_and_rejects_non_object(self):
        with patch("telemetry.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"state":{"number":2}}')
            self.assertEqual(read_snapshot()["state"]["number"], 2)
            opener.return_value.open.return_value = io.BytesIO(b'[]')
            with self.assertRaises(ValueError):
                read_snapshot()

    def test_oversized_response_is_rejected(self):
        with patch("telemetry.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b" " * (MAX_RESPONSE_BYTES + 1))
            with self.assertRaises(ValueError):
                read_snapshot()


if __name__ == "__main__":
    unittest.main()
