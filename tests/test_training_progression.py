"""Progressive practice: exact evidence, focused goals and safe persistence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import tempfile
import unittest

from osu_coach.core import engine, training, mod_policy
from osu_coach.core.expectations import expectation_for
from osu_coach.core.quest_rules import evaluate_attempt
from osu_coach.settings import settings_context, validate_settings, DEFAULTS
from osu_coach.app import Coach

NOW = datetime.now(timezone.utc)


def beatmap(i=1, **changes):
    return {'key': f'{i:032x}', 'id': i, 'set_id': i, 'title': f'Song {i}', 'version': 'Insane',
            'artist': 'Artist', 'mode': 0, 'stars': 4.5, 'od': 8, 'ar': 9, 'bpm': 180,
            'object_count': 400, 'max_combo': 500, 'length': 150, 'source': 'local',
            'play_conditions': mod_policy.context(), **changes}


def play(i=1, days=0, **changes):
    return {'id': f'play-{i}-{days}', 'beatmap_key': f'{i:032x}', 'beatmap_id': i, 'player': 'Tester',
            'played_at': (NOW - timedelta(days=days, minutes=2)).isoformat(), 'mode': 0, 'stars': 4.5,
            'accuracy': 96, 'misses': 10, 'max_combo': 350, 'map_max_combo': 500, 'object_count': 400,
            'judged_objects': 400, 'passed': True, 'completion': 1, 'client': 'lazer', 'mods': [],
            'mod_key': '{"mods":[],"rate":1.0}', 'od': 8, 'ar': 9, 'bpm': 180, 'title': 'Song', **changes}


class FocusedGoals(unittest.TestCase):
    def profile(self, **changes):
        return {'window': [play(i, accuracy=96, misses=4) for i in range(1, 7)], 'baseline': 4.5,
                'phase': 'training', 'challenge_unlocked': True, **changes}

    def test_accuracy_goal_does_not_require_grade_misses_or_combo(self):
        m = beatmap()
        expected = training.training_goal(expectation_for(m, self.profile(), 'practice'), m, self.profile(), 'practice')
        q = {'map': {**m, 'expectation': expected}, 'created_at': (NOW - timedelta(hours=1)).isoformat()}
        result = evaluate_attempt(q, play(accuracy=expected['accuracy_min'], grade='B', misses=6, max_combo=10), now=NOW)
        self.assertTrue(result['completed'])
        self.assertEqual(['complete', 'accuracy'], expected['required_keys'])
        self.assertFalse(next(c for c in result['checks'] if c['key'] == 'grade')['required'])
        self.assertAlmostEqual(96.5, expected['accuracy_min'])

    def test_full_completion_and_exact_mods_remain_mandatory(self):
        m = beatmap()
        expected = training.training_goal(expectation_for(m, self.profile(), 'practice'), m, self.profile(), 'practice')
        q = {'map': {**m, 'expectation': expected}, 'created_at': (NOW - timedelta(hours=1)).isoformat()}
        for changes in ({'completion': .5}, {'passed': False}, {'mods': ['HD']}, {'accuracy_rounded': True}):
            with self.subTest(changes=changes):
                self.assertFalse(evaluate_attempt(q, play(accuracy=100, **changes), now=NOW)['completed'])

    def test_challenge_allows_misses_without_grade_or_fc(self):
        m = beatmap()
        expected = training.training_goal(expectation_for(m, self.profile(), 'challenge'), m, self.profile(), 'challenge')
        self.assertGreater(expected['misses_max'], 0)
        self.assertEqual(['complete', 'misses', 'accuracy'], expected['required_keys'])
        self.assertLessEqual(expected['accuracy_min'], DEFAULTS['challenge_accuracy'])

    def test_consolidation_keeps_accuracy_and_miss_control(self):
        m = beatmap()
        expected = training.training_goal(expectation_for(m, self.profile(), 'consolidate'), m, self.profile(), 'consolidate')
        self.assertEqual(['complete', 'accuracy', 'misses'], expected['required_keys'])

    def test_benchmark_reduces_actual_misses_and_keeps_original_baseline(self):
        m = training.benchmark_candidates([beatmap()], [play(days=8)], now=NOW)[0]
        expected = training.training_goal(expectation_for(m, self.profile(), 'practice'), m, self.profile(), 'practice')
        self.assertEqual(8, expected['misses_max'])
        self.assertEqual(10, expected['benchmark_reference']['misses'])
        self.assertEqual('benchmark', expected['training_role'])

    def test_wrong_od_mods_or_pattern_cannot_raise_prediction(self):
        m = beatmap(tags=['skillset/streams'])
        current = self.profile(window=[play(i, accuracy=100, od=3, tags=['skillset/jumps']) for i in range(8)])
        self.assertEqual(0, expectation_for(m, current, 'practice')['samples'])
        current['window'] = [play(i, accuracy=100, mods=['HD'], tags=['skillset/streams']) for i in range(8)]
        self.assertEqual(0, expectation_for(m, current, 'practice')['samples'])


class BenchmarksAndEvolution(unittest.TestCase):
    def test_cooldown_uses_latest_attempt_even_if_failed(self):
        history = [play(days=15), play(days=1, passed=False, completion=.3)]
        self.assertEqual([], training.benchmark_candidates([beatmap()], history, now=NOW))
        self.assertEqual(1, len(training.benchmark_candidates([beatmap()], history[:1], now=NOW)))

    def test_disabled_benchmarks_and_changed_revision_are_excluded(self):
        with settings_context({'benchmark_enabled': False}):
            self.assertEqual([], training.benchmark_candidates([beatmap()], [play(days=15)], now=NOW))
        self.assertEqual([], training.benchmark_candidates([beatmap(key='f' * 32)], [play(days=15)], now=NOW))

    def test_benchmark_requires_installed_map_and_equivalent_conditions(self):
        for changes in ({'source': 'online'}, {'od': 9}, {'play_conditions': mod_policy.context('HD')}):
            self.assertEqual([], training.benchmark_candidates([beatmap(**changes)], [play(days=15)], now=NOW))

    def test_evolution_reports_tradeoffs_without_mixing_personal_bests(self):
        before = play(days=12, accuracy=95, misses=10, max_combo=400)
        after = play(days=0, accuracy=97, misses=8, max_combo=350)
        result = training.evolution([before, after])
        self.assertEqual(1, result['compared_maps'])
        item = result['comparisons'][0]
        self.assertTrue(item['improved'])
        self.assertTrue(item['has_setback'])
        self.assertEqual([2, -2, -50], [c['delta'] for c in item['changes']])
        self.assertEqual(before['id'], item['before_id'])

    def test_partial_accuracy_never_counts_as_improvement(self):
        result = training.evolution([play(days=12), play(accuracy=100, misses=0, passed=False, completion=.1)])
        self.assertEqual(0, result['improved_maps'])
        self.assertEqual([], result['comparisons'][0]['changes'])

    def test_same_session_mod_change_and_od_change_are_not_comparable(self):
        for other in (play(id='another'), play(days=0, mods=['DT']), play(days=0, od=9)):
            before = play(days=0 if other['id'] == 'another' else 12)
            self.assertEqual(0, training.evolution([before, other])['compared_maps'])

    def test_excluded_and_duplicate_attempts_do_not_create_progress(self):
        before, after = play(days=12), play(accuracy=98, excluded=True)
        self.assertEqual(0, training.evolution([before, after])['compared_maps'])

    def test_evolution_can_use_results_older_than_session_and_reference(self):
        rows = [play(days=50, accuracy=94), play(accuracy=97)]
        self.assertEqual(1, len(engine.recent_window(rows, NOW)))
        trend = engine.recent_window(rows, NOW, limit=1000, days=90)
        self.assertEqual(1, training.evolution(trend)['improved_maps'])


class SkillAndSelectionTests(unittest.TestCase):
    def test_sparse_skill_is_unknown_and_distinct_maps_and_sessions_are_needed(self):
        rows = [play(i, tags=['skillset/streams']) for i in range(1, 8)]
        result = training.skill_references(rows, 4.5, NOW)
        self.assertTrue(all(item['reference'] is None for item in result))

    def test_skill_references_change_targets_without_changing_general_level(self):
        rows = [play(i, days=day, accuracy=96, misses=2, stars=4, tags=['skillset/streams']) for i in range(1, 6) for day in (0, 2)]
        levels = training.skill_references(rows, 4.5, NOW)
        player = {'skill_levels': levels}
        self.assertEqual(4, training.skill_target(beatmap(tags=['skillset/streams']), player, 4.5, 4.5))
        self.assertAlmostEqual(3.65, training.skill_target(beatmap(tags=['skillset/streams']), player, 4.15, 4.5))
        self.assertEqual(4.5, training.skill_target(beatmap(), player, 4.5, 4.5))

    def test_bpm_is_open_by_default_and_strict_when_configured(self):
        profile = engine.assess([play(i, misses=1, bpm=100) for i in range(1, 7)], NOW)
        m = beatmap(100, stars=profile['baseline'], bpm=190)
        self.assertTrue(engine.recommend([m], profile, stages={'practice'})[0]['maps'])
        with settings_context({'bpm_hard_limit': True}):
            self.assertFalse(engine.recommend([m], profile, stages={'practice'})[0]['maps'])

    def test_one_challenge_in_main_practice_and_consolidation_remains_controlled(self):
        profile = engine.assess([play(i, accuracy=98, misses=0) for i in range(1, 7)], NOW)
        maps = [beatmap(100+i, stars=profile['baseline'] - .5 + i*.035) for i in range(28)]
        groups = engine.recommend(maps, profile, fill_online=True)
        self.assertEqual('Consolidar', groups[2]['label'])
        self.assertEqual(1, sum(m['training_role'] == 'challenge' for m in groups[1]['maps']))
        self.assertTrue(all(m['training_role'] == 'consolidate' for m in groups[2]['maps']))

    def test_multiple_due_benchmarks_do_not_empty_regular_slots(self):
        profile = engine.assess([play(i, misses=1) for i in range(1, 7)], NOW)
        maps = [beatmap(100+i, stars=profile['baseline'], benchmark={'accuracy': 95, 'misses': 10, 'played_at': NOW.isoformat()}) for i in range(5)]
        maps += [beatmap(200+i, stars=profile['baseline']) for i in range(6)]
        groups = engine.recommend(maps, profile, fill_online=True)
        self.assertLessEqual(sum(bool(m.get('benchmark')) for g in groups for m in g['maps']), 1)
        self.assertEqual(3, len(groups[1]['maps']))

    def test_density_changes_with_notes_even_when_bpm_stays_equal(self):
        from osu_coach.beatmaps.catalog import _metadata
        prefix = '[General]\nMode:0\n[TimingPoints]\n0,500,4,2,1,100,1,0\n[HitObjects]\n'
        def content(gap):
            return (prefix + '\n'.join(f'100,100,{i*gap},1,0' for i in range(50))).encode()
        self.assertGreater(_metadata(content(125))['note_density'], _metadata(content(250))['note_density'])



class AppProgressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None, no_tosu=True, tosu_url='http://127.0.0.1:24050/json/v2')
        self.coach = Coach(self.args)
        self.coach.config['since'] = (NOW - timedelta(days=80)).isoformat()
        self.coach.catalog = [beatmap(i, stars=4.0+i*.025) for i in range(1, 45)]

    def tearDown(self):
        self.coach.close()
        self.coach.db.close()
        self.temp.cleanup()

    def test_state_exposes_history_and_benchmark_without_resetting_rank(self):
        self.coach.add_play(play(20, days=50, accuracy=94))
        self.coach.add_play(play(20, days=10, accuracy=97))
        state = self.coach.state()
        self.assertEqual(2, state['player_profile']['evolution']['plays'])
        self.assertEqual(1, state['player_profile']['evolution']['improved_maps'])
        self.assertEqual(1, state['profile']['attempts'])
        self.assertTrue(state['recommendation_policy']['benchmark_enabled'])
        self.assertLessEqual(sum(bool(q['map'].get('benchmark')) for g in state['quest_board']['groups'] for q in g['quests']), 1)
        self.assertEqual(2, len(self.coach.plays()))

    def test_old_started_mission_is_frozen_and_pending_ones_can_migrate(self):
        self.coach.add_play(play(20))
        first = self.coach.state()['quest_board']
        quests = [q for g in first['groups'] for q in g['quests']]
        for q in quests:
            q['map']['expectation'].pop('model_version', None)
        protected = quests[0]
        protected.update(status='in_progress', attempt_count=1)
        with self.coach.db:
            self.coach.quest_store._save(first)
        updated = self.coach.state()['quest_board']
        saved = next(q for g in updated['groups'] for q in g['quests'] if q['id'] == protected['id'])
        self.assertEqual(protected['map']['expectation'], saved['map']['expectation'])
        self.assertTrue(any(q['map']['expectation'].get('model_version') == 1 for g in updated['groups'] for q in g['quests']))


if __name__ == '__main__':
    unittest.main()
