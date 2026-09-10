"""Additional observed telemetry needed by map missions, using synthetic data."""

import unittest

from telemetry import TosuTracker
from test_telemetry import NOW, snapshot, timestamp


class QuestTelemetryTests(unittest.TestCase):
    def test_final_basic_judgements_grade_and_observed_start_are_saved(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        event = tracker.feed(snapshot(7, 100), NOW + 60)
        self.assertEqual((event["n300"], event["n100"], event["n50"]), (97, 2, 0))
        self.assertEqual(event["grade"], "A")
        self.assertEqual(event["started_at"], timestamp())

    def test_missing_or_numeric_grade_does_not_use_global_rank_or_previous_live_grade(self):
        for rank in (None, 1, "1", "bad", {}):
            with self.subTest(rank=rank):
                tracker = TosuTracker()
                live, result = snapshot(), snapshot(7, 100)
                live["play"]["rank"] = {"current": "S", "maxThisPlay": "SS"}
                result["resultsScreen"]["rank"] = rank
                tracker.feed(live, NOW)
                event = tracker.feed(result, NOW + 60)
                self.assertNotIn("grade", event)
                self.assertNotIn("globalRank", event)

    def test_silver_grade_is_preserved_with_normalized_alias(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        result = snapshot(7, 100)
        result["resultsScreen"]["rank"] = "XH"
        self.assertEqual(tracker.feed(result, NOW + 60)["grade"], "SSH")

    def test_observed_failure_uses_live_current_grade_not_hypothetical_maximum(self):
        tracker = TosuTracker()
        live = snapshot(failed=True)
        live["play"]["rank"] = {"current": "B", "maxThisPlay": "S"}
        tracker.feed(live, NOW)
        event = tracker.feed({"state": {"number": 5}}, NOW + 1)
        self.assertFalse(event["passed"])
        self.assertEqual(event["grade"], "B")
        self.assertEqual(event["n300"], 57)

    def test_quick_retry_has_its_own_observed_start(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(failed=True), NOW)
        failure = tracker.feed(snapshot(hits=0), NOW + 10)
        tracker.feed(snapshot(), NOW + 11)
        result = tracker.feed(snapshot(7, 100, date=timestamp(70)), NOW + 70)
        self.assertEqual(failure["started_at"], timestamp())
        self.assertEqual(result["started_at"], timestamp(10))

    def test_backwards_result_date_does_not_fabricate_a_start_before_observation(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(), NOW)
        result = tracker.feed(snapshot(7, 100, date=timestamp(-1)), NOW + 60)
        self.assertIsNotNone(result)
        self.assertNotIn("started_at", result)

    def test_start_records_first_observation_not_last_poll(self):
        tracker = TosuTracker()
        tracker.feed(snapshot(hits=0), NOW)
        tracker.feed(snapshot(), NOW + 30)
        event = tracker.feed(snapshot(7, 100), NOW + 60)
        self.assertEqual(event["started_at"], timestamp())


if __name__ == "__main__":
    unittest.main()
