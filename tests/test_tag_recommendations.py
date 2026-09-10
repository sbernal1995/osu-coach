import unittest
from engine import assess, recommend


class TagRecommendationIntegrationTests(unittest.TestCase):
    def test_focus_changes_candidate_order_without_bypassing_star_filters(self):
        def map_data(key, stars, tagged=False):
            return {"key": key, "id": 1, "set_id": 0, "title": key,
                    "artist": "Fixture", "stars": stars, "bpm": 150,
                    "ar": 7, "length": 120, "mode": 0,
                    "tags": [{"name": "skillset/streams", "source": "manual"}] if tagged else []}
        maps = [map_data("neutral", 3), map_data("practice", 3.02, True), map_data("too-hard", 5, True)]
        profile = assess([], initial=3)
        analysis = {"focus_tag": "skillset/streams", "items": [
            {"tag": "skillset/streams", "name": "Streams", "status": "practice",
             "confidence": "medium", "evidence_weight": .5, "stats_scope": "comparable",
             "comparable_plays": 8, "comparable_maps": 5, "comparable_sessions": 2}]}
        plain = recommend(maps, profile, limit=1)
        tagged = recommend(maps, profile, limit=1, tag_analysis=analysis)
        self.assertEqual("neutral", plain[1]["maps"][0]["key"])
        self.assertEqual("practice", tagged[1]["maps"][0]["key"])
        self.assertIn("streams", tagged[1]["maps"][0]["reason"])
        self.assertTrue(all(m["key"] != "too-hard" for group in tagged for m in group["maps"]))
