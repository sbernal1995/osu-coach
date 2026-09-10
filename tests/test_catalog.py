"""Pruebas con mapas pequeños generados en carpetas temporales."""

import builtins
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from osu_coach.beatmaps import catalog

try:
    import rosu_pp_py as rosu
except ImportError:
    rosu = None


MAP = """osu file format v14

[General]
AudioFilename: song.mp3
Mode: 0

[Metadata]
Title: Example
TitleUnicode: Ejemplo ágil
Artist: Artist
ArtistUnicode: Artista
Creator: Mapper
Version: Normal
BeatmapID: 123
BeatmapSetID: 456

[Difficulty]
HPDrainRate:5
CircleSize:4
OverallDifficulty:5
ApproachRate:7
SliderMultiplier:1.4
SliderTickRate:1

[TimingPoints]
0,500,4,2,1,100,1,0

[HitObjects]
64,192,1000,1,0,0:0:0:0:
448,192,1500,1,0,0:0:0:0:
256,64,2000,1,0,0:0:0:0:
256,320,2500,1,0,0:0:0:0:
64,192,3000,1,0,0:0:0:0:
448,192,3500,1,0,0:0:0:0:
256,64,4000,1,0,0:0:0:0:
256,320,4500,1,0,0:0:0:0:
64,192,5000,1,0,0:0:0:0:
448,192,5500,1,0,0:0:0:0:
256,64,6000,1,0,0:0:0:0:
256,320,6500,1,0,0:0:0:0:
"""


class CatalogParsingTests(unittest.TestCase):
    def test_romanized_search_metadata_is_separate_from_unicode_presentation(self):
        content = MAP.replace("Title: Example", "Title: Harumachi Clover (Swing Arrangement) [Dictate Edit]")
        content = content.replace("TitleUnicode: Ejemplo ágil", "TitleUnicode: 春待ちクローバー (Swing Arrangement) [Dictate Edit]")
        metadata = catalog._metadata(content.encode())
        self.assertEqual("春待ちクローバー (Swing Arrangement) [Dictate Edit]", metadata["title"])
        self.assertEqual("Harumachi Clover (Swing Arrangement) [Dictate Edit]", metadata["title_romanized"])
        self.assertEqual("Artista", metadata["artist"])
        self.assertEqual("Artist", metadata["artist_romanized"])

    def test_mapper_tags_remain_separate_from_skill_tags(self):
        content = MAP.replace("Creator: Mapper", "Creator: Mapper\nTags: anime artist-name jumps anime")
        metadata = catalog._metadata(content.encode())
        self.assertEqual(["anime", "artist-name", "jumps"], metadata["mapper_tags"])
        self.assertNotIn("tags", metadata)

    def test_missing_dependency_is_explained(self):
        original_import = builtins.__import__

        def fail_rosu(name, *args, **kwargs):
            if name == "rosu_pp_py":
                raise ImportError("simulated")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_rosu):
            with self.assertRaisesRegex(RuntimeError, "Falta rosu-pp-py"):
                catalog.scan_catalog("missing")

    def test_duration_includes_spinner_and_slider_end(self):
        spinner = MAP + "256,192,7000,8,0,9000,0:0:0:0:\n"
        self.assertEqual(catalog._metadata(spinner.encode())["length"], 8)
        slider = MAP + "256,192,7000,2,0,L|400:192,2,280\n"
        self.assertEqual(catalog._metadata(slider.encode())["length"], 8)

    def test_inherited_timing_changes_slider_duration(self):
        accelerated = MAP.replace("[HitObjects]", "6000,-50,4,2,1,100,0,0\n\n[HitObjects]")
        accelerated += "256,192,7000,2,0,L|400:192,2,280\n"
        self.assertEqual(catalog._metadata(accelerated.encode())["length"], 7)

    def test_nan_values_and_empty_objects_rejected(self):
        with self.assertRaises(catalog.InvalidBeatmap):
            catalog._metadata(MAP.replace("1000,1", "NaN,1").encode())
        with self.assertRaises(catalog.InvalidBeatmap):
            catalog._metadata(MAP.split("[HitObjects]")[0].encode())

    def test_binary_resources_rejected_after_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("a" * 64)
            path.write_bytes(b"not an osu map" * 1000)
            with self.assertRaises(catalog.InvalidBeatmap):
                catalog._read_map(path)

    def test_large_map_is_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.osu"
            with path.open("wb") as file:
                file.truncate(catalog.MAX_MAP_BYTES + 1)
            with patch.object(Path, "open", side_effect=AssertionError("must not open")):
                with self.assertRaises(catalog.InvalidBeatmap):
                    catalog._read_map(path)


