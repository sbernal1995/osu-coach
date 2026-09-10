"""Tag evidence must reflect current, comparable maps rather than repetitions."""
from datetime import datetime, timedelta, timezone
import unittest

from osu_coach.core.tag_analysis import analyze_tags, skill_tags, tag_priority, TAG_NAMES


NOW = datetime(2026, 9, 8, 19, tzinfo=timezone.utc)


def beatmap(key, *names, stars=4.0, **changes):
    result = {"key": key, "id": int(key) if str(key).isdigit() else 0,
              "stars": stars, "mode": 0,
              "tags": [{"name": name, "source": "community", "id": index + 1}
                       for index, name in enumerate(names)]}
    result.update(changes)
    return result


def play(index, key, accuracy=95, **changes):
    result = {"id": str(index), "beatmap_key": key, "stars": 4.0,
              "played_at": (NOW + timedelta(minutes=index, hours=2 if index >= 4 else 0)).isoformat(),
              "accuracy": accuracy, "misses": 0, "judged_objects": 400,
              "object_count": 400, "max_combo": 490, "map_max_combo": 500,
              "passed": True, "completion": 1, "mode": 0}
    result.update(changes)
    return result


def entry(analysis, name):
    return next(item for item in analysis["items"] if item["tag"] == name)


class TagEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [beatmap("a", "skillset/streams"), beatmap("b", "skillset/streams"),
                        beatmap("c", "skillset/jumps"), beatmap("d", "skillset/jumps")]
        self.catalog += [beatmap(key, "skillset/streams") for key in ("e", "f", "g")]
        self.catalog += [beatmap(key, "skillset/jumps") for key in ("h", "i", "j")]

    def test_eight_results_on_five_maps_in_two_sessions_create_a_practice_focus(self):
        analysis = analyze_tags([play(i, key) for i, key in enumerate(("a", "a", "b", "b", "e", "e", "f", "g"))], self.catalog, 4)
        item = entry(analysis, "skillset/streams")
        self.assertEqual((item["plays"], item["distinct_maps"]), (8, 5))
        self.assertEqual((item["status"], item["confidence"]), ("practice", "medium"))
        self.assertEqual(analysis["focus_tag"], "skillset/streams")
        self.assertEqual(analysis["tagged_plays"], 8)
        self.assertEqual(item["comparable_sessions"], 2)

    def test_two_results_and_repeating_only_one_map_are_insufficient(self):
        for results in ([play(1, "a"), play(2, "b")], [play(i, "a") for i in range(12)]):
            with self.subTest(results=len(results)):
                analysis = analyze_tags(results, self.catalog, 4)
                item = entry(analysis, "skillset/streams")
                self.assertEqual((item["status"], item["confidence"]), ("learning", "low"))
                self.assertIsNone(analysis["focus_tag"])
                self.assertLessEqual(item["plays"], 2)

    def test_each_evidence_threshold_is_required(self):
        keys = ("a", "a", "b", "b", "e", "e", "f", "g")
        cases = [[play(i, key) for i, key in enumerate(keys[:-1])],
                 [play(i, key) for i, key in enumerate(("a", "a", "b", "b", "e", "e", "f", "f"))],
                 [play(i, key, played_at=(NOW + timedelta(minutes=i)).isoformat()) for i, key in enumerate(keys)]]
        for rows in cases:
            result = analyze_tags(rows, self.catalog, 4)
            self.assertEqual("learning", entry(result, "skillset/streams")["status"])
            self.assertIsNone(result["focus_tag"])

    def test_missing_miss_measurements_cannot_create_strength_or_weakness(self):
        rows = [play(i, key, 80, misses=None) for i, key in enumerate(("a", "a", "b", "b", "e", "e", "f", "g"))]
        result = analyze_tags(rows, self.catalog, 4)
        item = entry(result, "skillset/streams")
        self.assertEqual("learning", item["status"])
        self.assertEqual(0, item["comparable_plays"])
        self.assertIsNone(item["miss_rate"])

    def test_sessions_are_assigned_before_filtering_for_a_tag(self):
        keys = ("a", "a", "b", "b", "e", "e", "f", "g")
        rows = [play(i, key, played_at=(NOW + timedelta(minutes=minute)).isoformat())
                for i, (key, minute) in enumerate(zip(keys, (0, 10, 20, 30, 100, 110, 120, 130)))]
        self.assertEqual(2, entry(analyze_tags(rows, self.catalog, 4), "skillset/streams")["comparable_sessions"])
        rows.append(play(9, "untagged", played_at=(NOW + timedelta(minutes=60)).isoformat()))
        result = analyze_tags(rows, self.catalog, 4)
        item = entry(result, "skillset/streams")
        self.assertEqual(1, item["comparable_sessions"])
        self.assertEqual("learning", item["status"])

    def test_recent_maps_have_more_influence_than_old_maps(self):
        maps = [beatmap(str(i), "skillset/streams") for i in range(10)]
        rows = [play(i, str(i), 80, played_at=(NOW - timedelta(days=20)).isoformat()) for i in range(5)]
        rows += [play(i, str(i), 100, played_at=NOW.isoformat()) for i in range(5, 10)]
        item = entry(analyze_tags(rows, maps, 4, now=NOW), "skillset/streams")
        self.assertEqual(96, item["accuracy"])
        self.assertEqual(2, item["comparable_sessions"])
        self.assertEqual("medium", item["confidence"])

    def test_only_two_latest_attempts_per_map_contribute(self):
        results = [play(i, "a", accuracy=10) for i in range(6)]
        results.extend([play(6, "a", 99), play(7, "a", 99), play(8, "b", 99)])
        analysis = analyze_tags(list(reversed(results)), self.catalog, 4)
        item = entry(analysis, "skillset/streams")
        self.assertEqual(item["accuracy"], 99)
        self.assertEqual(item["status"], "learning")
        self.assertEqual((item["plays"], analysis["tagged_plays"]), (3, 9))

    def test_equal_weight_per_map_prevents_repeat_bias(self):
        result = analyze_tags([play(1, "a", 100), play(2, "a", 100), play(3, "b", 90)], self.catalog, 4)
        self.assertEqual(entry(result, "skillset/streams")["accuracy"], 95)

    def test_harder_maps_cannot_become_a_weakness_at_current_level(self):
        results = [play(i, key, 80, stars=6) for i, key in enumerate(["a", "a", "b"])]
        results += [play(i + 3, key, 99) for i, key in enumerate(["c", "c", "d", "d", "h", "h", "i", "j"])]
        analysis = analyze_tags(results, self.catalog, 4)
        difficult = entry(analysis, "skillset/streams")
        self.assertEqual((difficult["status"], difficult["comparable_plays"]), ("learning", 0))
        self.assertEqual(difficult["outside_band_plays"], 3)
        self.assertEqual(difficult["stats_scope"], "outside_band")
        self.assertEqual(entry(analysis, "skillset/jumps")["status"], "strength")
        self.assertIsNone(analysis["focus_tag"])

    def test_easy_maps_cannot_create_a_strength_at_current_level(self):
        results = [play(i, key, 100, stars=2) for i, key in enumerate(["a", "a", "b"])]
        analysis = analyze_tags(results, self.catalog, 4)
        self.assertEqual(entry(analysis, "skillset/streams")["status"], "learning")

    def test_out_of_band_results_do_not_contaminate_comparable_metrics(self):
        results = [play(0, "a", 50, stars=6), play(1, "a", 99), play(2, "b", 99), play(3, "b", 99)]
        item = entry(analyze_tags(results, self.catalog, 4), "skillset/streams")
        self.assertEqual(item["accuracy"], 99)
        self.assertEqual(item["status"], "learning")
        self.assertEqual((item["comparable_plays"], item["outside_band_plays"]), (3, 1))

    def test_failures_partial_results_and_misses_are_considered(self):
        for changes in ({"passed": False}, {"completion": .4}, {"misses": 2, "judged_objects": 100}):
            with self.subTest(changes=changes):
                results = [play(i, key, 99, **changes) for i, key in enumerate(["a", "a", "b", "b", "e", "e", "f", "g"])]
                item = entry(analyze_tags(results, self.catalog, 4), "skillset/streams")
                self.assertEqual(item["status"], "practice")
        results = [play(i, key, 99, max_combo=300) for i, key in enumerate(["a", "a", "b"])]
        self.assertEqual(entry(analyze_tags(results, self.catalog, 4), "skillset/streams")["status"], "learning")

    def test_cooccurring_tags_are_both_counted_and_disclosed(self):
        maps = [beatmap(key, "skillset/streams", "reading/overlaps") for key in ("a", "b")]
        result = analyze_tags([play(1, "a"), play(2, "a"), play(3, "b")], maps, 4)
        self.assertEqual(result["tagged_plays"], 3)
        self.assertTrue(all(item["plays"] == 3 and item["cooccurs"] for item in result["items"]))
        self.assertIn("todas las etiquetas", result["cooccurrence_message"])

    def test_rank_and_pp_have_no_effect(self):
        results = [play(i, key) for i, key in enumerate(["a", "a", "b"])]
        altered = [dict(p, pp=12345, rank=1, historical_best=100) for p in results]
        self.assertEqual(analyze_tags(results, self.catalog, 4), analyze_tags(altered, self.catalog, 4))

    def test_new_window_reset_removes_all_evidence(self):
        for rows in ([], [play(1, "a", played_at="invalid")]):
            analysis = analyze_tags(rows, self.catalog, 4)
            self.assertEqual(analysis["tagged_plays"], 0)
            self.assertIsNone(analysis["focus_tag"])
            self.assertTrue(all(item["status"] == "explore" and item["accuracy"] is None for item in analysis["items"]))

    def test_id_lookup_joins_different_key_and_deduplicates_retries(self):
        maps = [beatmap("123", "skillset/reading"), beatmap("456", "skillset/reading")]
        results = [play(0, "unknown1", beatmap_id=123), play(1, "unknown2", beatmap_id=123),
                   play(2, "123", beatmap_id=123), play(3, "456", beatmap_id=456)]
        results.append(dict(results[-1]))
        item = entry(analyze_tags(results, maps, 4), "skillset/reading")
        self.assertEqual((item["plays"], item["distinct_maps"]), (3, 2))

    def test_output_is_bounded_and_played_tags_remain_visible(self):
        maps = [beatmap(str(i), name) for i, name in enumerate(TAG_NAMES)]
        maps.extend([beatmap("a", "tech/finger control"), beatmap("b", "tech/finger control")])
        result = analyze_tags([play(1, "a"), play(2, "a"), play(3, "b")], maps, 4)
        self.assertEqual(len(result["items"]), 25)
        self.assertEqual(result["items"][0]["tag"], "tech/finger control")


