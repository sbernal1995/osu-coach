"""La sensación de dificultad alimenta evaluación y recomendaciones sin tocar rangos."""
from datetime import datetime, timedelta, timezone
import unittest

from osu_coach.core import engine
from osu_coach.core.feel import apply_feel
from osu_coach.core.progress_rules import rank_evidence


NOW = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)


def play(index, *, stars, felt=0.0, **changes):
    result = {
        "id": f"feel-attempt-{index}",
        "beatmap_key": f"feel-map-{index}",
        "played_at": (NOW - timedelta(minutes=60 - index)).isoformat(),
        "mode": 0, "accuracy": 99.0, "object_count": 400, "judged_objects": 400,
        "misses": 0, "max_combo": 490, "map_max_combo": 500,
        "passed": True, "completion": 1.0,
        "bpm": 150, "ar": 7.0, "length": 120,
        "player": "Jugador", "client": "lazer", "mods": [],
    }
    if felt:
        # Mirrors the coach's idempotent stamp: measured value survives in stars_sr.
        result["stars_sr"] = stars
        result["stars"] = apply_feel(stars, felt)
        result["feel"] = felt
    else:
        result["stars"] = stars
    result.update(changes)
    return result


def beatmap(key, stars, **changes):
    result = {
        "key": key, "id": 1, "set_id": 1, "title": f"Canción {key}", "version": "Práctica",
        "mode": 0, "stars": stars, "bpm": 150, "ar": 7.0, "length": 120,
        "source": "local", "local": True,
    }
    result.update(changes)
    return result


class FeelAssessmentTests(unittest.TestCase):
    def test_baseline_uses_effective_stars(self):
        measured = engine.assess([play(i, stars=3.0) for i in range(5)], NOW)
        felt = engine.assess([play(i, stars=3.0, felt=0.5) for i in range(5)], NOW)
        self.assertGreater(felt["baseline"], measured["baseline"] + 0.3)
        self.assertLessEqual(felt["baseline"], 4.0)
        self.assertEqual(felt["window"][0]["stars_sr"], 3.0)


class FeelRecommendationTests(unittest.TestCase):
    def test_warmup_band_follows_effective_stars(self):
        profile = engine.assess([play(i, stars=4.0) for i in range(5)], NOW)
        profile.update(baseline=4.0, challenge_unlocked=True)
        # m-up measures 3.5 but feels hard (+0.5 → 4.0): out of the warmup band.
        # m-down measures 4.0 but feels soft (-0.5 → 3.5): inside the warmup band.
        maps = [beatmap("feel-up", 4.0), beatmap("feel-down", 3.5)]
        groups = engine.recommend(maps, profile, stages={"warmup"}, limit=1, fill_online=True)
        self.assertEqual([item["key"] for item in groups[0]["maps"]], ["feel-down"])


class FeelRankEvidenceTests(unittest.TestCase):
    def test_ranks_prove_measured_stars_not_the_personal_feel(self):
        # Measured 4.75 across three maps; a huge personal feel should never
        # move the earned rung toward the felt 5.5 reading.
        rows = [play(i, stars=4.75, felt=0.75) for i in range(3)]
        result = rank_evidence({"phase": "training", "baseline": 4.66, "window": rows})
        self.assertEqual(result["candidate_stars"], 4.75)
        self.assertEqual(result["completed_maps"], 3)
        proof = rank_evidence({"phase": "training", "baseline": 4.66, "window": rows}, earned_stars=4.5)
        self.assertEqual([item["stars"] for item in proof["qualifying_maps"]], [4.75, 4.75, 4.75])


if __name__ == "__main__":
    unittest.main()