@unittest.skipIf(rosu is None, "rosu-pp-py no está instalado")
class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "normal.osu"
        self.path.write_text(MAP, encoding="utf-8", newline="\n")

    def test_metadata_and_md5_use_exact_content(self):
        progress = []
        rows = catalog.scan_catalog(self.root, progress.append)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["key"], hashlib.md5(self.path.read_bytes()).hexdigest())
        self.assertEqual(row["id"], 123)
        self.assertEqual(row["set_id"], 456)
        self.assertEqual(row["title"], "Ejemplo ágil")
        self.assertEqual(row["title_romanized"], "Example")
        self.assertEqual(row["artist"], "Artista")
        self.assertEqual(row["artist_romanized"], "Artist")
        self.assertEqual(row["bpm"], 120)
        self.assertEqual(row["object_count"], 12)
        self.assertEqual(row["max_combo"], 12)
        self.assertGreater(row["stars"], 0)
        self.assertEqual(row["source"], "local")
        self.assertEqual(progress, [0, 1])
        self.assertEqual(self.path.read_text(encoding="utf-8"), MAP)

    def test_hashed_lazer_file_and_duplicates(self):
        directory = self.root / ".hidden" / "0a"
        directory.mkdir(parents=True)
        hashed = directory / ("a" * 64)
        hashed.write_bytes(self.path.read_bytes())
        self.assertEqual(len(catalog.scan_catalog(self.root)), 1)
        self.path.unlink()
        rows = catalog.scan_catalog(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(hashed.resolve()))

    def test_bad_files_and_other_modes_do_not_stop_scan(self):
        (self.root / "empty.osu").write_text("osu file format v14\n")
        (self.root / "broken.osu").write_text(MAP + "not,an,object")
        (self.root / "mania.osu").write_text(MAP.replace("Mode: 0", "Mode: 3"))
        (self.root / ("b" * 64)).write_bytes(b"image resource")
        self.assertEqual(len(catalog.scan_catalog(self.root)), 1)

    def test_missing_directory_is_explicit(self):
        with self.assertRaises(FileNotFoundError):
            catalog.scan_catalog(self.root / "absent")

    def test_mods_recalculate_stars_and_attributes(self):
        normal = catalog.difficulty_for(self.path)
        faster = catalog.difficulty_for(self.path, "DT")
        self.assertEqual(faster["clock_rate"], 1.5)
        self.assertEqual(faster["bpm"], 180)
        self.assertGreater(faster["stars"], normal["stars"])
        self.assertGreater(faster["ar"], normal["ar"])
        self.assertLess(faster["length"], normal["length"])
        hardrock = catalog.difficulty_for(self.path, "HR")
        self.assertGreater(hardrock["cs"], normal["cs"])

    def test_custom_speed_mod(self):
        result = catalog.difficulty_for(
            self.path, [{"acronym": "DT", "settings": {"speed_change": 1.2}}]
        )
        self.assertAlmostEqual(result["clock_rate"], 1.2)
        self.assertAlmostEqual(result["bpm"], 144)

    def test_explicit_clock_rate_applies_without_mods(self):
        result = catalog.difficulty_for(self.path, clock_rate=1.2)
        self.assertAlmostEqual(result["clock_rate"], 1.2)
        self.assertAlmostEqual(result["bpm"], 144)

    def test_explicit_clock_rate_overrides_mod_speed(self):
        result = catalog.difficulty_for(self.path, "DT", clock_rate=1.25)
        self.assertAlmostEqual(result["clock_rate"], 1.25)
        self.assertAlmostEqual(result["bpm"], 150)

    def test_invalid_clock_rate_is_rejected(self):
        for rate in (0, -1, float("nan"), float("inf")):
            with self.subTest(rate=rate), self.assertRaises(catalog.InvalidBeatmap):
                catalog.difficulty_for(self.path, clock_rate=rate)

    def test_suspicious_native_map_is_skipped_before_calculation(self):
        real_beatmap = rosu.Beatmap(bytes=MAP.encode())

        class SuspiciousMap:
            mode = real_beatmap.mode
            n_objects = real_beatmap.n_objects

            def is_suspicious(self):
                return True

        with patch.object(rosu, "Beatmap", return_value=SuspiciousMap()):
            with patch.object(rosu, "Difficulty", side_effect=AssertionError("must not calculate")):
                self.assertEqual(catalog.scan_catalog(self.root), [])


if __name__ == "__main__":
    unittest.main()
