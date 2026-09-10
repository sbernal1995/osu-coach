"""Independent long-term reference and short-term session regressions."""
from datetime import timedelta
import unittest

from osu_coach.core import engine
from osu_coach.core.evidence import recency_weight, session_ids, session_count, trimmed_weighted_mean
from tests.test_engine import NOW, play, beatmap


class MemoryWindowTests(unittest.TestCase):
    def recent(self, index, **changes):
        return play(index, played_at=(NOW - timedelta(minutes=200-index)).isoformat(), **changes)

    def test_reference_keeps_more_than_twenty_without_expanding_session(self):
        scores = [self.recent(i, stars=3 if i < 30 else 4) for i in range(50)]
        result = engine.assess(scores, NOW)
        short_only = engine.assess(scores[-20:], NOW)
        self.assertEqual(result['attempts'], 50)
        self.assertEqual(result['session']['attempts'], 20)
        self.assertEqual(result['session_window'], scores[-20:])
        self.assertLess(result['baseline'], short_only['baseline'] - .3)

    def test_session_and_reference_have_separate_age_limits(self):
        scores = [play(0, played_at=(NOW-timedelta(days=8)).isoformat()),
                  play(1, played_at=(NOW-timedelta(days=31)).isoformat()), self.recent(2)]
        result = engine.assess(scores, NOW)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['session']['attempts'], 1)
        self.assertEqual(result['session_window'], [scores[-1]])

    def test_old_fast_maps_cannot_loosen_the_current_sessions_tempo_limit(self):
        old = [play(i, bpm=240, played_at=(NOW-timedelta(days=10,minutes=i)).isoformat()) for i in range(50)]
        current = [self.recent(i+60, bpm=150) for i in range(20)]
        profile = engine.assess(old+current, NOW)
        candidates = [beatmap(1, stars=profile['baseline'], bpm=165),
                      beatmap(2, stars=profile['baseline'], bpm=166)]
        selected = [m['key'] for g in engine.recommend(candidates, profile) for m in g['maps']]
        self.assertIn('candidate-1', selected)
        self.assertNotIn('candidate-2', selected)

    def test_two_bad_attempts_adjust_session_while_supported_reference_stays_stable(self):
        scores = [self.recent(i) for i in range(30)]
        before = engine.assess(scores, NOW)
        scores += [self.recent(i, passed=False, accuracy=80, misses=30, completion=.6) for i in (30,31)]
        result = engine.assess(scores, NOW)
        self.assertEqual(result['baseline'], before['baseline'])
        self.assertEqual(result['session']['adjustment'], -.25)
        self.assertFalse(result['challenge_unlocked'])
        adjusted = engine.apply_player_profile(result, {'status':'learning','progression':{},'priorities':[]})
        self.assertEqual(adjusted['training_adjustments']['practice_offset'], -.25)

    def test_one_extreme_map_cannot_raise_the_reference(self):
        scores = [self.recent(i) for i in range(30)]
        before = engine.assess(scores, NOW)
        result = engine.assess(scores+[self.recent(31, stars=9)], NOW)
        self.assertEqual(result['baseline'], before['baseline'])

    def test_newer_maps_weigh_more_than_older_maps(self):
        old = [play(i, stars=3, played_at=(NOW-timedelta(days=20,minutes=i)).isoformat()) for i in range(10)]
        new = [self.recent(i+20, stars=4) for i in range(10)]
        higher_recent = engine.assess(old+new, NOW)['baseline']
        lower_recent = engine.assess([{**p,'stars':4} for p in old]+[{**p,'stars':3} for p in new], NOW)['baseline']
        self.assertGreater(higher_recent, lower_recent + .5)

    def test_retries_do_not_duplicate_one_maps_weight(self):
        scores = [self.recent(i, stars=3) for i in range(15)]
        scores += [self.recent(20, stars=4, key='practised')]
        before = engine.assess(scores, NOW)
        repeated = scores+[self.recent(i, stars=4, key='practised') for i in range(21,60)]
        after = engine.assess(repeated, NOW)
        self.assertEqual(before['baseline'], after['baseline'])
        self.assertEqual(after['distinct_maps'], 16)

    def test_reset_excludes_all_earlier_evidence_from_both_windows(self):
        scores = [self.recent(i) for i in range(30)]
        result = engine.assess(scores, NOW, since=scores[-2]['played_at'])
        self.assertEqual(result['window'], scores[-2:])
        self.assertEqual(result['session_window'], scores[-2:])
        self.assertEqual(result['phase'], 'calibrating')


class EvidenceHelperTests(unittest.TestCase):
    def test_recency_halves_every_ten_days(self):
        self.assertEqual(recency_weight({'played_at':NOW.isoformat()}, NOW), 1)
        self.assertEqual(recency_weight({'played_at':(NOW-timedelta(days=10)).isoformat()}, NOW), .5)
        self.assertEqual(recency_weight({'played_at':(NOW-timedelta(days=20)).isoformat()}, NOW), .25)

    def test_full_session_assignment_prevents_filtered_gaps_inventing_sessions(self):
        scores = [play(i, played_at=(NOW-timedelta(minutes=120-40*i)).isoformat()) for i in range(4)]
        sessions = session_ids(scores)
        self.assertEqual(session_count([scores[0],scores[-1]], sessions), 1)
        extra = play(4, played_at=(NOW+timedelta(hours=1)).isoformat())
        self.assertEqual(session_count(scores+[extra]), 2)

    def test_trimming_uses_partial_weights_without_overweighting_extremes(self):
        self.assertAlmostEqual(trimmed_weighted_mean([(0,1),(3,3),(10,1)]), 3)
        self.assertAlmostEqual(trimmed_weighted_mean([(2,1)]), 2)


if __name__ == '__main__':
    unittest.main()
