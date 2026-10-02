import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from daydreamer_agent.domain.errors import ProviderError
from daydreamer_agent.web.server import Server
from daydreamer_agent.web.store import Store
from daydreamer_agent.web.worker import process


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'web').mkdir()
        (self.root / 'web/index.html').write_text('hello')
        (self.root / 'outputs').mkdir()
        self.code = 'test-only-connection-code-123456'
        self.server = Server(('127.0.0.1', 0), self.root / 'data', self.root / 'web', self.root, self.code)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        extra = {'Cookie': self.cookie} if self.cookie else {}
        conn.request(method, path, body, extra | (headers or {}))
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def login(self):
        status, headers, _ = self.request('POST', '/api/session', json.dumps({'code': self.code}), {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def upload(self, key='test-upload-1234567'):
        status, _, body = self.request('POST', '/api/jobs', b'fake-video-for-api-test', {'Content-Type': 'video/mp4', 'Idempotency-Key': key})
        return status, json.loads(body)

    def test_private_binding_csrf_and_static_traversal(self):
        self.assertEqual(self.request('GET', '/api/jobs')[0], 401)
        status, headers, body = self.request('GET', '/')
        self.assertEqual((status, body), (200, b'hello'))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('GET', '/api/jobs/' + '0' * 32 + '/video')[0], 404)
        self.login()
        self.assertEqual(self.request('GET', '/api/jobs')[0], 200)
        self.assertEqual(self.request('POST', '/api/jobs', b'x', {'Origin': 'https://attacker.invalid'})[0], 403)
        self.assertEqual(self.request('GET', '/%2e%2e/config.js')[0], 404)
        self.assertEqual(self.request('GET', '/.env')[0], 404)
        self.cookie = None
        self.assertEqual(self.request('POST', '/api/session', json.dumps({'code': self.code}), {'Content-Type': 'application/json'})[0], 409)
        self.assertEqual(self.request('GET', '/api/jobs', headers={'Authorization': 'Bearer ' + self.code})[0], 401)

    def test_upload_idempotency_queue_range_and_played(self):
        self.login()
        status, job = self.upload()
        self.assertEqual(status, 202)
        self.assertEqual(job['state'], 'queued')
        self.assertNotIn('source', job)
        again, repeated = self.upload()
        self.assertEqual(again, 200)
        self.assertEqual(repeated['id'], job['id'])
        store = self.server.store
        self.assertEqual(store.claim()['id'], job['id'])
        self.assertIsNone(store.claim())
        video = self.root / 'outputs/test.mp4'
        video.write_bytes(b'0123456789')
        store.update(job['id'], state='ready', video=str(video))
        path = '/api/jobs/' + job['id']
        status, headers, data = self.request('GET', path + '/video', headers={'Range': 'bytes=2-5'})
        self.assertEqual((status, data), (206, b'2345'))
        self.assertEqual(headers['Content-Range'], 'bytes 2-5/10')
        self.assertEqual(self.request('GET', path + '/video', headers={'Range': 'bytes=50-'})[0], 416)
        self.assertEqual(self.request('HEAD', path + '/video')[2], b'')
        self.assertEqual(self.request('POST', path + '/played')[0], 200)
        self.assertEqual(store.next()['id'], job['id'])
        self.assertEqual(self.request('POST', path + '/resume')[0], 409)

    def test_bad_upload_does_not_queue_or_trigger_model(self):
        self.login()
        self.assertEqual(self.request('POST', '/api/jobs', b'text', {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/jobs', b'123', {'Content-Type': 'video/mp4'})[0], 400)
        self.assertEqual(self.server.store.list(), [])

    def test_playback_prefers_newest_unplayed_then_random_history(self):
        store = self.server.store
        history = store.reserve('history-playback-123456', '.mp4')
        older = store.reserve('older-playback-12345678', '.mp4')
        newest = store.reserve('newest-playback-1234567', '.mp4')
        pending = store.reserve('pending-playback-123456', '.mp4')
        failed = store.reserve('failed-playback-1234567', '.mp4')
        store.update(history['id'], state='ready', played=1)
        with patch('daydreamer_agent.web.store.time.time', return_value=100):
            store.update(older['id'], state='ready')
        with patch('daydreamer_agent.web.store.time.time', return_value=200):
            store.update(newest['id'], state='ready')
        store.update(pending['id'], state='processing')
        store.update(failed['id'], state='failed')
        def next_job():
            status, _, body = self.request('GET', '/api/videos/next')
            self.assertEqual(status, 200)
            return json.loads(body)['job']
        self.assertEqual(next_job()['id'], newest['id'])
        self.assertEqual(self.request('POST', '/api/jobs/' + newest['id'] + '/played')[0], 200)
        self.assertEqual(next_job()['id'], older['id'])
        self.assertEqual(self.request('POST', '/api/jobs/' + older['id'] + '/played')[0], 200)
        expected = {history['id'], older['id'], newest['id']}
        def choose_history(rows):
            self.assertEqual({row['id'] for row in rows}, expected)
            return next(row for row in rows if row['id'] == history['id'])
        with patch('daydreamer_agent.web.store.secrets.choice', side_effect=choose_history) as choose:
            self.assertEqual(next_job()['id'], history['id'])
            choose.assert_called_once()
        fresh = store.reserve('fresh-playback-12345678', '.mp4')
        store.update(fresh['id'], state='ready')
        self.assertEqual(next_job()['id'], fresh['id'])

    def test_playback_waits_only_when_no_completed_video_exists(self):
        store = self.server.store
        self.assertIsNone(store.next())
        failed = store.reserve('failed-only-1234567890', '.mp4')
        store.update(failed['id'], state='failed')
        self.assertIsNone(store.next())
        pending = store.reserve('pending-only-123456789', '.mp4')
        store.update(pending['id'], state='queued')
        self.assertEqual(store.next()['id'], pending['id'])

    def test_history_avoids_previous_clip_without_blocking_new_or_only_clip(self):
        store = self.server.store
        first = store.reserve('history-first-123456789', '.mp4')
        store.update(first['id'], state='ready', played=1)
        def next_id(previous):
            status, _, body = self.request('GET', '/api/videos/next?previous=' + previous)
            self.assertEqual(status, 200)
            return json.loads(body)['job']['id']
        self.assertEqual(next_id(first['id']), first['id'])
        second = store.reserve('history-second-12345678', '.mp4')
        store.update(second['id'], state='ready', played=1)
        previous = first['id']
        for _ in range(6):
            selected = next_id(previous)
            self.assertNotEqual(selected, previous)
            previous = selected
        store.update(second['id'], played=0)
        self.assertEqual(next_id(second['id']), second['id'])
        self.assertEqual(self.request('GET', '/api/videos/next?previous=invalid')[0], 400)

    def test_native_uploader_is_independent_and_has_limited_permissions(self):
        token = 'native-upload-only-test-token-123456789'
        self.server.upload_token = token
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'video/mp4', 'Idempotency-Key': 'native-recording-123456'}
        status, _, body = self.request('POST', '/api/jobs', b'video', headers)
        self.assertEqual(status, 202)
        job = json.loads(body)
        with self.server.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        auth = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.request('GET', '/api/jobs/' + job['id'], headers=auth)[0], 200)
        for method, path in [('GET', '/api/jobs'), ('POST', '/api/jobs/' + job['id'] + '/resume')]:
            self.assertEqual(self.request(method, path, headers=auth)[0], 403)
        self.assertEqual(self.request('POST', '/api/jobs', b'video', headers)[0], 200)
        self.assertEqual(len(self.server.store.list()), 1)
        self.login()  # The uploader did not consume the browser's first binding.
        self.assertEqual(self.request('GET', '/api/jobs')[0], 200)

    def test_old_upload_page_redirects_to_player(self):
        status, headers, _ = self.request('GET', '/upload')
        self.assertEqual((status, headers['Location']), (303, '/'))

    def test_anonymous_playback_keeps_paid_operations_protected(self):
        store = self.server.store
        job = store.reserve('public-playback-123456', '.mp4')
        video = self.root / 'outputs/test.mp4'
        video.write_bytes(b'0123456789')
        store.update(job['id'], state='ready', video=str(video))
        base = '/api/jobs/' + job['id']
        self.assertEqual(json.loads(self.request('GET', '/api/videos/next')[2])['job']['id'], job['id'])
        self.assertEqual(self.request('GET', base)[0], 200)
        status, _, body = self.request('GET', base + '/video', headers={'Range': 'bytes=1-3'})
        self.assertEqual((status, body), (206, b'123'))
        self.assertEqual(self.request('HEAD', base + '/video')[0], 200)
        self.assertEqual(self.request('POST', base + '/played')[0], 200)
        self.assertEqual(self.request('POST', '/api/jobs', b'video', {'Content-Type': 'video/mp4'})[0], 401)
        self.assertEqual(self.request('POST', base + '/resume')[0], 401)
        self.assertEqual(self.request('GET', '/api/jobs')[0], 401)
        self.assertEqual(self.request('GET', '/connect')[1]['Location'], '/')

    def test_restart_requeues_same_run_with_bounded_recovery(self):
        store = self.server.store
        job = store.reserve('recovery-test-123456', '.mp4')
        for index in range(3):
            store.update(job['id'], state='processing', run_id='original-run')
            store.recover_worker()
        result = store.get(job['id'])
        self.assertEqual(result['state'], 'paused')
        self.assertEqual(result['run_id'], 'original-run')
        self.assertEqual(result['recoveries'], 3)

    def test_worker_failure_resume_preserves_run_and_completed_delivery(self):
        store = self.server.store
        job = store.reserve('worker-test-123456', '.mp4')
        store.update(job['id'], state='queued', run_id='saved-run')
        with patch('daydreamer_agent.web.worker.run_command', side_effect=ProviderError('network unavailable')) as run:
            process(store, store.claim(), self.root)
        self.assertEqual(run.call_args.args[0].run, 'saved-run')
        self.assertEqual(store.get(job['id'])['state'], 'failed')
        self.assertTrue(store.resume(job['id']))
        video = self.root / 'outputs/ready.mp4'
        video.write_bytes(b'video')
        with patch('daydreamer_agent.web.worker.run_command', return_value={'status': 'preview_ready', 'delivery': {'video': str(video)}}):
            process(store, store.claim(), self.root)
        self.assertEqual(store.get(job['id'])['state'], 'ready')
        self.assertEqual(store.get(job['id'])['run_id'], 'saved-run')


if __name__ == '__main__':
    unittest.main()
