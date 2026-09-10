"""Queries must not interpret song punctuation as osu! search operators."""
from copy import deepcopy
import unittest

from osu_coach.beatmaps.map_search import search_details


class MapSearchTests(unittest.TestCase):
    def setUp(self):
        self.beatmap = {"id": 1762724, "set_id": 842412,
                        "title": "春待ちクローバー (Swing Arrangement) [Dictate Edit]",
                        "title_romanized": "Harumachi Clover (Swing Arrangement) [Dictate Edit]",
                        "artist_romanized": "Will Stetson", "creator": "Sotarks", "version": "Expert"}

    def test_exact_difficulty_id_avoids_bracket_filter_and_script_dependencies(self):
        before = deepcopy(self.beatmap)
        result = search_details(self.beatmap)
        self.assertEqual(result["search_text"], "1762724")
        self.assertEqual(result["search_method"], "id")
        self.assertEqual(result["search_title_text"], "Harumachi Clover Swing Arrangement Dictate Edit Expert")
        self.assertEqual(result["search_mapper_text"], "Sotarks")
        self.assertEqual(self.beatmap, before)

    def test_legacy_quest_does_not_reuse_its_frozen_broken_search(self):
        self.beatmap.pop("title_romanized")
        self.beatmap["search_text"] = self.beatmap["title"] + " Expert"
        result = search_details(self.beatmap)
        self.assertEqual(result["search_text"], "1762724")
        self.assertEqual(result["search_title_text"], "春待ちクローバー Swing Arrangement Dictate Edit Expert")

    def test_unsubmitted_map_uses_romanized_title_and_difficulty_without_set_id(self):
        self.beatmap["id"] = 0
        result = search_details(self.beatmap)
        self.assertEqual(result["search_method"], "title")
        self.assertEqual(result["search_text"], result["search_title_text"])
        self.assertNotIn("842412", result["search_text"])

    def test_metadata_operators_become_separate_words(self):
        result = search_details({"title": 'ar=9 title:foo "歌" [Hard]',
                                 "version": "A-B (Edit)", "creator": 'Foo "Bar" [Team]'})
        self.assertEqual(result["search_text"], "ar 9 title foo 歌 Hard A B Edit")
        self.assertEqual(result["search_mapper_text"], "Foo Bar Team")

    def test_invalid_ids_use_text_query(self):
        for identifier in (None, 0, -1, True, False, 1.5, "-10", "1.5", "not-an-id"):
            with self.subTest(identifier=identifier):
                result = search_details({**self.beatmap, "id": identifier})
                self.assertEqual(result["search_method"], "title")

    def test_string_identifier_is_copied_as_a_single_number(self):
        result = search_details({**self.beatmap, "id": "01762724"})
        self.assertEqual(result["search_text"], "1762724")

    def test_mapper_fallback_preserves_words_without_requiring_quote_support(self):
        result = search_details({"creator": "Foo Bar"})
        self.assertEqual(result["search_text"], "Foo Bar")
        self.assertEqual(result["search_method"], "mapper")

    def test_numeric_metadata_does_not_become_an_unrelated_map_id(self):
        result = search_details({"title": "1984", "creator": "123"})
        self.assertEqual(result["search_text"], "title=1984")
        self.assertEqual(result["search_mapper_text"], "creator=123")

    def test_empty_metadata_has_no_query(self):
        result = search_details({"title": "[]", "creator": None, "id": 0})
        self.assertEqual(result["search_text"], "")
        self.assertEqual(result["search_method"], "unavailable")


if __name__ == "__main__":
    unittest.main()
