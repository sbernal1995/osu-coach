"""Settings persist safely and drive real discovery without external network calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Coach, Handler, ThreadingHTTPServer
from discovery_source import candidate_quality_ok, parse_candidates
from discovery_store import DiscoveryStore
from settings import DEFAULTS, SCHEMA, get_setting, settings_context, validate_settings
from tests.test_discovery_source import beatmapset
from tests.test_discovery_store import beatmap


class SettingsValidationTests(unittest.TestCase):
    def test_schema_defaults_are_complete_valid_and_isolated(self):
        self.assertEqual(39, len(SCHEMA))
        self.assertEqual(DEFAULTS, validate_settings({}))
        self.assertEqual(len(SCHEMA), len({item['key'] for item in SCHEMA}))
        changed = validate_settings({'quality_min_votes': 30})
        self.assertEqual(30, changed['quality_min_votes'])
        self.assertEqual(10, DEFAULTS['quality_min_votes'])

    def test_bad_types_unknown_keys_ranges_and_relations_are_rejected(self):
        bad = [{'reference_plays': True}, {'reference_plays': 5.5}, {'quality_min_rating': float('nan')},
               {'quality_min_rating': 11}, {'quality_min_votes': '20'}, {'quality_min_plays': 10**400}, {'discovery_enabled': 0},
               {'unknown': 1}, {'reference_plays': 10}, {'reference_days': 2},
               {'calibration_maps': 6}, {'profile_min_maps': 10},
               {'discovery_retry_minutes': 0}, {'consolidate_increment': .8},
               {'rank_required_maps': 30, 'reference_plays': 20}]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_settings(values)

    def test_contexts_are_nested_thread_isolated_and_default_after_exit(self):
        with settings_context({'reference_plays': 70}):
            self.assertEqual(70, get_setting('reference_plays'))
            with settings_context({'reference_plays': 40}):
                self.assertEqual(40, get_setting('reference_plays'))
            self.assertEqual(70, get_setting('reference_plays'))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(100, pool.submit(get_setting, 'reference_plays').result())
        self.assertEqual(100, get_setting('reference_plays'))

    def test_source_rating_vote_and_play_filters_follow_the_current_preferences(self):
        item = beatmap()
        self.assertTrue(candidate_quality_ok(item))
        for values in ({'quality_min_rating': 9.5}, {'quality_min_votes': 26}, {'quality_min_plays': 25001}):
            with self.subTest(values=values), settings_context(values):
                self.assertFalse(candidate_quality_ok(item))
        item['popularity'].update(rating=7.5, votes=5, rating_votes=5, play_count=500)
        self.assertFalse(candidate_quality_ok(item))
        with settings_context({'quality_min_rating': 7, 'quality_min_votes': 5, 'quality_min_plays': 500}):
            self.assertTrue(candidate_quality_ok(item))
            source = beatmapset(ratings=[0]*7+[5,0,0,0], play_count=500)
            self.assertEqual(1, len(parse_candidates(source, 4, 5)))
        self.assertFalse(candidate_quality_ok(item))


class SettingsAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.args = argparse.Namespace(data_dir=self.temp.name, demo=False, maps=None, no_tosu=True,
                                       tosu_url='http://127.0.0.1:24050/json/v2')
        self.coach = Coach(self.args)

    def tearDown(self):
        self.coach.close()
        if self.coach.discovery_store.thread:
            self.coach.discovery_store.thread.join(3)
        self.coach.db.close()
        self.temp.cleanup()

    def test_partial_save_reopen_and_reset_keep_other_preferences(self):
        self.coach.update_settings({'reference_plays': 60, 'quality_min_rating': 9.2})
        self.coach.update_settings({'quality_min_votes': 40})
        self.assertEqual(60, self.coach.state()['settings']['values']['reference_plays'])
        self.coach.close(); self.coach.db.close()
        self.coach = Coach(self.args)
        self.assertEqual(9.2, self.coach.settings['quality_min_rating'])
        self.assertEqual(40, self.coach.settings['quality_min_votes'])
        self.assertEqual(9.2, self.coach.state()['discovery']['quality_policy']['min_rating'])
        self.coach.update_settings(reset=True)
        self.assertEqual(DEFAULTS, self.coach.settings)

    def test_failed_validation_or_persistence_leaves_settings_and_file_unchanged(self):
        before = Path(self.temp.name, 'config.json').read_bytes()
        with self.assertRaises(ValueError): self.coach.update_settings({'quality_min_rating': -1})
        with patch('app.save_json', side_effect=OSError('cannot write')):
            with self.assertRaises(OSError): self.coach.update_settings({'quality_min_rating': 9})
        self.assertEqual(DEFAULTS, self.coach.settings)
        self.assertEqual(before, Path(self.temp.name, 'config.json').read_bytes())
        self.assertEqual(DEFAULTS, self.coach.discovery_store.settings)

    def test_old_config_preserves_initial_reference_and_adds_defaults(self):
        self.coach.close(); self.coach.db.close()
        path = Path(self.temp.name, 'config.json')
        old = json.loads(path.read_text(encoding='utf-8'))
        old.pop('settings'); old['initial_stars'] = 4.2
        path.write_text(json.dumps(old), encoding='utf-8')
        self.coach = Coach(self.args)
        self.assertEqual(4.2, self.coach.settings['initial_stars'])
        self.assertEqual(4.2, self.coach.state()['profile']['baseline'])
        self.assertEqual(10000, self.coach.settings['quality_min_plays'])

    def test_settings_endpoint_requires_token_origin_and_valid_body(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.coach = self.coach
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/settings'
        def send(body, token=True, origin=None):
            headers = {'Content-Type': 'application/json'}
            if token: headers['X-Coach-Token'] = self.coach.token
            if origin: headers['Origin'] = origin
            return urlopen(Request(url, data=json.dumps(body).encode(), headers=headers), timeout=3)
        try:
            for body, token, origin, status in [({'values': {}},False,None,403),
                    ({'values': {}},True,'https://outside.example',403),
                    ({'values': {'quality_min_rating': 15}},True,None,400),
                    ({'reset': True,'values': {}},True,None,400), ({'unexpected': 1},True,None,400)]:
                with self.subTest(body=body, token=token, origin=origin), self.assertRaises(HTTPError) as caught:
                    send(body, token, origin)
                self.assertEqual(status, caught.exception.code)
            with send({'values': {'quality_min_rating': 9}}) as response:
                result=json.load(response)
            self.assertTrue(result['ok']); self.assertEqual(9, result['settings']['values']['quality_min_rating'])
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


class SettingsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.fetch=Mock(return_value=[beatmap()])
        self.store=DiscoveryStore(self.temp.name, fetcher=self.fetch)

    def tearDown(self):
        self.store.stop.set()
        if self.store.thread: self.store.thread.join(3)
        self.temp.cleanup()

    def test_custom_filters_apply_to_cache_and_background_fetch_and_survive_restart(self):
        values=validate_settings({'quality_min_rating': 9.5})
        self.store.set_settings(values); self.store.maps=[beatmap()]
        self.assertEqual([],self.store.candidates([]))
        self.assertTrue(self.store.sync(4.5)); self.store.thread.join(3)
        self.assertEqual([],self.store.maps)
        reopened=DiscoveryStore(self.temp.name,settings=values)
        self.assertEqual(9.5,reopened.snapshot([])['quality_policy']['min_rating'])
        self.assertEqual(self.store.fetched_epoch,reopened.fetched_epoch)

    def test_automatic_toggle_allows_explicit_manual_search(self):
        self.store.set_settings(validate_settings({'discovery_enabled':False}))
        self.assertFalse(self.store.sync(4.5))
        self.assertFalse(self.store.snapshot([])['automatic'])
        self.assertIsNone(self.store.snapshot([])['next_retry'])
        self.assertTrue(self.store.sync(4.5,force=True)); self.store.thread.join(3)
        self.fetch.assert_called_once()

    def test_configured_daily_and_missing_map_intervals(self):
        now=2000000000
        self.store.set_settings(validate_settings({'discovery_interval_hours':2,'discovery_retry_minutes':4}))
        batch=Mock(return_value={'maps':[beatmap()], 'next_cursor':{'page':2}, 'exhausted':False})
        self.store.batch_fetcher=batch
        with patch('discovery_store.time.time', return_value=now):
            self.assertTrue(self.store.sync(4.5)); self.store.thread.join(3)
        with patch('discovery_store.time.time', return_value=now+7199): self.assertFalse(self.store.sync(4.5))
        with patch('discovery_store.time.time', return_value=now+7200):
            self.assertTrue(self.store.sync(4.5,needs=[{'min_stars':4.2,'max_stars':4.7}]))
            self.store.thread.join(3)
        self.assertEqual(now+7200+240,self.store.demand_retry_at)

    def test_in_flight_old_policy_cannot_commit_after_settings_change(self):
        entered,release=threading.Event(),threading.Event()
        def fetch(*_):
            entered.set(); self.assertTrue(release.wait(3)); return [beatmap()]
        self.store.fetcher=fetch
        try:
            self.assertTrue(self.store.sync(4.5)); self.assertTrue(entered.wait(2))
            self.store.set_settings(validate_settings({'quality_min_rating':9.5}))
            self.assertFalse(self.store.sync(4.5))
            release.set(); self.store.thread.join(3)
            self.assertEqual([],self.store.maps)
            self.assertFalse(Path(self.temp.name,'discovery.json').exists())
            self.assertEqual('ready',self.store.status['state'])
        finally: release.set()


if __name__ == '__main__': unittest.main()
