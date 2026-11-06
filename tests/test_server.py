"""API and backend tests; YouTube network access is neither required nor used."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DLP = types.ModuleType('yt_dlp')
DLP.YoutubeDL = object
sys.modules.setdefault('yt_dlp', DLP)
os.environ['ALLOWED_ORIGINS'] = 'https://example.github.io'
with tempfile.TemporaryDirectory() as tmp:
    os.environ['DOWNLOAD_DIR'] = tmp
    spec = importlib.util.spec_from_file_location('mp4_backend_test', ROOT / 'backend/server.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Keep test directory around for test lifetime; separate from initial temporary context.
mod.FILES = Path(tempfile.mkdtemp(prefix='mp4-studio-test-'))


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), mod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        import shutil
        shutil.rmtree(mod.FILES, ignore_errors=True)

    def request(self, path, method='GET', payload=None, origin='https://example.github.io', **headers):
        if payload is not None:
            body = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        else:
            body = None
        if origin is not None:
            headers['Origin'] = origin
        request = Request(f'http://127.0.0.1:{self.port}{path}', method=method, data=body, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as response:
            return response.code, dict(response.headers), response.read()

    def test_url_validation(self):
        checks = [
            ('https://youtu.be/dQw4w9WgXcQ?t=1', True),
            ('https://www.youtube.com/watch?v=dQw4w9WgXcQ', True),
            ('https://youtube.com/shorts/dQw4w9WgXcQ', True),
            ('https://youtube.com/watch?v=short', False),
            ('http://youtube.com.evil.tld/watch?v=dQw4w9WgXcQ', False),
            ('https://example.com/watch?v=dQw4w9WgXcQ', False),
            ('ftp://youtube.com/watch?v=dQw4w9WgXcQ', False),
            ('https://youtube.com/playlist?list=AAAA', False),
        ]
        for url, expect in checks:
            with self.subTest(url=url):
                self.assertEqual(mod.canonical_video_url(url) is not None, expect)
        self.assertEqual(mod.canonical_video_url('https://youtu.be/dQw4w9WgXcQ?t=1'), 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')

    def test_health_and_info(self):
        s, headers, data = self.request('/api/health')
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(data)['ok'])
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://example.github.io')
        s, _, data = self.request('/api/info')
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(data)['max_video_mb'], 180)

    def test_cors(self):
        s, headers, _ = self.request('/api/info', origin='https://unapproved.example')
        self.assertEqual(s, 200)
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        s, _, _ = self.request('/api/jobs', 'OPTIONS', origin='https://unapproved.example')
        self.assertEqual(s, 403)
        s, headers, _ = self.request('/api/jobs', 'OPTIONS')
        self.assertEqual(s, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://example.github.io')

    def test_validation_and_job_progress_and_download(self):
        s, _, _ = self.request('/api/jobs', 'POST', {'url': 'https://evil.com/', 'quality': 'best'})
        self.assertEqual(s, 400)
        s, _, _ = self.request('/api/jobs', 'POST', {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'quality': '999'})
        self.assertEqual(s, 400)
        s, _, _ = self.request('/api/jobs', 'POST', {'url': 'https://youtu.be/dQw4w9WgXcQ', 'quality': '720'}, origin='https://unapproved.example')
        self.assertEqual(s, 403)
        # Fake a completed job so no external requests occur.
        import unittest.mock
        def fake_job(job_id):
            folder = mod.FILES / job_id
            folder.mkdir(parents=True)
            file = folder / 'example.mp4'
            file.write_bytes(b'FAKE_MP4_FOR_INTEGRATION_TEST')
            mod.update(job_id, status='ready', stage='MP4 ready', progress=100,
                       title='Test video', path=str(file), filename=file.name, size=file.stat().st_size)
        with unittest.mock.patch.object(mod, 'run_job', side_effect=fake_job):
            s, _, data = self.request('/api/jobs', 'POST', {'url': 'https://youtu.be/dQw4w9WgXcQ', 'quality': '720'})
            self.assertEqual(s, 202)
            job = json.loads(data)
            key = job['id']
            self.assertRegex(key, r'^[A-Za-z0-9_-]{32}$')
            self.assertNotIn('path', job)
            self.assertNotIn('url', job)
            self.assertNotIn('ip', job)
            # Worker can win race with status check.
            s, _, data = self.request('/api/jobs/' + key)
            self.assertEqual(s, 200)
            self.assertEqual(json.loads(data)['id'], key)
            threading.Event().wait(0.04)
            s, headers, data = self.request('/api/jobs/' + key + '/download')
            self.assertEqual(s, 200)
            self.assertEqual(headers['Content-Type'], 'video/mp4')
            self.assertEqual(data, b'FAKE_MP4_FOR_INTEGRATION_TEST')
            s, _, _ = self.request('/api/jobs/' + ('A' * 32))
            self.assertEqual(s, 404)

    def test_limits(self):
        self.assertIn('limit', mod.check_info({'duration': 10000}).lower())
        self.assertIn('limit', mod.check_info({'filesize': mod.MAX_BYTES + 1}).lower())
        self.assertTrue(mod.check_info({'is_live': True}))
        self.assertIsNone(mod.check_info({'duration': 30, 'filesize': 1024}))


if __name__ == '__main__':
    unittest.main()
