"""Media 1.2 regression tests, generated files and databases only in profile scratch."""
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
import pyvips

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
TOKEN = 'media-only-strong-token-0123456789abcdef'
spec = importlib.util.spec_from_file_location('media_backend', PROJECT / 'server.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ranker-media-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'archive'
        self.project = self.root / 'project'
        self.archive.mkdir()
        self.project.mkdir()
        self.config = {'archive_root':str(self.archive), 'api_token':TOKEN,
                       'settle_seconds':0, 'extensions':['.png'], 'thumbnail_max_pixels':128}

    def ranker(self):
        ranker = backend.Ranker(self.config, self.project)
        self.addCleanup(ranker.close)
        ranker.scan()
        return ranker

    def image(self, name='small.png', size=(32, 16)):
        path = self.archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', size, (20, 120, 220)).save(path)
        return path

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

    def request(self, server, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def digest(self, path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def test_running_companion_has_exclusive_port_ownership(self):
        ranker = self.ranker()
        server = backend.make_server(ranker, 0)
        second = None
        try:
            with self.assertRaises(OSError, msg='A second companion must not steal an occupied port'):
                second = backend.make_server(ranker, server.server_port)
        finally:
            if second is not None:
                second.server_close()
            server.server_close()

    def test_oversized_hardlinked_png_preview_preserves_source_and_pending_pair(self):
        path = self.archive / 'large.png'
        pyvips.Image.black(6476, 8350, bands=4).new_from_image([40, 120, 210, 128]).pngsave(str(path))
        linked = self.archive / 'linked.png'
        os.link(path, linked)
        originals = [self.digest(path), self.digest(linked)]
        self.assertEqual(path.stat().st_ino, linked.stat().st_ino)
        ranker = self.ranker()
        before = ranker.state()
        server = self.serve(ranker)
        status, headers, body = self.request(server, 'GET', '/api/image/' +
            self.image_id(ranker, 'large.png') + '?token=' + TOKEN)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        with Image.open(io.BytesIO(body)) as preview:
            self.assertLessEqual(max(preview.size), 128)
            self.assertGreater(min(preview.size), 0)
        self.assertEqual([self.digest(path), self.digest(linked)], originals)
        self.assertEqual(ranker.state(), before)
        self.assertEqual(Image.MAX_IMAGE_PIXELS, 89478485)

    def test_source_budgets_are_configurable_and_checked_before_decode(self):
        path = self.image(size=(100, 100))
        self.config['max_source_pixels'] = 9999
        ranker = self.ranker()
        with self.assertRaises(backend.APIError) as error:
            ranker.image_bytes(self.image_id(ranker, 'small.png'))
        self.assertEqual(error.exception.status, 413)
        self.config['max_source_pixels'] = 10000
        self.config['max_source_bytes'] = path.stat().st_size - 1
        size_limited = self.ranker()
        with self.assertRaises(backend.APIError) as error:
            size_limited.image_bytes(self.image_id(size_limited, 'small.png'))
        self.assertEqual(error.exception.status, 413)
        validated = backend.validate_config(dict(self.config, max_source_bytes=1024))
        self.assertEqual(validated['max_source_pixels'], 10000)
        for key, values in {'max_source_pixels':[True, 0, 1.5, 1_000_000_001],
                            'max_source_bytes':[True, 0, 1.5, 1_073_741_825]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    backend.validate_config(dict(self.config, **{key:value}))

    def test_missing_vips_keeps_small_pillow_fallback_but_reports_large_dependency(self):
        self.image()
        path = self.archive / 'large.png'
        pyvips.Image.black(6500, 6500, bands=3).pngsave(str(path))
        ranker = self.ranker()
        original_limit = Image.MAX_IMAGE_PIXELS
        with patch.object(backend, 'pyvips', None):
            body, content_type = ranker.image_bytes(self.image_id(ranker, 'small.png'))
            self.assertEqual(content_type, 'image/jpeg')
            self.assertTrue(body.startswith(b'\xff\xd8'))
            with self.assertRaises(backend.APIError) as error:
                ranker.image_bytes(self.image_id(ranker, 'large.png'))
            self.assertEqual(error.exception.status, 503)
            self.assertIn('pyvips', str(error.exception))
        self.assertEqual(Image.MAX_IMAGE_PIXELS, original_limit)
    def video(self, name='clip.mp4', large=False):
        path = self.archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        webm = path.suffix.lower() == '.webm'
        source = 'testsrc2=s=640x360:r=30' if large else 'color=c=red:s=160x90:r=10'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', source,
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                        '-t', '1' if large else '0.4', '-threads', '1', '-c:v', 'libvpx-vp9' if webm else 'libx264',
                        '-crf', '0' if large else '23',
                        '-pix_fmt', 'yuv420p', '-c:a', 'libopus' if webm else 'aac', str(path)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        return path

    def test_video_discovery_adds_kinds_without_changing_existing_ranking_identity(self):
        self.image('one.png')
        self.image('two.png')
        ranker = self.ranker()
        pair = ranker.state()['pair']
        ranker.vote(pair['id'], pair['left']['id'])
        before = ranker.state()
        self.video('clip.mp4')
        self.video('other.webm')
        self.video('copy/CLIP.MP4')
        self.video('.hidden/private.mp4')
        self.video('image-ranker/private.mp4')
        (self.archive / 'unfinished.mp4.part').write_bytes(b'partial')
        ranker.scan()
        state = ranker.state()
        self.assertEqual(state['stats']['images'], 4)
        self.assertEqual(state['stats']['votes'], 1)
        self.assertEqual(state['pair'], before['pair'])
        with ranker.connection() as db:
            items = {row['name_key']:ranker._image(row) for row in db.execute('SELECT * FROM images WHERE active=1')}
        self.assertEqual(items['one.png']['kind'], 'image')
        self.assertTrue(items['one.png']['url'].startswith('/api/image/'))
        for name in ['clip.mp4', 'other.webm']:
            self.assertEqual(items[name]['kind'], 'video')
            self.assertTrue(items[name]['url'].startswith('/api/video/'))
            self.assertTrue(items[name]['poster_url'].startswith('/api/image/'))
        self.assertEqual(self.ranker().state(), state)

    def test_video_config_is_strict_and_disabled_scan_preserves_pending_pair(self):
        for key, values in {'include_videos':[0, 'true', None],
                            'video_extensions':[['.png'], ['.mp4.part'], ['.avi'], [], '.mp4']}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    backend.validate_config(dict(self.config, **{key:value}))
        self.video()
        self.image()
        ranker = self.ranker()
        before = ranker.state()['pair']
        video_id = self.image_id(ranker, 'clip.mp4')
        self.config['include_videos'] = False
        restricted = self.ranker()
        self.assertEqual(restricted.state()['stats']['images'], 1)
        self.assertEqual(restricted.state()['pair'], before)
        self.config['include_videos'] = True
        restored = self.ranker()
        self.assertEqual(self.image_id(restored, 'clip.mp4'), video_id)
        self.assertEqual(restored.state()['pair'], before)

    def test_video_get_streams_original_mp4_and_webm_with_authentication(self):
        paths = [self.video(), self.video('clip.webm')]
        self.image()
        ranker = self.ranker()
        server = self.serve(ranker)
        before = ranker.state()
        for path in paths:
            route = '/api/video/' + self.image_id(ranker, path.name)
            self.assertEqual(self.request(server, 'GET', route)[0], 401)
            status, headers, body = self.request(server, 'GET', route,
                {'Authorization':'Bearer ' + TOKEN})
            self.assertEqual(status, 200, body)
            self.assertEqual(body, path.read_bytes())
            self.assertEqual(headers['Content-Type'], 'video/mp4' if path.suffix == '.mp4' else 'video/webm')
            self.assertEqual(headers['Content-Length'], str(path.stat().st_size))
            self.assertEqual(headers['Accept-Ranges'], 'bytes')
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(self.request(server, 'GET', route + '?token=' + TOKEN,
                {'Host':'evil.example'})[0], 403)
        for image_id in [self.image_id(ranker, 'small.png'), '99999', '../config.json']:
            self.assertEqual(self.request(server, 'GET', '/api/video/' + image_id + '?token=' + TOKEN)[0], 404)
        self.assertEqual(ranker.state(), before)

    def test_video_byte_ranges_are_single_bounded_and_report_unsatisfiable(self):
        path = self.video()
        data = path.read_bytes()
        ranker = self.ranker()
        server = self.serve(ranker)
        route = '/api/video/' + self.image_id(ranker, 'clip.mp4') + '?token=' + TOKEN
        for value, start, end in [('bytes=0-9', 0, 9), ('bytes=20-', 20, len(data)-1),
                                  ('bytes=-15', len(data)-15, len(data)-1),
                                  ('bytes=1-999999', 1, len(data)-1),
                                  ('bytes=-999999', 0, len(data)-1)]:
            with self.subTest(range=value):
                status, headers, body = self.request(server, 'GET', route, {'Range':value})
                self.assertEqual(status, 206)
                self.assertEqual(body, data[start:end+1])
                self.assertEqual(headers['Content-Length'], str(end-start+1))
                self.assertEqual(headers['Content-Range'], f'bytes {start}-{end}/{len(data)}')
        for value in ['bytes=999999-', 'bytes=9-2', 'bytes=-0', 'bytes=', 'bytes=a-b',
                      'bytes=0-1,4-5', 'items=0-9', 'bytes=+1-2', 'bytes=1--2', 'bytes=١-٢']:
            with self.subTest(range=value):
                # HTTP headers are latin-1; the non-ASCII digit case uses superscript digits.
                value = value.replace('١', '¹').replace('٢', '²')
                status, headers, _ = self.request(server, 'GET', route, {'Range':value})
                self.assertEqual(status, 416)
                self.assertEqual(headers['Content-Range'], f'bytes */{len(data)}')
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            conn.putrequest('GET', route)
            conn.putheader('Range', 'bytes=0-1')
            conn.putheader('Range', 'bytes=4-5')
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 416)
            response.read()
        finally:
            conn.close()

    def test_video_head_is_authenticated_bodyless_and_exposes_range_cors(self):
        path = self.video()
        ranker = self.ranker()
        server = self.serve(ranker)
        route = '/api/video/' + self.image_id(ranker, 'clip.mp4')
        status, _, body = self.request(server, 'HEAD', route)
        self.assertEqual(status, 401)
        self.assertEqual(body, b'')
        for headers, expected in [({}, 200), ({'Range':'bytes=2-12'}, 206),
                                   ({'Range':'bytes=999999-'}, 416)]:
            status, response, body = self.request(server, 'HEAD', route + '?token=' + TOKEN, headers)
            self.assertEqual(status, expected)
            self.assertEqual(body, b'')
            self.assertEqual(response['Content-Length'], str(path.stat().st_size) if expected == 200 else ('11' if expected == 206 else '0'))
            self.assertIn('HEAD', response['Access-Control-Allow-Methods'])
            self.assertIn('Range', response['Access-Control-Allow-Headers'])
            self.assertIn('Content-Range', response['Access-Control-Expose-Headers'])
        self.assertEqual(self.request(server, 'HEAD', '/api/state?token=' + TOKEN)[0], 405)
        self.assertEqual(self.request(server, 'HEAD', route + '?token=' + TOKEN,
                                     {'Host':'evil.example'})[0], 403)

    def test_video_image_endpoint_returns_real_first_frame_jpeg_without_source_writes(self):
        paths = [self.video(), self.video('clip.webm')]
        original = [self.digest(path) for path in paths]
        ranker = self.ranker()
        before = ranker.state()
        server = self.serve(ranker)
        for path in paths:
            route = '/api/image/' + self.image_id(ranker, path.name) + '?token=' + TOKEN
            status, headers, body = self.request(server, 'GET', route)
            self.assertEqual(status, 200, body)
            self.assertEqual(headers['Content-Type'], 'image/jpeg')
            with Image.open(io.BytesIO(body)) as preview:
                self.assertLessEqual(max(preview.size), 128)
                self.assertAlmostEqual(preview.width / preview.height, 160 / 90, delta=.03)
                red, green, blue = preview.convert('RGB').getpixel((preview.width//2, preview.height//2))
                self.assertGreater(red, 180)
                self.assertLess(green, 50)
                self.assertLess(blue, 50)
        self.assertEqual([self.digest(path) for path in paths], original)
        self.assertEqual(ranker.state(), before)
        self.assertEqual({path.name for path in self.project.iterdir()}, {'data'})
        self.assertEqual(len(ranker.thumbnail_cache), 2)

    def test_video_size_budget_is_separate_from_image_budget(self):
        path = self.video()
        self.config['max_source_bytes'] = 1
        ranker = self.ranker()
        server = self.serve(ranker)
        video_id = self.image_id(ranker, 'clip.mp4')
        self.assertEqual(ranker.config.get('max_video_bytes'), 8 * 1024**3)
        for prefix in ['video', 'image']:
            self.assertEqual(self.request(server, 'GET', f'/api/{prefix}/{video_id}?token={TOKEN}')[0], 200)
        self.config['max_video_bytes'] = path.stat().st_size - 1
        limited = self.ranker()
        limited_server = self.serve(limited)
        for prefix in ['video', 'image']:
            self.assertEqual(self.request(limited_server, 'GET', f'/api/{prefix}/{video_id}?token={TOKEN}')[0], 413)
        for value in [True, 0, -1, 1.5, float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                backend.validate_config(dict(self.config, max_video_bytes=value))

    def test_corrupt_images_return_decode_error_without_dropping_http_connection(self):
        (self.archive / 'broken.png').write_bytes(b'not an image')
        ranker = self.ranker()
        server = self.serve(ranker)
        status, _, body = self.request(server, 'GET', '/api/image/' +
            self.image_id(ranker, 'broken.png') + '?token=' + TOKEN)
        self.assertEqual(status, 415)
        self.assertIn('cannot be decoded', json.loads(body)['error'])
        self.assertNotIn(str(self.archive), body.decode())

    def test_exploration_indexed_seek_can_select_appended_video_in_least_matches_bucket(self):
        for index in range(80):
            self.image(f'{index:03d}.png', (1, 1))
        self.config['exploration_fraction'] = 1
        ranker = self.ranker()
        self.video('appended.mp4')
        ranker.scan()
        video_id = self.image_id(ranker, 'appended.mp4')
        queries = []
        original_connection = ranker.connection
        from contextlib import contextmanager
        @contextmanager
        def traced():
            with original_connection() as db:
                db.set_trace_callback(queries.append)
                yield db
        ranker.connection = traced
        with patch.object(backend.random, 'randint', return_value=int(video_id)), \
             patch.object(backend.random, 'choice', side_effect=lambda rows:rows[0]):
            state = ranker.state()
        self.assertEqual(state['pair']['left']['id'], video_id)
        self.assertEqual(state['pair']['left']['kind'], 'video')
        self.assertEqual(state['pair']['left']['matches'], 0)
        self.assertEqual(state['pair']['right']['matches'], 0)
        self.assertNotEqual(state['pair']['right']['id'], video_id)
        self.assertFalse(any('random(' in query.lower() for query in queries))
        seek_queries = [query for query in queries if 'matches=' in query and 'INDEXED BY image_matches' in query]
        self.assertTrue(seek_queries)
        with original_connection() as db:
            for query in seek_queries:
                plan = db.execute('EXPLAIN QUERY PLAN ' + query).fetchall()
                self.assertFalse(any('TEMP B-TREE' in row['detail'] for row in plan))
                self.assertTrue(any('image_matches' in row['detail'] for row in plan))
        with patch.object(backend.random, 'randint', side_effect=AssertionError('pending pair must not reselect')):
            self.assertEqual(ranker.state()['pair'], state['pair'])

    def test_video_streaming_rejects_changed_inactive_deleted_and_junction_sources(self):
        path = self.video()
        self.image()
        ranker = self.ranker()
        server = self.serve(ranker)
        video_id = self.image_id(ranker, 'clip.mp4')
        route = '/api/video/' + video_id + '?token=' + TOKEN
        before = ranker.state()
        old_mtime = path.stat().st_mtime_ns
        os.utime(path, ns=(old_mtime + 10000000, old_mtime + 10000000))
        self.assertEqual(self.request(server, 'GET', route)[0], 410)
        ranker.scan()
        with ranker.connection() as db:
            db.execute('UPDATE images SET active=0 WHERE id=?', (video_id,))
        self.assertEqual(self.request(server, 'HEAD', route)[0], 410)
        ranker.scan()
        outside = self.root / 'outside'
        outside.mkdir()
        moved = outside / 'clip.mp4'
        path.rename(moved)
        self.assertEqual(self.request(server, 'GET', route)[0], 410)
        junction = self.archive / 'junction'
        subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(outside)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with ranker.connection() as db:
            db.execute('UPDATE images SET path=? WHERE id=?', (str(junction / moved.name), video_id))
        self.assertEqual(self.request(server, 'GET', route)[0], 410)
        self.assertEqual(self.request(server, 'GET', '/api/image/' + video_id + '?token=' + TOKEN)[0], 410)
        self.assertEqual(ranker.state()['pair'], before['pair'])
        self.assertEqual(ranker.state()['stats']['votes'], 0)

    def test_decoder_limits_output_and_reaps_process_on_deadline(self):
        import sys
        import time
        with self.assertRaises(backend.APIError) as error:
            backend.Ranker._decoder_output([sys.executable, '-c',
                'import sys; sys.stdout.buffer.write(b"x" * 1000000); sys.stdout.flush()'], 1024, 2)
        self.assertEqual(error.exception.status, 413)
        started = time.monotonic()
        with self.assertRaises(backend.APIError) as error:
            backend.Ranker._decoder_output([sys.executable, '-c', 'import time; time.sleep(10)'], 1024, .2)
        self.assertEqual(error.exception.status, 503)
        self.assertLess(time.monotonic() - started, 2)

    def test_decoder_absence_keeps_video_streaming_available(self):
        path = self.video()
        ranker = self.ranker()
        server = self.serve(ranker)
        video_id = self.image_id(ranker, 'clip.mp4')
        with patch.dict(os.environ, {'PATH':''}):
            status, _, body = self.request(server, 'GET', f'/api/image/{video_id}?token={TOKEN}')
            self.assertEqual(status, 503)
            self.assertIn('ffmpeg', json.loads(body)['error'])
            self.assertEqual(self.request(server, 'GET', f'/api/video/{video_id}?token={TOKEN}')[2], path.read_bytes())

    def test_video_chunks_are_64k_bounded_head_reads_nothing_and_closing_stops_stream(self):
        from contextlib import contextmanager
        path = self.video(large=True)
        self.assertGreater(path.stat().st_size, 2 * 65536)
        ranker = self.ranker()
        server = self.serve(ranker)
        route = '/api/video/' + self.image_id(ranker, path.name) + '?token=' + TOKEN
        original = ranker.video_source
        reads = []
        stop_after_read = threading.Event()
        class ObservedStream:
            def __init__(self, stream):
                self.stream = stream
            def seek(self, offset):
                return self.stream.seek(offset)
            def read(self, size):
                reads.append(size)
                if stop_after_read.is_set():
                    server.closing.set()
                return self.stream.read(size)
        @contextmanager
        def observed(video_id):
            with original(video_id) as (stream, size, content_type):
                yield ObservedStream(stream), size, content_type
        ranker.video_source = observed
        self.assertEqual(self.request(server, 'HEAD', route)[0], 200)
        self.assertEqual(reads, [])
        self.assertEqual(self.request(server, 'GET', route)[2], path.read_bytes())
        self.assertGreater(len(reads), 2)
        self.assertTrue(all(0 < size <= 65536 for size in reads), reads)
        reads.clear()
        stop_after_read.set()
        try:
            with self.assertRaises(http.client.IncompleteRead) as error:
                self.request(server, 'GET', route)
            self.assertEqual(len(error.exception.partial), 65536)
            self.assertEqual(reads, [65536])
        finally:
            server.closing.clear()


if __name__ == '__main__':
    unittest.main()
