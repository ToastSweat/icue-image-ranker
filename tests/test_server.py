"""Isolated backend tests; no real archive images or ranking data are used."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import http.client
import threading
from urllib.parse import quote
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1kAAAAASUVORK5CYII=')
TOKEN = 'test-only-strong-token-0123456789abcdef'


def load_backend():
    path = PROJECT / 'server.py'
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location('ranker_backend', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backend = load_backend()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ranker-test-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.config = {'archive_root': str(self.archive), 'api_token': TOKEN,
                       'settle_seconds': 0, 'scan_interval_seconds': 3600,
                       'extensions': ['.png'], 'excluded_dirs': ['excluded'],
                       'exploration_fraction': .65}

    def image(self, name):
        path = self.archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        return path

    def ranker(self):
        self.assertIsNotNone(backend, 'server.py backend must exist')
        ranker = backend.Ranker(self.config, self.project)
        self.addCleanup(ranker.close)
        return ranker

    def serve(self, ranker):
        self.assertTrue(callable(getattr(backend, 'make_server', None)), 'HTTP server factory missing')
        server = backend.make_server(ranker, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.addCleanup(cleanup)
        return server

    def request(self, server, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_http_api_authenticates_reads_and_supports_bearer(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        server = self.serve(ranker)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        for path in ['/api/state', '/api/health', '/api/export', '/api/image/1']:
            status, _, body = self.request(server, 'GET', path)
            self.assertEqual(status, 401)
            self.assertNotIn(str(self.archive).encode(), body)
        status, headers, body = self.request(server, 'GET', '/api/state?token=' + TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['pair'], ranker.state()['pair'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, _, body = self.request(server, 'GET', '/api/health',
                                      headers={'Authorization':'Bearer ' + TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])
        self.assertEqual(self.request(server, 'GET', '/api/state?token=wrong')[0], 401)

    def test_scan_deduplicates_names_and_keeps_archive_read_only(self):
        originals = [self.image('one/a.png'), self.image('two/A.png'), self.image('b.png')]
        self.image('.hidden/private.png')
        self.image('excluded/private.png')
        self.image('unfinished.png.part')
        ranker = self.ranker()
        ranker.scan()
        state = ranker.state()
        self.assertEqual(state['stats']['images'], 2)
        self.assertEqual(state['stats']['votes'], 0)
        self.assertEqual(state['stats']['rated'], 0)
        self.assertEqual(state['top'], [])
        self.assertNotEqual(state['pair']['left']['id'], state['pair']['right']['id'])
        self.assertEqual([p.read_bytes() for p in originals], [PNG] * 3)
        self.assertTrue((self.project / 'data/rankings.sqlite3').is_file())

    def test_pending_pair_survives_restart_scan_and_deleted_source(self):
        for name in ['a.png', 'b.png', 'c.png']:
            self.image(name)
        ranker = self.ranker()
        ranker.scan()
        first = ranker.state()['pair']
        self.assertEqual(ranker.state()['pair'], first)
        restarted = self.ranker()
        self.assertEqual(restarted.state()['pair'], first)
        (self.archive / first['left']['name']).unlink()
        restarted.scan()
        state = restarted.state()
        self.assertEqual(state['pair'], first)
        self.assertIn('unavailable', state.get('error', ''))
        self.assertEqual(state['stats']['images'], 2)

    def test_vote_commits_elo_once_and_publishes_rated_leaderboard(self):
        for name in ['a.png', 'b.png', 'c.png']:
            self.image(name)
        ranker = self.ranker()
        ranker.scan()
        pair = ranker.state()['pair']
        self.assertTrue(callable(getattr(ranker, 'vote', None)), 'vote operation missing')
        state = ranker.vote(pair['id'], pair['left']['id'])
        self.assertEqual(state['stats']['votes'], 1)
        self.assertEqual(state['stats']['rated'], 2)
        self.assertEqual([im['rating'] for im in state['top']], [1016, 984])
        self.assertEqual([im['matches'] for im in state['top']], [1, 1])
        self.assertNotEqual(state['pair']['id'], pair['id'])
        with self.assertRaises(backend.APIError) as duplicate:
            ranker.vote(pair['id'], pair['left']['id'])
        self.assertEqual(duplicate.exception.status, 409)
        self.assertEqual(ranker.state()['stats']['votes'], 1)
        self.assertEqual(self.ranker().state(), ranker.state())

    def test_committed_vote_receipt_survives_restart_for_lost_responses(self):
        for name in ['a.png', 'b.png', 'c.png']:
            self.image(name)
        ranker = self.ranker()
        ranker.scan()
        pair = ranker.state()['pair']
        winner = pair['left']['id']
        state = ranker.vote(pair['id'], winner)
        receipt = {'pair_id': pair['id'], 'winner': winner}
        self.assertEqual(state.get('last_vote'), receipt)
        self.assertEqual(self.ranker().state().get('last_vote'), receipt)

    def test_selection_never_repeats_consecutive_unordered_pair(self):
        for name in ['a.png', 'b.png', 'c.png']:
            self.image(name)
        ranker = self.ranker()
        ranker.scan()
        previous = None
        backend.random.seed(42)
        for _ in range(24):
            pair = ranker.state()['pair']
            ids = frozenset([pair['left']['id'], pair['right']['id']])
            self.assertNotEqual(ids, previous)
            previous = ids
            ranker.vote(pair['id'], pair['left']['id'])

    def test_include_dirs_inactivate_without_erasing_ranks(self):
        for name in ['chosen/a.png', 'chosen/b.png', 'other/c.png']:
            self.image(name)
        ranker = self.ranker()
        ranker.scan()
        pair = ranker.state()['pair']
        ranker.vote(pair['id'], pair['left']['id'])
        with ranker.connection() as db:
            before = {row['name']: (row['id'], row['rating'], row['matches'])
                      for row in db.execute('SELECT * FROM images')}
        ranker.close()
        self.config['include_dirs'] = ['chosen']
        restricted = self.ranker()
        restricted.scan()
        self.assertEqual(restricted.state()['stats']['images'], 2)
        with restricted.connection() as db:
            excluded = db.execute("SELECT * FROM images WHERE name='c.png'").fetchone()
            self.assertEqual(excluded['active'], 0)
            self.assertEqual((excluded['id'], excluded['rating'], excluded['matches']), before['c.png'])
        restricted.close()
        self.config['include_dirs'] = []
        restored = self.ranker()
        restored.scan()
        self.assertEqual(restored.state()['stats']['images'], 3)
        with restored.connection() as db:
            after = {row['name']: (row['id'], row['rating'], row['matches'])
                     for row in db.execute('SELECT * FROM images')}
        self.assertEqual(after, before)

    def test_config_rejects_unsafe_include_dirs_and_weak_tokens(self):
        self.assertTrue(callable(getattr(backend, 'validate_config', None)), 'config validation missing')
        for value in [['../outside'], ['/absolute'], ['C:/outside'], ['C:relative'],
                      ['chosen/../../outside'], ['..\\outside'], ['.hidden'], 'chosen']:
            with self.subTest(include_dirs=value):
                config = dict(self.config, include_dirs=value)
                with self.assertRaises(ValueError):
                    backend.validate_config(config)
        for token in ['', 'short', 'a' * 40]:
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    backend.validate_config(dict(self.config, api_token=token))
        validated = backend.validate_config(dict(self.config, include_dirs=['chosen']))
        self.assertEqual(validated['include_dirs'], ['chosen'])
        self.assertEqual(validated['port'], 47831)

    def test_http_vote_validates_json_and_keeps_pair_on_failure(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        server = self.serve(ranker)
        pair = ranker.state()['pair']
        path = '/api/vote?token=' + TOKEN
        headers = {'Content-Type':'application/json'}
        for body in ['not-json', '[]', '{}', json.dumps({'pair_id':pair['id'], 'winner':'999'})]:
            status, _, payload = self.request(server, 'POST', path, body, headers)
            self.assertEqual(status, 400)
            self.assertIn('error', json.loads(payload))
            self.assertEqual(ranker.state()['pair'], pair)
        vote = json.dumps({'pair_id':pair['id'], 'winner':pair['left']['id']})
        self.assertEqual(self.request(server, 'POST', '/api/vote', vote, headers)[0], 401)
        status, _, payload = self.request(server, 'POST', path, vote, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['stats']['votes'], 1)
        self.assertEqual(self.request(server, 'POST', path, vote, headers)[0], 409)

    def test_http_limits_host_methods_body_and_supports_qt_cors(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        server = self.serve(ranker)
        path = '/api/state?token=' + TOKEN
        self.assertEqual(self.request(server, 'GET', path, headers={'Host':'attacker.example'})[0], 403)
        self.assertEqual(self.request(server, 'PUT', path)[0], 405)
        status, headers, body = self.request(server, 'OPTIONS', '/api/vote', headers={
            'Origin':'null', 'Access-Control-Request-Method':'POST',
            'Access-Control-Request-Headers':'authorization,content-type'})
        self.assertEqual(status, 204)
        self.assertEqual(body, b'')
        self.assertEqual(headers['Access-Control-Allow-Origin'], '*')
        self.assertIn('Authorization', headers['Access-Control-Allow-Headers'])
        self.assertNotIn('Access-Control-Allow-Credentials', headers)
        for origin in ['null', 'qrc://widget', 'file://']:
            status, headers, _ = self.request(server, 'GET', path, headers={'Origin':origin})
            self.assertEqual(status, 200)
            self.assertEqual(headers['Access-Control-Allow-Origin'], '*')
        vote_path = '/api/vote?token=' + TOKEN
        self.assertEqual(self.request(server, 'POST', vote_path, 'x' * 4097,
                                     {'Content-Type':'application/json'})[0], 413)
        self.assertEqual(self.request(server, 'POST', vote_path, '',
                                     {'Content-Type':'application/json', 'Content-Length':'-1'})[0], 400)
        self.assertEqual(self.request(server, 'POST', vote_path, '{}',
                                     {'Content-Type':'text/plain'})[0], 415)
        self.assertEqual(self.request(server, 'POST', vote_path, '',
                                     {'Transfer-Encoding':'chunked'})[0], 400)
        self.assertEqual(ranker.state()['stats']['votes'], 0)

    def test_image_endpoint_delivers_bounded_on_demand_thumbnail(self):
        from PIL import Image
        self.config['thumbnail_max_pixels'] = 128
        path = self.archive / 'large.png'
        Image.new('RGB', (3000, 1800), (30, 120, 210)).save(path)
        self.image('small.png')
        ranker = self.ranker()
        ranker.scan()
        server = self.serve(ranker)
        with ranker.connection() as db:
            image_id = str(db.execute("SELECT id FROM images WHERE name='large.png'").fetchone()[0])
        status, headers, body = self.request(server, 'GET', '/api/image/' + image_id + '?token=' + TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        import io
        with Image.open(io.BytesIO(body)) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), 128)
            self.assertGreater(min(thumbnail.size), 0)
        self.assertLess(len(body), path.stat().st_size)
        self.assertEqual(len(ranker.thumbnail_cache), 1)
        self.assertEqual(self.request(server, 'GET', '/api/image/' + image_id + '?token=' + TOKEN)[2], body)
        self.assertEqual(len(ranker.thumbnail_cache), 1)
        self.assertEqual(self.request(server, 'GET', '/api/image/../../config.json?token=' + TOKEN)[0], 404)
        self.assertEqual(self.request(server, 'GET', '/api/image/99999?token=' + TOKEN)[0], 404)

    def test_scan_never_follows_windows_junctions_or_included_project(self):
        import subprocess
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'escape.png').write_bytes(PNG)
        link = self.archive / 'junction'
        subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(outside)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.image('a.png')
        self.image('b.png')
        self.config['include_dirs'] = ['junction']
        ranker = self.ranker()
        ranker.scan()
        self.assertEqual(ranker.state()['stats']['images'], 0)
        ranker.close()
        self.config['include_dirs'] = []
        all_images = self.ranker()
        all_images.scan()
        self.assertEqual(all_images.state()['stats']['images'], 2)
        self.assertIsNone(all_images._safe_path(link / 'escape.png'))

    def test_state_queries_use_constant_counters_and_indexed_leaderboard(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        ranker.state()
        from contextlib import contextmanager
        original = ranker.connection
        queries = []
        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(queries.append)
                yield db
        ranker.connection = traced
        ranker.state()
        self.assertFalse(any('count(' in query.lower() or 'random(' in query.lower() for query in queries), queries)
        with original() as db:
            leaderboard_query = next(query for query in queries if 'matches>0' in query)
            plan = db.execute('EXPLAIN QUERY PLAN ' + leaderboard_query).fetchall()
            self.assertTrue(any('rated_leaderboard' in row['detail'] for row in plan), [tuple(row) for row in plan])

    def test_selection_mixes_leading_close_competitors_and_least_rated_newcomers(self):
        for index in range(90):
            self.image(f'{index:03d}.png')
        self.config['exploration_fraction'] = 0
        ranker = self.ranker()
        ranker.scan()
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=10,rating=900+id*10 WHERE id>2')
        backend.random.seed(1)
        pair = ranker.state()['pair']
        self.assertGreaterEqual(pair['left']['rating'], 1710)
        self.assertLessEqual(abs(pair['left']['rating'] - pair['right']['rating']), 100)
        ranker.vote(pair['id'], pair['left']['id'])
        with ranker.connection() as db:
            db.execute("DELETE FROM meta WHERE key='pair'")
        ranker.config['exploration_fraction'] = 1
        newcomers = ranker.state()['pair']
        self.assertEqual(newcomers['left']['matches'], 0)
        self.assertEqual(newcomers['right']['matches'], 0)

    def test_state_stays_responsive_during_uncommitted_scan_batch(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        pair = ranker.state()['pair']
        from contextlib import contextmanager
        original = ranker.connection
        paused, release, read_done = threading.Event(), threading.Event(), threading.Event()
        @contextmanager
        def slow_writer():
            with original() as db:
                yield db
                if threading.current_thread().name == 'scanner-test' and db.in_transaction:
                    paused.set()
                    release.wait(5)
        ranker.connection = slow_writer
        scan_thread = threading.Thread(target=ranker.scan, name='scanner-test')
        scan_thread.start()
        self.assertTrue(paused.wait(5))
        states = []
        def read():
            states.append(ranker.state())
            read_done.set()
        read_thread = threading.Thread(target=read)
        read_thread.start()
        responsive = read_done.wait(1)
        release.set()
        scan_thread.join(5)
        read_thread.join(5)
        self.assertTrue(responsive, 'state blocked behind scan writer instead of using WAL snapshot')
        self.assertEqual(states[0]['pair'], pair)
        self.assertTrue(states[0]['stats']['scanning'])

    def test_export_download_is_read_only_and_omits_private_paths_and_token(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        pair = ranker.state()['pair']
        ranker.vote(pair['id'], pair['right']['id'])
        before = ranker.state()
        server = self.serve(ranker)
        status, headers, body = self.request(server, 'GET', '/api/export?token=' + TOKEN)
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        exported = json.loads(body)
        self.assertEqual(len(exported['ratings']), 2)
        self.assertEqual(len(exported['votes']), 1)
        self.assertEqual(exported['votes'][0]['winner'], pair['right']['id'])
        self.assertNotIn(str(self.archive).encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(ranker.state(), before)

    def test_preview_serves_only_widget_runtime_not_project_secrets(self):
        widget = self.project / 'widget'
        widget.mkdir()
        (widget / 'index.html').write_text('<!DOCTYPE html><title>Fixture</title>', encoding='utf-8')
        (widget / 'runtime.js').write_text('window.fixture = true;', encoding='utf-8')
        (self.project / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')
        server = self.serve(self.ranker())
        status, headers, body = self.request(server, 'GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'Fixture', body)
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(self.request(server, 'GET', '/runtime.js')[0], 200)
        for path in ['/config.json', '/server.py', '/../config.json', '/%2e%2e/config.json',
                     '/widget/../config.json', '/runtime.js:stream']:
            self.assertEqual(self.request(server, 'GET', path)[0], 404, path)
        self.assertEqual(self.request(server, 'GET', '/runtime.js',
                                     headers={'Origin':'https://untrusted.example'})[0], 403)
        self.assertEqual(self.request(server, 'GET', '/runtime.js',
                                     headers={'Sec-Fetch-Site':'cross-site'})[0], 403)

    def test_cli_config_port_override_and_graceful_shutdown(self):
        import socket
        import time
        self.assertTrue(callable(getattr(backend, 'main', None)), 'CLI entry point missing')
        self.image('a.png')
        self.image('b.png')
        config_path = self.root / 'separate-config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        stop = threading.Event()
        results = []
        thread = threading.Thread(target=lambda:results.append(backend.main(
            ['--config',str(config_path),'--port',str(port)],
            project_dir=self.project,shutdown_event=stop)), name='cli-test')
        thread.start()
        self.addCleanup(stop.set)
        deadline = time.monotonic() + 10
        state = None
        while time.monotonic() < deadline:
            try:
                connection = http.client.HTTPConnection('127.0.0.1',port,timeout=1)
                connection.request('GET','/api/state?token=' + TOKEN)
                response = connection.getresponse()
                state = json.loads(response.read())
                connection.close()
                if state['pair']:
                    break
            except (OSError,http.client.HTTPException):
                pass
            time.sleep(.02)
        stop.set()
        thread.join(10)
        self.assertFalse(thread.is_alive(), 'main did not gracefully shut down')
        self.assertEqual(results, [0])
        self.assertIsNotNone(state)
        self.assertIsNotNone(state['pair'])
        self.assertTrue((self.project / 'data/rankings.sqlite3').is_file())
        self.assertFalse((self.root / 'data').exists())
        with socket.socket() as sock:
            sock.settimeout(1)
            self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)

    def test_concurrent_first_state_creates_exactly_one_persistent_pair(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        self.image('a.png')
        self.image('b.png')
        first = self.ranker()
        first.scan()
        second = self.ranker()
        rendezvous = threading.Barrier(2)
        local = threading.local()
        original_choice = backend.random.choice
        def slow_choice(rows):
            if not getattr(local, 'arrived', False):
                local.arrived = True
                try:
                    rendezvous.wait(.3)
                except threading.BrokenBarrierError:
                    pass
            return original_choice(rows)
        with patch.object(backend.random,'choice',side_effect=slow_choice), ThreadPoolExecutor(2) as pool:
            states = list(pool.map(lambda ranker:ranker.state(), [first,second]))
        self.assertEqual(states[0]['pair']['id'], states[1]['pair']['id'])
        with first.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pairs WHERE winner IS NULL').fetchone()[0], 1)

    def test_http_worker_pool_is_bounded_and_saturated_requests_get_503(self):
        import socket
        import time
        server = self.serve(self.ranker())
        self.assertEqual(getattr(server,'max_workers',None), 16, 'HTTP worker concurrency must be bounded')
        sockets = []
        try:
            for _ in range(server.max_workers):
                sock = socket.create_connection(('127.0.0.1',server.server_port),timeout=3)
                sock.sendall(b'GET /api/health HTTP/1.1\r\n')
                sockets.append(sock)
            deadline = time.monotonic() + 5
            while server.worker_count < server.max_workers and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(server.worker_count, server.max_workers)
            status, _, body = self.request(server,'GET','/api/health?token=' + TOKEN)
            self.assertEqual(status,503)
            self.assertNotIn(str(self.archive).encode(),body)
        finally:
            for sock in sockets:
                sock.close()

    def test_http_rejects_ambiguous_or_deep_json_without_accepting_vote(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        server = self.serve(ranker)
        pair = ranker.state()['pair']
        ambiguous = '{"pair_id":"' + pair['id'] + '","winner":"' + pair['left']['id'] + '\",\"winner\":\"' + pair['right']['id'] + '\"}'
        for body in [ambiguous, '[' * 1500 + '0' + ']' * 1500,
                     '{"pair_id":NaN,"winner":"1"}']:
            status, _, payload = self.request(server,'POST','/api/vote?token=' + TOKEN,body,
                                               {'Content-Type':'application/json'})
            self.assertEqual(status,400)
            self.assertIn('error',json.loads(payload))
            self.assertEqual(ranker.state()['pair'], pair)
            self.assertEqual(ranker.state()['stats']['votes'],0)

    def test_scan_inactivation_is_batched_not_a_catalog_wide_write_transaction(self):
        from contextlib import contextmanager
        for index in range(1025):
            self.image(f'{index:04d}.png')
        ranker = self.ranker()
        ranker.scan()
        for path in self.archive.iterdir():
            path.unlink()
        sizes = []
        original = ranker.connection
        @contextmanager
        def tracked():
            with original() as db:
                before = db.total_changes
                yield db
                sizes.append(db.total_changes - before)
        ranker.connection = tracked
        ranker.scan()
        self.assertEqual(ranker.state()['stats']['images'],0)
        self.assertLessEqual(max(sizes),1024, 'more than 512 image+counter updates in one transaction')

    def test_scan_cancellation_stops_inside_large_flat_directory(self):
        for index in range(1025):
            self.image(f'{index:04d}.png')
        ranker = self.ranker()
        original = ranker._scan_batch
        def stop_after_first_batch(rows):
            original(rows)
            if rows:
                ranker.stop.set()
        ranker._scan_batch = stop_after_first_batch
        ranker.scan()
        self.assertEqual(ranker.state()['stats']['images'],512)
        self.assertFalse(ranker.scanning)

    def test_config_rejects_non_image_extensions_and_invalid_numeric_settings(self):
        for key, values in {'extensions':[['.html'],['.mp4'],['.svg'],['.py']],
                            'port':[0,65536,True,1.5], 'settle_seconds':[-1,float('nan')],
                            'scan_interval_seconds':[0], 'exploration_fraction':[1.1],
                            'thumbnail_max_pixels':[0,9000], 'host':['0.0.0.0']}.items():
            for value in values:
                with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                    backend.validate_config(dict(self.config, **{key:value}))

    def test_thumbnail_cache_invalidates_when_deduplicated_representative_changes_path(self):
        from PIL import Image
        import time
        one = self.archive / 'one/duplicate.png'
        two = self.archive / 'two/duplicate.png'
        for path, color in [(one,(255,0,0)),(two,(0,0,255))]:
            path.parent.mkdir()
            Image.new('RGB',(1,1),color).save(path)
        self.assertEqual(one.stat().st_size,two.stat().st_size)
        timestamp = time.time_ns() - 1_000_000_000
        for path in [one,two]:
            os.utime(path,ns=(timestamp,timestamp))
        ranker = self.ranker()
        ranker.scan()
        with ranker.connection() as db:
            row = db.execute('SELECT * FROM images').fetchone()
        before, _ = ranker.image_bytes(str(row['id']))
        Path(row['path']).unlink()
        ranker.scan()
        after, _ = ranker.image_bytes(str(row['id']))
        self.assertNotEqual(before,after, 'cache returned the previous file at identical size/mtime')

    def test_invalid_cli_configuration_fails_cleanly_without_private_details(self):
        import io
        from contextlib import redirect_stderr
        path = self.root / 'invalid-config.json'
        for invalid in [[],None,'private-configuration',dict(self.config,host='0.0.0.0')]:
            path.write_text(json.dumps(invalid),encoding='utf-8')
            errors = io.StringIO()
            with redirect_stderr(errors):
                try:
                    result = backend.main(['--config',str(path),'--port','47831'],project_dir=self.project)
                except Exception as error:
                    self.fail(f'invalid configuration escaped as {type(error).__name__}')
            self.assertEqual(result,1)
            self.assertNotIn(str(self.archive),errors.getvalue())
            self.assertNotIn(TOKEN,errors.getvalue())
            self.assertNotIn('Traceback',errors.getvalue())

    def test_settle_window_includes_newly_created_files_with_preserved_old_mtime(self):
        import time
        from unittest.mock import patch
        self.config['settle_seconds'] = 120
        now = time.time()
        for name in ['a.png','b.png']:
            path = self.image(name)
            os.utime(path,(now-1000,now-1000))
        ranker = self.ranker()
        ranker.scan()
        self.assertEqual(ranker.state()['stats']['images'],0, 'recently copied file was indexed immediately')
        with patch.object(backend.time,'time',return_value=now+125):
            ranker.scan()
            state = ranker.state()
            self.assertEqual(state['stats']['images'],2)
            self.assertIsNotNone(state['pair'])
            self.assertNotIn('error',state)

    def test_scan_database_failure_is_reported_without_losing_pending_pair(self):
        self.image('a.png')
        self.image('b.png')
        ranker = self.ranker()
        ranker.scan()
        pair = ranker.state()['pair']
        with ranker.connection() as db:
            db.execute("CREATE TRIGGER fixture_scan_failure BEFORE UPDATE OF seen ON images "
                       "BEGIN SELECT RAISE(FAIL,'fixture write failure'); END")
        try:
            ranker.scan()
        except Exception as error:
            self.fail(f'scan failure escaped as {type(error).__name__}')
        state = ranker.state()
        self.assertIn('scan failed',state.get('error','').lower())
        self.assertEqual(state['pair'],pair)
        self.assertEqual(state['stats']['images'],2)
        self.assertFalse(state['stats']['scanning'])
        with ranker.connection() as db:
            db.execute('DROP TRIGGER fixture_scan_failure')
        ranker.scan()
        self.assertNotIn('error',ranker.state())

    def test_ranking_database_never_follows_output_directory_junction(self):
        import subprocess
        outside = self.root / 'outside-output'
        outside.mkdir()
        subprocess.run(['cmd.exe','/c','mklink','/J',str(self.project / 'data'),str(outside)],
                       check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        with self.assertRaises(ValueError):
            backend.Ranker(self.config,self.project)
        self.assertEqual(list(outside.iterdir()),[])

    def test_graceful_server_close_interrupts_incomplete_client_requests(self):
        import socket
        import time
        server = self.serve(self.ranker())
        with socket.create_connection(('127.0.0.1',server.server_port),timeout=3) as client:
            client.sendall(b'GET /api/health HTTP/1.1\r\n')
            deadline = time.monotonic()+3
            while server.worker_count == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            started = time.monotonic()
            server.shutdown()
            server.server_close()
            elapsed = time.monotonic()-started
        self.assertLess(elapsed,2, 'shutdown waited for an incomplete request timeout')
        self.assertEqual(server.worker_count,0)


if __name__ == '__main__':
    unittest.main()
