"""Manual easing preserves evidence and affects the next recommendations."""
import json
import sqlite3
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from osu_coach.app import Handler
from osu_coach.settings import settings_context
from osu_coach.storage.training_store import TrainingStore
import test_progress_integration as fixtures


class ManualDecreaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.store = TrainingStore(self.db)
        self.profile = {'baseline': 4.46, 'phase': 'training'}
        self.level = self.store.sync('one', self.profile, [], [])

    def test_lowering_is_persistent_and_cannot_be_replayed(self):
        target = self.store.lower('one', self.level['cycle'])
        self.assertEqual(4.21, target)
        lowered = TrainingStore(self.db).sync('one', self.profile, [], [])
        self.assertEqual(target, lowered['stars'])
        self.assertEqual(0, lowered['completed_maps'])
        self.assertNotEqual(self.level['cycle'], lowered['cycle'])
        self.assertEqual('manual_decrease', lowered['history'][0]['type'])
        with self.assertRaises(ValueError): self.store.lower('one', self.level['cycle'])
        self.assertEqual(lowered, self.store.sync('one', self.profile, [], []))

    def test_minimum_and_configurable_step(self):
        with settings_context({'training_decrease_step': .1}):
            self.assertEqual(4.36, self.store.sync('one', self.profile, [], [])['lower_stars'])
            self.assertEqual(4.36, self.store.lower('one', self.level['cycle']))
        low = self.store.sync('low', {'baseline': .6, 'phase': 'training'}, [], [])
        self.assertEqual(.5, self.store.lower('low', low['cycle']))
        minimum = self.store.sync('low', self.profile, [], [])
        self.assertIsNone(minimum['lower_stars'])
        with self.assertRaises(ValueError): self.store.lower('low', minimum['cycle'])

    def test_wrong_profile_and_disabled_progression_cannot_lower(self):
        with self.assertRaises(ValueError): self.store.lower('other', self.level['cycle'])
        with settings_context({'training_progress_enabled': False}):
            with self.assertRaises(ValueError): self.store.lower('one', self.level['cycle'])
        self.assertEqual(self.level, self.store.sync('one', self.profile, [], []))


class ManualDecreaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProgressIntegrationTests('test_empty_progress_exposes_learning_and_no_invented_record')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.coach = self.fixture.coach
        self.fixture.seed()
        self.before = self.coach.state()
        self.level = self.before['profile']['training_level']

    def attempted_quest(self):
        q = next(q for g in self.before['quest_board']['groups'] for q in g['quests']
                 if q['map'].get('training_progress', {}).get('eligible'))
        m = q['map']
        self.coach.add_play(self.fixture.play(100, played_at=datetime.now(timezone.utc).isoformat(),
                                             beatmap_key=m['key'], beatmap_id=m['id'], stars=m['stars'],
                                             accuracy=80, passed=False, completion=.5, misses=20))
        return q

    def test_lowering_replaces_attempted_hard_missions_and_keeps_records(self):
        q = self.attempted_quest()
        tables = ['plays', 'quest_attempts', 'quest_completions', 'coach_progress_points', 'coach_rank_milestones']
        records = {t: self.coach.db.execute('SELECT * FROM '+t).fetchall() for t in tables}
        self.coach.lower_training_level(self.level['cycle'])
        after = self.coach.state()
        lowered = after['profile']['training_level']
        self.assertAlmostEqual(self.level['stars'] - .25, lowered['stars'])
        self.assertEqual(lowered['stars'], after['recommendations'][1]['target'])
        self.assertFalse(any(item['id'] == q['id'] for g in after['quest_board']['groups'] for item in g['quests']))
        skipped = self.coach.db.execute('SELECT data FROM quest_skips WHERE quest_id=?',(q['id'],)).fetchone()
        self.assertEqual('practice_lowered', json.loads(skipped[0])['skipped_reason'])
        self.assertEqual(1, json.loads(skipped[0])['attempt_count'])
        for t in tables: self.assertEqual(records[t], self.coach.db.execute('SELECT * FROM '+t).fetchall())
        self.assertEqual(self.before['coach_progress']['rank'], after['coach_progress']['rank'])

    def test_current_game_keeps_its_objectives_until_it_finishes(self):
        q = self.attempted_quest()
        self.coach.last_snapshot = {'state': {'number': 2}, 'beatmap': {'checksum': q['map']['key'], 'id': q['map']['id']}}
        self.coach.lower_training_level(self.level['cycle'])
        board = self.coach.state()['quest_board']
        kept = next(item for g in board['groups'] for item in g['quests'] if item['id'] == q['id'])
        self.assertEqual(q['map']['expectation'], kept['map']['expectation'])
        self.coach.last_snapshot = {'state': {'number': 5}}
        board = self.coach.state()['quest_board']
        self.assertFalse(any(item['id'] == q['id'] for g in board['groups'] for item in g['quests']))

    def test_endpoint_checks_token_body_and_stale_cycle(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.coach = self.coach
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(body, token=True, origin=None):
            headers = {'Content-Type': 'application/json'}
            if token: headers['X-Coach-Token'] = self.coach.token
            if origin: headers['Origin'] = origin
            # Auth failures are checked before the body is read. Sending no body
            # avoids a Windows TCP reset while testing that early rejection.
            payload = json.dumps(body).encode() if token and origin is None else b''
            req = Request(f'http://127.0.0.1:{server.server_port}/api/training/lower', data=payload, headers=headers)
            with urlopen(req, timeout=5) as response: return response.status
        try:
            for body, token, origin, status in [({'cycle':self.level['cycle']},False,None,403),
                    ({'cycle':self.level['cycle']},True,'https://example.com',403),
                    ({'cycle':123},True,None,400), ({'cycle':self.level['cycle'],'amount':3},True,None,400)]:
                with self.assertRaises(HTTPError) as error: post(body,token,origin)
                self.assertEqual(status,error.exception.code)
            self.assertEqual(200,post({'cycle':self.level['cycle']}))
            with self.assertRaises(HTTPError) as error: post({'cycle':self.level['cycle']})
            self.assertEqual(400,error.exception.code)
            self.assertEqual(1,len(self.coach.state()['profile']['training_level']['history']))
        finally:
            server.shutdown();server.server_close();thread.join(2)
