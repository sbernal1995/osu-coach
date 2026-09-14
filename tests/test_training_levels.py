"""Practice levels are earned by distinct completed missions, never by rereads."""
import copy
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from osu_coach.storage.training_store import TrainingStore
from osu_coach.core import engine
from osu_coach.settings import settings_context
from test_training_progression import beatmap, play

NOW = datetime.now(timezone.utc)


class TrainingLevels(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.store = TrainingStore(self.db)
        self.profile = {'baseline': 4.46, 'phase': 'training', 'training_adjustments': {}}
        self.level = self.store.sync('profile/epoch', self.profile, [], [])

    def completion(self, i, hour=0, **changes):
        p = play(i, accuracy=98, misses=1, played_at=(NOW + timedelta(hours=hour, minutes=i)).isoformat())
        q = {'id': str(i), 'status': 'completed', 'completed_play_id': p['id'], 'completed_at': p['played_at'],
             'map': beatmap(i, training_progress={'cycle': self.level['cycle'], 'eligible': True})}
        q.update(changes)
        return q, p

    def sync(self, rows, scope='profile/epoch'):
        return self.store.sync(scope, self.profile, [q for q, p in rows], [p for q, p in rows])

    def test_initial_reference_is_frozen_across_rereads_and_restart(self):
        self.profile['baseline'] = 3.5
        result = TrainingStore(self.db).sync('profile/epoch', self.profile, [], [])
        self.assertEqual(4.46, result['stars'])
        self.assertEqual(self.level['cycle'], result['cycle'])

    def test_three_distinct_maps_in_two_sessions_advance_once(self):
        rows = [self.completion(1), self.completion(2), self.completion(3, hour=2)]
        result = self.sync(rows)
        self.assertEqual(4.56, result['stars'])
        self.assertEqual(0, result['completed_maps'])
        self.assertEqual(1, len(result['history']))
        self.assertEqual(result, self.sync(rows))
        self.assertEqual(result, TrainingStore(self.db).sync('profile/epoch', self.profile, [q for q,p in rows], [p for q,p in rows]))

    def test_single_session_does_not_advance_but_later_session_can(self):
        rows = [self.completion(i) for i in range(1, 4)]
        self.assertEqual(4.46, self.sync(rows)['stars'])
        rows.append(self.completion(4, hour=2))
        self.assertEqual(4.56, self.sync(rows)['stars'])

    def test_duplicate_difficulties_and_old_or_incomplete_missions_do_not_count(self):
        first = self.completion(1)
        duplicate = self.completion(2)
        duplicate[0]['map'] = copy.deepcopy(first[0]['map'])
        old = self.completion(3)
        old[0]['map']['training_progress']['cycle'] = 'older'
        warmup = self.completion(4)
        warmup[0]['map']['training_progress']['eligible'] = False
        pending = self.completion(5, status='in_progress')
        result = self.sync([first, duplicate, old, warmup, pending])
        self.assertEqual(1, result['completed_maps'])

    def test_recalibration_and_other_profiles_start_separate_levels(self):
        self.sync([self.completion(1)])
        self.assertEqual(0, self.sync([], 'other/epoch')['completed_maps'])
        self.assertEqual(0, self.sync([], 'profile/new-epoch')['completed_maps'])
        self.assertEqual(1, self.sync([])['completed_maps'])

    def test_settings_do_not_change_active_cycle_or_retroactively_award(self):
        with settings_context({'training_step': .25, 'training_required_maps': 1, 'training_required_sessions': 1}):
            result = self.sync([self.completion(1)])
            self.assertEqual(4.46, result['stars'])
            self.assertEqual(3, result['required_maps'])
            rows = [self.completion(1), self.completion(2), self.completion(3, hour=2)]
            result = self.sync(rows)
            self.assertEqual(4.56, result['stars'])
            self.assertEqual(.25, result['step'])

    def test_recovery_preserves_level_and_defers_ascent(self):
        self.profile['training_adjustments']['mode'] = 'recover'
        rows = [self.completion(1), self.completion(2), self.completion(3, hour=2)]
        self.assertEqual(4.46, self.sync(rows)['stars'])
        self.profile['training_adjustments']['mode'] = 'advance'
        self.assertEqual(4.46, self.sync(rows)['stars'])
        self.assertEqual(4.56, self.sync(rows + [self.completion(4, hour=3)])['stars'])

    def test_disabled_and_uncalibrated_profiles_do_not_create_levels(self):
        with settings_context({'training_progress_enabled': False}):
            self.assertIsNone(self.sync([], 'other'))
        self.profile['phase'] = 'calibrating'
        self.assertIsNone(self.sync([], 'other'))

    def test_intervening_plays_prevent_artificial_session_split(self):
        rows = [self.completion(1), self.completion(2), self.completion(3, hour=2)]
        attempts = [p for q,p in rows] + [play(50+i, played_at=(NOW + timedelta(minutes=30*i)).isoformat()) for i in range(1,5)]
        result = self.store.sync('profile/epoch', self.profile, [q for q,p in rows], attempts)
        self.assertEqual(1, result['completed_sessions'])
        self.assertEqual(4.46, result['stars'])

    def test_practice_target_uses_earned_level_while_baseline_is_preserved(self):
        profile = {'baseline': 4.0, 'window': [], 'session_window': [], 'phase': 'training',
                   'challenge_unlocked': False, 'session': {'focus': 'Completar con control', 'message': 'Recuperar control'}, 'training_level': {**self.level, 'stars': 4.6}}
        player = {'status': 'ready', 'progression': {'mode': 'consolidate'}}
        adjusted = engine.apply_player_profile(profile, player)
        groups = engine.recommend([beatmap(1, stars=4.6)], adjusted, player_profile=player)
        self.assertEqual(4.6, groups[1]['target'])
        self.assertEqual(4.0, adjusted['baseline'])
        m = groups[1]['maps'][0]
        self.assertTrue(m['training_progress']['eligible'])
        self.assertIn('misses', m['expectation']['required_keys'])
        profile['session_window'] = [play(i, stars=4.7, passed=False) for i in range(2)]
        recovering = engine.apply_player_profile(profile, player)
        self.assertEqual('recover', recovering['training_adjustments']['mode'])
        self.assertEqual(4.6, recovering['training_level']['stars'])
        self.assertEqual(4.35, engine.recommend([], recovering)[1]['target'])

    def test_warmups_lower_maps_and_benchmarks_do_not_offer_credit(self):
        profile = {'baseline': 4.46, 'window': [], 'phase': 'training', 'challenge_unlocked': False,
                   'training_level': self.level}
        groups = engine.recommend([beatmap(i, stars=4.2) for i in range(1, 8)], profile)
        for group in groups:
            for m in group['maps']:
                self.assertFalse(m['training_progress']['eligible'])
