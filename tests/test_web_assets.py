"""HTTP checks for the packaged frontend and its local-only asset boundary."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import threading
import unittest
from osu_coach.app import Handler


class WebAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def get(self, path, host=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request('GET', path, headers={'Host': host} if host else {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_browser_can_load_all_frontend_resources_with_correct_types(self):
        for path, content_type in [('/', 'text/html'), ('/assets/styles.css', 'text/css'),
                                   ('/assets/compact.css', 'text/css'), ('/assets/app.js', 'text/javascript'),
                                   ('/assets/profile-radar.js', 'text/javascript')]:
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers['Content-Type'].startswith(content_type))
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertGreater(len(body), 100)

    def test_assets_never_expose_runtime_or_arbitrary_files(self):
        for path in ['/assets/../app.py', '/assets/%2e%2e/app.py', '/assets/..%5capp.py',
                     '/assets/../../data/live/config.json', '/assets/config.json',
                     '/assets//app.js', '/assets/', '/data/live/coach.sqlite3']:
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_external_host_is_rejected_even_for_public_assets(self):
        self.assertEqual(self.get('/assets/app.js', 'example.org')[0], 403)

    def test_query_string_does_not_break_loading_an_allowed_asset(self):
        status, _, body = self.get('/assets/profile-radar.js?v=2')
        self.assertEqual(status, 200)
        self.assertIn(b'buildRadarModel', body)
