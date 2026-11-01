"""Recovery regressions use generated media and disposable databases only."""
import hashlib
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
import pyvips

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
TOKEN = 'recovery-test-only-token-0123456789abcdef'
spec = importlib.util.spec_from_file_location('recovery_backend', PROJECT / 'server.py')
assert spec is not None and spec.loader is not None
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ranker-recovery-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive, self.project = self.root / 'archive', self.root / 'project'
        self.archive.mkdir()
        self.project.mkdir()
        self.config = {'archive_root':str(self.archive), 'api_token':TOKEN,
                       'settle_seconds':0, 'extensions':['.png', '.jpg', '.bmp'],
                       'thumbnail_max_pixels':128, 'exploration_fraction':1}

    def image(self, name, size=(32, 16), format=None):
        path = self.archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', size, (20, 120, 220)).save(path, format=format)
        return path

    def ranker(self, *, scan=True):
        ranker = backend.Ranker(self.config, self.project)
        self.addCleanup(ranker.close)
        if scan:
            ranker.scan()
        return ranker

    def image_id(self, ranker, name):
        with ranker.connection() as db:
            return str(db.execute('SELECT id FROM images WHERE name=?', (name,)).fetchone()[0])

    def serve(self, ranker):
        server = backend.make_server(ranker, 0)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def cleanup():
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.addCleanup(cleanup)
        return server

    def request(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def snapshot(self, ranker):
        return json.loads(b''.join(ranker.export_chunks()))

    def pair_ids(self, pair):
        return {pair['left']['id'], pair['right']['id']}

    def test_mislabeled_bmp_preview_preserves_hardlinked_source_and_pending_pair(self):
        path = self.image('bmp-inside.jpg', (672, 1000), 'BMP')
        linked = self.archive / 'linked.bmp'
        os.link(path, linked)
        originals = [hashlib.sha256(item.read_bytes()).hexdigest() for item in (path, linked)]
        ranker = self.ranker()
        before = ranker.state()
        limit = Image.MAX_IMAGE_PIXELS
        server = self.serve(ranker)
        status, headers, body = self.request(server, 'GET', '/api/image/' +
            self.image_id(ranker, path.name) + '?token=' + TOKEN)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        with Image.open(io.BytesIO(body)) as preview:
            self.assertLessEqual(max(preview.size), 128)
            self.assertAlmostEqual(preview.width / preview.height, 672 / 1000, delta=.02)
        self.assertEqual([hashlib.sha256(item.read_bytes()).hexdigest() for item in (path, linked)], originals)
        self.assertEqual(ranker.state(), before)
        self.assertEqual(Image.MAX_IMAGE_PIXELS, limit)

    def test_explicit_http_skip_advances_without_ratings_votes_or_history_loss(self):
        for name in ('a.png', 'b.png', 'c.png'):
            self.image(name)
        ranker = self.ranker()
        voted = ranker.state()['pair']
        before = ranker.vote(voted['id'], voted['left']['id'])
        pair = before['pair']
        exported = self.snapshot(ranker)
        server = self.serve(ranker)
        payload = json.dumps({'pair_id':pair['id'], 'failed_ids':[]})
        status, _, raw = self.request(server, 'POST', '/api/skip?token=' + TOKEN,
                                     payload, {'Content-Type':'application/json'})
        self.assertEqual(status, 200, raw)
        state = json.loads(raw)
        self.assertNotEqual(state['pair']['id'], pair['id'])
        self.assertNotEqual(self.pair_ids(state['pair']), self.pair_ids(pair))
        self.assertEqual(state['stats'], before['stats'])
        self.assertEqual(state['top'], before['top'])
        self.assertEqual(state['last_vote'], before['last_vote'])
        self.assertEqual(state['last_skip'], {'pair_id':pair['id'], 'failed_ids':[]})
        self.assertEqual(self.snapshot(ranker)['ratings'], exported['ratings'])
        self.assertEqual(self.snapshot(ranker)['votes'], exported['votes'])
        restarted = self.ranker()
        self.assertEqual(restarted.state(), state)
        with ranker.connection() as db:
            logged = db.execute('SELECT * FROM pair_skips WHERE pair_id=?', (pair['id'],)).fetchone()
            self.assertEqual(json.loads(logged['failed_ids']), [])
            retired = db.execute('SELECT * FROM pairs WHERE id=?', (pair['id'],)).fetchone()
            self.assertIsNone(retired['winner'])
            self.assertIsNone(retired['voted'])
        self.assertEqual(self.request(server, 'POST', '/api/skip?token=' + TOKEN,
            payload, {'Content-Type':'application/json'})[0], 409)
        with self.assertRaises(backend.APIError) as error:
            ranker.vote(pair['id'], pair['left']['id'])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(ranker.state(), state)

    def test_confirmed_corrupt_jpeg_skip_quarantines_without_catalog_or_rating_mutation(self):
        for name in ('a.jpg', 'b.jpg', 'c.jpg', 'd.jpg'):
            self.image(name)
        ranker = self.ranker()
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=2,rating=1000+id')
        pair = ranker.state()['pair']
        failed = pair['left']['id']
        path = self.archive / pair['left']['name']
        path.write_bytes(b'\xff\xd8\xff\xe0corrupt jpeg fixture')
        os.utime(path, (1_600_000_000, 1_600_000_000))
        ranker.scan()
        before = self.snapshot(ranker)
        for _ in range(3):
            self.assertEqual(ranker.state()['pair'], pair)
            with self.assertRaises(backend.APIError) as error:
                ranker.image_bytes(failed)
            self.assertEqual(error.exception.status, 415)
        state = ranker.skip(pair['id'], [failed])
        self.assertNotIn(failed, self.pair_ids(state['pair']))
        self.assertNotIn(failed, {image['id'] for image in state['top']})
        after = self.snapshot(ranker)
        self.assertEqual(after['ratings'], before['ratings'])
        self.assertEqual(after['votes'], before['votes'])
        with ranker.connection() as db:
            quarantined = db.execute('SELECT * FROM media_failures WHERE image_id=?', (failed,)).fetchone()
            self.assertEqual(quarantined['reason'], 415)
            self.assertEqual(quarantined['size'], path.stat().st_size)
            self.assertEqual(quarantined['mtime'], path.stat().st_mtime_ns)
        restarted = self.ranker()
        self.assertEqual(restarted.state(), state)
        for fraction in (0, 1):
            ranker.config['exploration_fraction'] = fraction
            for _ in range(4):
                pending = ranker.state()['pair']
                self.assertNotIn(failed, self.pair_ids(pending))
                ranker.skip(pending['id'], [])

    def test_http_pair_status_reconciles_older_votes_and_skips_after_latest_receipt_changes(self):
        for name in ('a.png', 'b.png', 'c.png', 'd.png'):
            self.image(name)
        ranker = self.ranker()
        server = self.serve(ranker)
        first = ranker.state()['pair']
        route = '/api/state?token=' + TOKEN + '&pair_id='
        status, _, raw = self.request(server, 'GET', route + first['id'])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw).get('pair_status'), {'id':first['id'], 'status':'pending'})
        self.assertIsNone(json.loads(raw)['last_skip'])
        other = self.ranker()
        second = other.vote(first['id'], first['left']['id'])['pair']
        third = other.skip(second['id'], [])['pair']
        fourth = other.vote(third['id'], third['left']['id'])['pair']
        pending = other.skip(fourth['id'], [])['pair']
        self.assertNotIn('pair_status', ranker.state())
        for pair_id, expected in ((first['id'], 'voted'), (second['id'], 'skipped'),
                                  (third['id'], 'voted'), (fourth['id'], 'skipped'),
                                  (pending['id'], 'pending'), ('never-created', 'unknown')):
            with self.subTest(status=expected):
                status, _, raw = self.request(server, 'GET', route + pair_id)
                self.assertEqual(status, 200)
                state = json.loads(raw)
                self.assertEqual(state['pair_status'], {'id':pair_id, 'status':expected})
                self.assertEqual(state['pair'], pending)
        self.assertEqual(ranker.state(first['id'])['pair_status']['status'], 'voted')

    def test_skip_method_rejects_invalid_shapes_before_rechecks_or_writes(self):
        for name in ('a.png', 'b.png', 'c.png'):
            self.image(name)
        ranker = self.ranker()
        before = ranker.state()
        pair = before['pair']
        for pair_id, failed in ((pair['id'], [pair['left']['id']] * 2),
                                (pair['id'], list(self.pair_ids(pair)) + ['999']),
                                (pair['id'], '1'), (pair['id'], None), (pair['id'], [1]),
                                (pair['id'], [{}]), (pair['id'], ['']),
                                ('', []), (None, []), (1, []),
                                (pair['id'], ['0' + pair['left']['id']])):
            with self.subTest(pair_id=pair_id, failed=failed), patch.object(ranker, 'image_bytes',
                    side_effect=AssertionError('bad payload must not decode')):
                with self.assertRaises(backend.APIError) as error:
                    ranker.skip(pair_id, failed)
                self.assertEqual(error.exception.status, 400)
            self.assertEqual(ranker.state(), before)

    def test_export_includes_read_only_skip_audit_without_private_details(self):
        for name in ('a.png', 'b.png', 'c.png'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        failed = [pair['left']['id']]
        ranker.skip(pair['id'], failed)
        before = ranker.state()
        server = self.serve(ranker)
        status, headers, raw = self.request(server, 'GET', '/api/export?token=' + TOKEN)
        self.assertEqual(status, 200)
        exported = json.loads(raw)
        self.assertIn('skips', exported)
        self.assertEqual(len(exported['skips']), 1)
        self.assertEqual(exported['skips'][0]['pair_id'], pair['id'])
        self.assertEqual(exported['skips'][0]['failed_ids'], failed)
        self.assertIsInstance(exported['skips'][0]['created'], (int, float))
        self.assertEqual(exported['votes'], [])
        self.assertNotIn(str(self.archive).encode(), raw)
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(ranker.state(), before)

    def test_top100_is_rated_only_sorted_bounded_and_filters_quarantined_media(self):
        first = self.image('000.png', (1, 1))
        for index in range(1, 110):
            os.link(first, self.archive / f'{index:03d}.png')
        ranker = self.ranker()
        with patch.object(backend.random, 'randint', return_value=1), \
                patch.object(backend.random, 'choice', side_effect=lambda rows:rows[0]):
            pair = ranker.state()['pair']
        # Replace the link, never overwrite the shared valid fixture inode.
        broken = self.archive / pair['left']['name']
        broken.unlink()
        broken.write_bytes(b'corrupt top fixture')
        os.utime(broken, (1_600_000_000, 1_600_000_000))
        ranker.scan()
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=2,rating=900+(id%7) WHERE id<=107')
            db.execute('UPDATE images SET active=0 WHERE id=107')
        failed = pair['left']['id']
        state = ranker.skip(pair['id'], [failed])
        expected = sorted([row for row in self.snapshot(ranker)['ratings']
                           if row['active'] and row['matches'] > 0 and row['id'] != failed],
                          key=lambda row:(-row['rating'], int(row['id'])))[:100]
        self.assertEqual(len(state['top']), 100)
        self.assertEqual([row['id'] for row in state['top']], [row['id'] for row in expected])
        self.assertTrue(all(row['matches'] > 0 for row in state['top']))
        self.assertNotIn(failed, {row['id'] for row in state['top']})

    def test_missing_source_only_recovers_after_explicit_skip_and_repaired_metadata_rescan(self):
        for name in ('a.png', 'b.png', 'c.png', 'd.png'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        failed = pair['left']['id']
        path = self.archive / pair['left']['name']
        original_mtime = path.stat().st_mtime_ns
        path.unlink()
        for _ in range(3):
            pending = ranker.state(pair['id'])
            self.assertEqual(pending['pair'], pair)
            self.assertEqual(pending['pair_status']['status'], 'pending')
            self.assertIn('unavailable', pending['error'])
        with self.assertRaises(backend.APIError) as error:
            ranker.image_bytes(failed)
        self.assertEqual(error.exception.status, 410)
        after = ranker.skip(pair['id'], [failed])
        self.assertNotIn(failed, self.pair_ids(after['pair']))
        with ranker.connection() as db:
            old_failure = dict(db.execute('SELECT * FROM media_failures WHERE image_id=?', (failed,)).fetchone())
            self.assertEqual(old_failure['reason'], 410)
        self.image(path.name)
        os.utime(path, ns=(original_mtime - 1_000_000_000, original_mtime - 1_000_000_000))
        ranker.scan()
        self.assertEqual(ranker.state()['pair'], after['pair'])
        with patch.object(backend.random, 'randint', return_value=int(failed)), \
                patch.object(backend.random, 'choice', side_effect=lambda rows:rows[0]):
            repaired = ranker.skip(after['pair']['id'], [])['pair']
        self.assertIn(failed, self.pair_ids(repaired))
        self.assertEqual(ranker.image_bytes(failed)[1], 'image/jpeg')
        with ranker.connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM media_failures WHERE image_id=?', (failed,)).fetchone()), old_failure)

    def test_empty_failed_report_still_excludes_a_confirmed_missing_pair_source(self):
        for name in ('a.png', 'b.png', 'c.png', 'd.png'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        failed = pair['right']['id']
        (self.archive / pair['right']['name']).unlink()
        result = ranker.skip(pair['id'], [])
        with ranker.connection() as db:
            failure = db.execute('SELECT reason FROM media_failures WHERE image_id=?', (failed,)).fetchone()
        self.assertIsNotNone(failure, 'cached clients may have no failed IDs but missing media must not recur')
        self.assertEqual(failure['reason'], 410)
        self.assertNotIn(failed, self.pair_ids(result['pair']))
        self.assertEqual(result['stats']['votes'], 0)

    def test_browser_failure_reports_recheck_valid_media_without_quarantining_it(self):
        for name in ('a.png', 'b.png', 'c.png'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        before = self.snapshot(ranker)
        decoded = []
        original = ranker.image_bytes
        def recheck(image_id):
            # A real independent SQLite writer can acquire the lock during decoding.
            with ranker.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('SELECT 1')
            decoded.append(image_id)
            return original(image_id)
        failed = [pair['left']['id'], pair['right']['id']]
        with patch.object(ranker, 'image_bytes', side_effect=recheck):
            state = ranker.skip(pair['id'], failed)
        self.assertEqual(decoded, failed)
        self.assertEqual(state['last_skip'], {'pair_id':pair['id'], 'failed_ids':failed})
        self.assertEqual(self.snapshot(ranker)['ratings'], before['ratings'])
        with ranker.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media_failures').fetchone()[0], 0)

    def test_transient_recheck_errors_do_not_quarantine_media_or_prevent_explicit_skip(self):
        for name in ('a.png', 'b.png', 'c.png', 'd.png'):
            self.image(name)
        ranker = self.ranker()
        for code in (401, 429, 500, 503):
            pair = ranker.state()['pair']
            with self.subTest(code=code), patch.object(ranker, 'image_bytes',
                    side_effect=backend.APIError(code, 'fixture transient')):
                state = ranker.skip(pair['id'], [pair['left']['id']])
            self.assertNotEqual(state['pair']['id'], pair['id'])
            self.assertEqual(state['stats']['votes'], 0)
            with ranker.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM media_failures').fetchone()[0], 0)

    def test_two_confirmed_failures_exclude_both_members_and_can_leave_no_pair(self):
        for name in ('a.jpg', 'b.jpg'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        left = self.archive / pair['left']['name']
        right = self.archive / pair['right']['name']
        left.write_bytes(b'\xff\xd8\xff\xe0corrupt jpeg fixture')
        os.utime(left, (1_600_000_000, 1_600_000_000))
        ranker.scan()
        right.unlink()
        state = ranker.skip(pair['id'], [pair['left']['id'], pair['right']['id']])
        self.assertIsNone(state['pair'])
        self.assertEqual(state['stats']['votes'], 0)
        self.assertEqual(ranker.state(pair['id'])['pair_status']['status'], 'skipped')
        with ranker.connection() as db:
            self.assertEqual({row['reason'] for row in db.execute('SELECT reason FROM media_failures')}, {410, 415})
        self.assertEqual(self.ranker(scan=False).state(), state)

    def test_source_budget_failure_is_quarantined_but_does_not_touch_ratings(self):
        for name in ('a.png', 'b.png', 'c.png', 'd.png'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        self.image(pair['left']['name'], (100, 100))
        os.utime(self.archive / pair['left']['name'], (1_600_000_000, 1_600_000_000))
        ranker.scan()
        ranker.config['max_source_pixels'] = 512
        before = self.snapshot(ranker)
        state = ranker.skip(pair['id'], [pair['left']['id']])
        self.assertNotIn(pair['left']['id'], self.pair_ids(state['pair']))
        self.assertEqual(self.snapshot(ranker)['ratings'], before['ratings'])
        with ranker.connection() as db:
            self.assertEqual(db.execute('SELECT reason FROM media_failures WHERE image_id=?',
                (pair['left']['id'],)).fetchone()[0], 413)

    def test_skip_transaction_failure_rolls_back_audit_quarantine_and_receipt(self):
        for name in ('a.jpg', 'b.jpg', 'c.jpg'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        path = self.archive / pair['left']['name']
        path.write_bytes(b'\xff\xd8\xff\xe0corrupt jpeg fixture')
        os.utime(path, (1_600_000_000, 1_600_000_000))
        ranker.scan()
        before = ranker.state()
        with ranker.connection() as db:
            db.execute("CREATE TRIGGER fixture_skip_failure BEFORE INSERT ON meta WHEN NEW.key='last_skip' "
                       "BEGIN SELECT RAISE(FAIL,'fixture skip failure'); END")
        server = self.serve(ranker)
        status, _, raw = self.request(server, 'POST', '/api/skip?token=' + TOKEN,
            json.dumps({'pair_id':pair['id'], 'failed_ids':[pair['left']['id']]}),
            {'Content-Type':'application/json'})
        self.assertEqual(status, 503, raw)
        self.assertEqual(ranker.state(), before)
        with ranker.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pair_skips').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM media_failures').fetchone()[0], 0)
            db.execute('DROP TRIGGER fixture_skip_failure')
        self.assertNotEqual(ranker.skip(pair['id'], [pair['left']['id']])['pair']['id'], pair['id'])

    def test_http_skip_preserves_auth_body_parser_limits_and_exact_payload_contract(self):
        for name in ('a.png', 'b.png', 'c.png'):
            self.image(name)
        ranker = self.ranker()
        before = ranker.state()
        pair = before['pair']
        server = self.serve(ranker)
        route = '/api/skip?token=' + TOKEN
        headers = {'Content-Type':'application/json'}
        good = json.dumps({'pair_id':pair['id'], 'failed_ids':[]})
        self.assertEqual(self.request(server, 'POST', '/api/skip', good, headers)[0], 401)
        self.assertEqual(self.request(server, 'POST', route, good,
            dict(headers, Host='untrusted.example'))[0], 403)
        malformed = ['not json', '[]', '{}', '{"pair_id":NaN,"failed_ids":[]}',
                     '[' * 1500 + '0' + ']' * 1500,
                     '{"pair_id":"' + pair['id'] + '","failed_ids":[],"failed_ids":[]}']
        malformed += [json.dumps(payload) for payload in (
            {'pair_id':pair['id'], 'winner':pair['left']['id']},
            {'pair_id':pair['id'], 'failed_ids':[], 'extra':True},
            {'pair_id':pair['id'], 'failed_ids':['999']},
            {'pair_id':pair['id'], 'failed_ids':[pair['left']['id']] * 2},
            {'pair_id':pair['id'], 'failed_ids':list(self.pair_ids(pair)) + ['999']},
            {'pair_id':pair['id'], 'failed_ids':[{}]},
            {'pair_id':pair['id'], 'failed_ids':[1]},
            {'pair_id':pair['id'], 'failed_ids':None},
            {'pair_id':pair['id'], 'failed_ids':'1'},
            {'pair_id':None, 'failed_ids':[]}, {'pair_id':'', 'failed_ids':[]})]
        for body in malformed:
            with self.subTest(body=body[:100]):
                status, _, raw = self.request(server, 'POST', route, body, headers)
                self.assertEqual(status, 400, raw)
                self.assertEqual(ranker.state(), before)
        for body, custom_headers, expected in (
                ('x' * 4097, headers, 413), ('{}', {'Content-Type':'text/plain'}, 415),
                ('', dict(headers, **{'Content-Length':'-1'}), 400),
                ('', dict(headers, **{'Transfer-Encoding':'chunked'}), 400)):
            self.assertEqual(self.request(server, 'POST', route, body, custom_headers)[0], expected)
        for length_headers, expected in (([], 411), ([('Content-Length','0'), ('Content-Length','0')], 400)):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.putrequest('POST', route)
                connection.putheader('Content-Type', 'application/json')
                for key, value in length_headers:
                    connection.putheader(key, value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
            finally:
                connection.close()
        self.assertEqual(self.request(server, 'GET', '/api/state?token=' + TOKEN + '&pair_id=')[0], 400)
        self.assertEqual(self.request(server, 'GET', '/api/state?token=' + TOKEN + '&pair_id=a&pair_id=b')[0], 400)
        self.assertEqual(ranker.state(), before)
        status, _, raw = self.request(server, 'POST', '/api/skip', good,
            dict(headers, Authorization='Bearer ' + TOKEN))
        self.assertEqual(status, 200, raw)
        accepted = ranker.state()
        with patch.object(ranker, 'image_bytes', side_effect=AssertionError('stale skip must not decode')):
            for pair_id in (pair['id'], 'not-current'):
                payload = json.dumps({'pair_id':pair_id, 'failed_ids':[pair['left']['id']]})
                self.assertEqual(self.request(server, 'POST', route, payload, headers)[0], 409)
        self.assertEqual(ranker.state(), accepted)

    def test_concurrent_skip_instances_commit_exactly_one_audit_and_next_pair(self):
        for name in ('a.png', 'b.png', 'c.png', 'd.png'):
            self.image(name)
        first = self.ranker()
        pair = first.state()['pair']
        second = self.ranker(scan=False)
        rendezvous = threading.Barrier(2)
        def recheck(ranker):
            original = ranker.image_bytes
            def synchronized(image_id):
                result = original(image_id)
                rendezvous.wait(5)
                return result
            return synchronized
        def skip(ranker):
            try:
                return ranker.skip(pair['id'], [pair['left']['id']])
            except backend.APIError as error:
                return error.status
        with patch.object(first, 'image_bytes', side_effect=recheck(first)), \
                patch.object(second, 'image_bytes', side_effect=recheck(second)), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(skip, (first, second)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn(409, results)
        with first.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pair_skips').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM pairs').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM media_failures').fetchone()[0], 0)
        self.assertEqual(first.state(), second.state())

    def test_vote_from_other_instance_during_recheck_makes_skip_stale_without_quarantine(self):
        for name in ('a.png', 'b.png', 'c.png'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        other = self.ranker(scan=False)
        def concurrently_voted(image_id):
            other.vote(pair['id'], pair['right']['id'])
            raise backend.APIError(415, 'fixture recheck failure')
        with patch.object(ranker, 'image_bytes', side_effect=concurrently_voted):
            with self.assertRaises(backend.APIError) as error:
                ranker.skip(pair['id'], [pair['left']['id']])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(ranker.state()['stats']['votes'], 1)
        self.assertIsNone(ranker.state()['last_skip'])
        with ranker.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media_failures').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM pair_skips').fetchone()[0], 0)

    def test_repaired_metadata_during_recheck_is_not_quarantined_as_old_failure(self):
        for name in ('a.png', 'b.png', 'c.png'):
            self.image(name)
        ranker = self.ranker()
        pair = ranker.state()['pair']
        other = self.ranker(scan=False)
        def concurrently_repaired(image_id):
            self.image(pair['left']['name'], (64, 64))
            os.utime(self.archive / pair['left']['name'], (1_600_000_000, 1_600_000_000))
            other.scan()
            raise backend.APIError(415, 'fixture old fingerprint failure')
        with patch.object(ranker, 'image_bytes', side_effect=concurrently_repaired):
            ranker.skip(pair['id'], [pair['left']['id']])
        with ranker.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media_failures').fetchone()[0], 0)
        self.assertEqual(ranker.image_bytes(pair['left']['id'])[1], 'image/jpeg')

    def test_selection_leaderboard_and_pair_receipts_keep_indexed_bounded_query_plans(self):
        for index in range(90):
            self.image(f'{index:03d}.png', (1, 1))
        ranker = self.ranker()
        original = ranker.connection
        queries = []
        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(queries.append)
                yield db
        ranker.connection = traced
        for fraction in (0, 1):
            ranker.config['exploration_fraction'] = fraction
            pair = ranker.state()['pair']
            ranker.skip(pair['id'], [])
            ranker.state(pair['id'])
        selectors = [query for query in queries if 'FROM images INDEXED BY' in query]
        self.assertTrue(selectors)
        self.assertFalse(any('count(' in query.lower() or 'random(' in query.lower() for query in queries))
        with original() as db:
            for query in selectors:
                self.assertIn('NOT EXISTS', query)
                self.assertIn('LIMIT', query)
                plan = [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN ' + query)]
                self.assertFalse(any('TEMP B-TREE' in item for item in plan), plan)
                self.assertTrue(any('INDEX' in item and 'images' in item for item in plan), plan)
                self.assertTrue(any('media_failures USING INTEGER PRIMARY KEY' in item for item in plan), plan)
            top = next(query for query in selectors if 'rated_leaderboard' in query)
            self.assertIn('LIMIT 100', top)
            receipt_queries = [query for query in queries if query.startswith('SELECT winner FROM pairs')
                               or query.startswith('SELECT 1 FROM pair_skips')]
            self.assertTrue(receipt_queries)
            for query in receipt_queries:
                plan = [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN ' + query)]
                self.assertTrue(any('SEARCH' in item and 'INDEX' in item for item in plan), plan)

    def test_bmp_fallback_preserves_configured_byte_pixel_and_source_change_guards(self):
        path = self.image('bmp-inside.jpg', (672, 1000), 'BMP')
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        ranker = self.ranker()
        image_id = self.image_id(ranker, path.name)
        original_stat = path.stat()
        for key, value in (('max_source_bytes', original_stat.st_size - 1),
                           ('max_source_pixels', 672 * 1000 - 1)):
            with self.subTest(key=key), patch.dict(ranker.config, {key:value}):
                with self.assertRaises(backend.APIError) as error:
                    ranker.image_bytes(image_id)
                self.assertEqual(error.exception.status, 413)
        os.utime(path, ns=(original_stat.st_mtime_ns - 1_000_000_000,
                           original_stat.st_mtime_ns - 1_000_000_000))
        with self.assertRaises(backend.APIError) as error:
            ranker.image_bytes(image_id)
        self.assertEqual(error.exception.status, 410)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
        self.assertEqual(len(ranker.thumbnail_cache), 0)

    def test_failed_decode_releases_source_handle_even_when_error_is_retained(self):
        path = self.archive / 'corrupt.jpg'
        path.write_bytes(b'\xff\xd8\xff\xe0corrupt jpeg fixture')
        ranker = self.ranker()
        with self.assertRaises(backend.APIError) as retained:
            ranker.image_bytes(self.image_id(ranker, path.name))
        self.assertEqual(retained.exception.status, 415)
        path.unlink()
        self.assertFalse(path.exists())

    def test_vips_decode_failure_fallback_refuses_large_bytes_and_pixels_before_load(self):
        oversized = self.image('padded.bmp', (2, 2), 'BMP')
        with oversized.open('ab') as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        large = self.archive / 'large.png'
        pyvips.Image.black(6500, 6500, bands=3).pngsave(str(large))
        ranker = self.ranker()
        before = ranker.state()
        limit = Image.MAX_IMAGE_PIXELS
        with patch.object(backend.pyvips.Source, 'new_from_descriptor', side_effect=pyvips.Error('unsupported')):
            for name in (oversized.name, large.name):
                with self.subTest(name=name), patch.object(Image.Image, 'load',
                        side_effect=AssertionError('large fallback must not load pixels')):
                    with self.assertRaises(backend.APIError) as error:
                        ranker.image_bytes(self.image_id(ranker, name))
                    self.assertEqual(error.exception.status, 413)
        self.assertEqual(Image.MAX_IMAGE_PIXELS, limit)
        self.assertEqual(ranker.state(), before)


if __name__ == '__main__':
    unittest.main()