class TagNormalizationTests(unittest.TestCase):
    def test_legacy_music_unknown_and_other_ruleset_tags_are_ignored(self):
        maps = [beatmap("a", "anime", "rock", "skillset/speedjack", "skillset/wristjack", "tech/hyperwalks",
                        "skillset/speed", "meta/variable timing", "artist/streams"),
                beatmap("b", tags="anime rock flow stream reading"), beatmap("c", tags=None), {"key": "d"}]
        self.assertTrue(all(skill_tags(m) == [] for m in maps))
        result = analyze_tags([play(1, "a")], maps, 4)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["tagged_plays"], 0)

    def test_explicit_canonical_mapper_and_manual_tags_keep_provenance(self):
        value = beatmap("a", tags=[{"name": " SKILLSET/READING ", "source": "mapper"},
                                   {"name": "skillset/reading", "source": "manual"},
                                   {"name": "tech/finger   control", "source": "community"}])
        tags = skill_tags(value)
        self.assertEqual(len(tags), 2)
        self.assertEqual(tags[0]["source"], "manual")
        self.assertEqual(tags[1]["name"], "tech/finger control")
        self.assertEqual(skill_tags(dict(value, mode=3)), [])

    def test_community_votes_below_public_threshold_are_ignored(self):
        value = beatmap("a", tags=[{"name": "skillset/reading", "source": "community", "count": 4},
                                   {"name": "skillset/jumps", "source": "community", "count": 5}])
        self.assertEqual([tag["name"] for tag in skill_tags(value)], ["skillset/jumps"])


class TagPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.maps = [beatmap(key, tag) for key, tag in (("a", "skillset/streams"), ("b", "skillset/streams"),
                                                       ("c", "skillset/jumps"), ("d", "skillset/jumps"))]
        self.maps += [beatmap(key, "skillset/streams") for key in ("e", "f", "g")]
        self.maps += [beatmap(key, "skillset/jumps") for key in ("h", "i", "j")]
        self.plays = [play(i, key) for i, key in enumerate(["a", "a", "b", "b", "e", "e", "f", "g"])]
        self.plays += [play(i + 8, key, 99, played_at=(NOW + timedelta(minutes=i + 4, hours=2 if i >= 4 else 0)).isoformat())
                       for i, key in enumerate(["c", "c", "d", "d", "h", "h", "i", "j"])]
        self.analysis = analyze_tags(self.plays, self.maps, 4)

    def test_practice_prefers_focus_and_warmup_prefers_strength(self):
        for candidate, stage, label in ((self.maps[0], "practice", "practicar"), (self.maps[2], "warmup", "yendo bien")):
            with self.subTest(stage=stage):
                penalty, reason = tag_priority(candidate, self.analysis, stage)
                self.assertGreaterEqual(penalty, -.4)
                self.assertLess(penalty, 0)
                self.assertIn(label, reason)
        self.assertEqual(tag_priority(self.maps[0], self.analysis, "warmup"), (0, None))
        self.assertEqual(tag_priority(self.maps[2], self.analysis, "practice"), (0, None))

    def test_missing_tags_low_evidence_and_unknown_stages_are_neutral(self):
        self.assertEqual(tag_priority(beatmap("none"), self.analysis, "practice"), (0, None))
        low = analyze_tags(self.plays[:2], self.maps, 4)
        self.assertEqual(tag_priority(self.maps[0], low, "practice"), (0, None))
        self.assertEqual(tag_priority(self.maps[0], self.analysis, "challenge"), (0, None))

    def test_boost_is_bounded_for_many_cooccurring_tags(self):
        mixed = beatmap("mixed", *TAG_NAMES)
        for stage in ("warmup", "practice", "challenge", "consolidate"):
            penalty, _ = tag_priority(mixed, self.analysis, stage)
            self.assertLessEqual(penalty, .4)
            self.assertGreaterEqual(penalty, -.4)


if __name__ == "__main__":
    unittest.main()
