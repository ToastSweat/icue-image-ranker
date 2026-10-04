"""Read-only archive catalog and persistent local Elo ranking service.

Run: python -B server.py --config config.json [--port 47831]
Database writes stay under this script's project directory, not the config's directory.
API bodies and worker concurrency are bounded. Streaming libvips/Pillow thumbnails
and ffmpeg video posters are generated only on demand; scans never decode or hash
archive files. The local token/package and
archive filesystem must be trusted against malicious same-user modification.
"""
import argparse
from collections import OrderedDict
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import random
import select
import signal
import socket
import sqlite3
import stat as stat_module
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid
import warnings

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None


try:
    import pyvips
except (ImportError, OSError):
    pyvips = None

if pyvips is not None:
    # Serialize decodes below; do not retain decoded source tiles between requests.
    pyvips.cache_set_max(0)
    pyvips.cache_set_max_mem(32 * 1024 * 1024)
    pyvips.cache_set_max_files(8)
    pyvips.concurrency_set(2)


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('Configuration must be an object.')
    result = dict(config)
    token = result.get('api_token')
    if not isinstance(token, str) or not 32 <= len(token) <= 256 or len(set(token)) < 8 or not all(
            c.isascii() and (c.isalnum() or c in '-_') for c in token):
        raise ValueError('api_token must be a strong random URL-safe token (at least 32 characters).')
    root = result.get('archive_root')
    if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
        raise ValueError('archive_root must be an existing absolute directory.')
    if result.get('host', '127.0.0.1') != '127.0.0.1':
        raise ValueError('Only 127.0.0.1 binding is permitted.')
    defaults = {'port':47831, 'settle_seconds':120, 'scan_interval_seconds':3600,
                'exploration_fraction':.65, 'thumbnail_max_pixels':1200,
                'max_source_pixels':500_000_000, 'max_source_bytes':256 * 1024 * 1024,
                'max_video_bytes':8 * 1024**3}
    for key, default in defaults.items():
        result.setdefault(key, default)
    limits = {'port':(1,65535), 'settle_seconds':(0,86400), 'scan_interval_seconds':(1,604800),
              'exploration_fraction':(0,1), 'thumbnail_max_pixels':(32,4096),
              'max_source_pixels':(1,1_000_000_000), 'max_source_bytes':(1,1024 * 1024 * 1024),
              'max_video_bytes':(1,1024**4)}
    for key, (minimum, maximum) in limits.items():
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int,float)) or not minimum <= value <= maximum:
            raise ValueError(f'Invalid {key}.')
        if key in ('port','thumbnail_max_pixels','max_source_pixels','max_source_bytes','max_video_bytes') and not isinstance(value, int):
            raise ValueError(f'{key} must be an integer.')
    result.setdefault('include_dirs', [])
    include = result['include_dirs']
    if not isinstance(include, list):
        raise ValueError('include_dirs must be a list of archive-relative directories.')
    for name in include:
        if not isinstance(name, str) or not name or ':' in name or name.startswith(('/', '\\')):
            raise ValueError('include_dirs cannot contain absolute paths or traversal.')
        parts = name.replace('\\', '/').split('/')
        if any(not part or part.startswith('.') for part in parts):
            raise ValueError('include_dirs cannot contain hidden directories or traversal.')
    result.setdefault('extensions', ['.jpg','.jpeg','.png','.gif','.webp','.bmp','.avif'])
    extensions = result['extensions']
    safe_extensions = {'.jpg','.jpeg','.jfif','.png','.gif','.webp','.bmp','.avif','.tif','.tiff'}
    if not isinstance(extensions,list) or not extensions or any(
            not isinstance(e,str) or e.lower() not in safe_extensions for e in extensions):
        raise ValueError('extensions must contain supported raster image suffixes.')
    result.setdefault('include_videos', True)
    result.setdefault('video_extensions', ['.mp4', '.webm'])
    if not isinstance(result['include_videos'], bool):
        raise ValueError('include_videos must be a boolean.')
    videos = result['video_extensions']
    if not isinstance(videos, list) or not videos or any(
            not isinstance(e, str) or e.lower() not in {'.mp4', '.webm'} for e in videos):
        raise ValueError('video_extensions must contain MP4 or WebM suffixes.')
    result.setdefault('excluded_dirs', ['.r34-watcher','image-ranker','node_modules','.git'])
    if not isinstance(result['excluded_dirs'],list) or any(
            not isinstance(e,str) or not e or '/' in e or '\\' in e for e in result['excluded_dirs']):
        raise ValueError('excluded_dirs must contain simple directory names.')
    return result


class Ranker:
    def __init__(self, config, project_dir):
        self.config = validate_config(config)
        self.project = Path(project_dir).resolve()
        self.archive = Path(config['archive_root']).resolve()
        self.token = config['api_token']
        self.lock = threading.RLock()
        self.scanning = False
        self.scan_error = None
        self.stop = threading.Event()
        self.scan_thread = None
        self.thumbnail_lock = threading.Lock()
        self.thumbnail_cache = OrderedDict()
        self.thumbnail_bytes = 0
        data = self.project / 'data'
        if not data.resolve().is_relative_to(self.project):
            raise ValueError('Ranking output directory must stay within the project.')
        if data.exists():
            info = data.lstat()
            if stat_module.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                raise ValueError('Ranking output directory cannot be a link or reparse point.')
        data.mkdir(parents=True, exist_ok=True)
        self.db = data / 'rankings.sqlite3'
        self._check_database_paths()
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY, name_key TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, path TEXT NOT NULL,
                    rating REAL NOT NULL DEFAULT 1000, matches INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1, seen TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS image_matches ON images(active,matches,id);
                CREATE INDEX IF NOT EXISTS image_rating ON images(active,rating DESC,id);
                CREATE INDEX IF NOT EXISTS image_active ON images(active,id);
                CREATE TABLE IF NOT EXISTS pairs (
                    id TEXT PRIMARY KEY, left_id INTEGER NOT NULL, right_id INTEGER NOT NULL,
                    winner TEXT, created REAL NOT NULL, voted REAL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS rated_leaderboard ON images(active,rating DESC,id) WHERE matches>0;
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY CHECK(id=1), images INTEGER NOT NULL,
                    rated INTEGER NOT NULL, votes INTEGER NOT NULL);
                INSERT OR IGNORE INTO stats SELECT 1,
                    (SELECT count(*) FROM images WHERE active=1),
                    (SELECT count(*) FROM images WHERE active=1 AND matches>0),
                    (SELECT count(*) FROM pairs WHERE winner IS NOT NULL);
                CREATE TRIGGER IF NOT EXISTS image_insert AFTER INSERT ON images BEGIN
                    UPDATE stats SET images=images+NEW.active,
                        rated=rated+NEW.active*(NEW.matches>0) WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS image_update AFTER UPDATE OF active,matches ON images BEGIN
                    UPDATE stats SET images=images+NEW.active-OLD.active,
                        rated=rated+NEW.active*(NEW.matches>0)-OLD.active*(OLD.matches>0) WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS image_delete AFTER DELETE ON images BEGIN
                    UPDATE stats SET images=images-OLD.active,
                        rated=rated-OLD.active*(OLD.matches>0) WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS vote_update AFTER UPDATE OF winner ON pairs BEGIN
                    UPDATE stats SET votes=votes+(NEW.winner IS NOT NULL)-(OLD.winner IS NOT NULL) WHERE id=1;
                END;
            ''')

    def _check_database_paths(self):
        for suffix in ('','-wal','-shm','-journal'):
            path = Path(str(self.db) + suffix)
            if not path.resolve().is_relative_to(self.project):
                raise ValueError('Ranking database must stay within the project.')
            try:
                info = path.lstat()
            except FileNotFoundError:
                # SQLite creates/removes WAL and SHM files as connections open/close.
                continue
            if stat_module.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                raise ValueError('Ranking database cannot be a link or reparse point.')

    @contextmanager
    def connection(self):
        self._check_database_paths()
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start_scanner(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        def run():
            while not self.stop.is_set():
                self.scan()
                self.stop.wait(self.config['scan_interval_seconds'])
        self.scan_thread = threading.Thread(target=run, name='image-ranker-scan', daemon=False)
        self.scan_thread.start()

    def close(self):
        self.stop.set()
        if self.scan_thread and self.scan_thread is not threading.current_thread():
            self.scan_thread.join()

    def scan(self):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        generation = uuid.uuid4().hex
        batch = []
        excluded = {name.lower() for name in self.config['excluded_dirs']}
        extensions = {e.lower() for e in self.config.get('extensions', ['.jpg','.jpeg','.png','.gif','.webp'])}
        if self.config['include_videos']:
            extensions.update(e.lower() for e in self.config['video_extensions'])
        cutoff = time.time() - self.config.get('settle_seconds', 120)
        stack = [self.archive / name for name in self.config.get('include_dirs', [])] or [self.archive]
        try:
            while stack and not self.stop.is_set():
                directory = stack.pop()
                if self._safe_path(directory) is None:
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if self.stop.is_set():
                            break
                        if entry.name.startswith('.') or entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in excluded and not getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0) & 0x400 and Path(entry.path).resolve() != self.project:
                                stack.append(Path(entry.path))
                            continue
                        if Path(entry.name).suffix.lower() not in extensions or not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        if max(stat.st_mtime,stat.st_ctime) > cutoff or getattr(stat, 'st_file_attributes', 0) & 0x400:
                            continue
                        batch.append((entry.name.lower(),entry.name,entry.path,generation,stat.st_size,stat.st_mtime_ns))
                        if len(batch) >= 512:
                            self._scan_batch(batch)
                            batch.clear()
            self._scan_batch(batch)
            cursor = 0
            while not self.stop.is_set():
                with self.connection() as db:
                    missing = db.execute('SELECT id FROM images INDEXED BY image_active WHERE active=1 AND id>? AND seen!=? ORDER BY id LIMIT 512',
                                         (cursor,generation)).fetchall()
                    if not missing:
                        break
                    cursor = missing[-1]['id']
                    db.executemany('UPDATE images SET active=0 WHERE id=? AND seen!=? AND active=1',
                                   [(row['id'],generation) for row in missing])
            self.scan_error = None
        except (OSError,sqlite3.Error):
            self.scan_error = 'Archive scan failed; existing catalog preserved.'
        finally:
            with self.lock:
                self.scanning = False

    def _scan_batch(self, batch):
        with self.connection() as db:
            db.executemany('''INSERT INTO images(name_key,name,path,seen,size,mtime)
                VALUES(?,?,?,?,?,?) ON CONFLICT(name_key) DO UPDATE SET
                name=excluded.name,path=excluded.path,seen=excluded.seen,
                size=excluded.size,mtime=excluded.mtime,active=1 WHERE images.seen != excluded.seen''', batch)

    @staticmethod
    def _kind(row):
        return 'video' if Path(row['name']).suffix.lower() in {'.mp4', '.webm'} else 'image'

    def _image(self, row):
        kind = self._kind(row)
        poster = f"/api/image/{row['id']}?token={quote(self.token, safe='')}"
        return {'id':str(row['id']), 'name':row['name'], 'rating':row['rating'],
                'matches':row['matches'], 'kind':kind, 'poster_url':poster,
                'url':poster if kind == 'image' else
                    f"/api/video/{row['id']}?token={quote(self.token, safe='')}"}

    def _pair(self, db):
        current = db.execute("SELECT value FROM meta WHERE key='pair'").fetchone()
        if current:
            pair = db.execute('SELECT * FROM pairs WHERE id=?', (current['value'],)).fetchone()
            if pair and pair['winner'] is None:
                return pair
        rows = db.execute('SELECT * FROM images INDEXED BY image_matches WHERE active=1 ORDER BY matches,id LIMIT 64').fetchall()
        if len(rows) < 2:
            return None
        # Serialize pair creation across connections/processes, then recheck.
        db.execute('BEGIN IMMEDIATE')
        current = db.execute("SELECT value FROM meta WHERE key='pair'").fetchone()
        if current:
            pair = db.execute('SELECT * FROM pairs WHERE id=?', (current['value'],)).fetchone()
            if pair and pair['winner'] is None:
                return pair
        rows = db.execute('SELECT * FROM images INDEXED BY image_matches WHERE active=1 ORDER BY matches,id LIMIT 64').fetchall()
        if len(rows) < 2:
            return None
        if random.random() < self.config['exploration_fraction']:
            minimum = rows[0]['matches']
            first = db.execute('SELECT id FROM images INDEXED BY image_matches WHERE active=1 AND matches=? ORDER BY id LIMIT 1',
                               (minimum,)).fetchone()['id']
            last = db.execute('SELECT id FROM images INDEXED BY image_matches WHERE active=1 AND matches=? ORDER BY id DESC LIMIT 1',
                              (minimum,)).fetchone()['id']
            pivot = random.randint(first, last)
            least = db.execute('SELECT * FROM images INDEXED BY image_matches WHERE active=1 AND matches=? AND id>=? ORDER BY id LIMIT 64',
                               (minimum,pivot)).fetchall()
            if len(least) < 64:
                least += db.execute('SELECT * FROM images INDEXED BY image_matches WHERE active=1 AND matches=? AND id<? ORDER BY id LIMIT ?',
                                    (minimum,pivot,64-len(least))).fetchall()
            left = random.choice(least)
            opponents = [row for row in least if row['id'] != left['id']]
            if not opponents:
                opponents = [row for row in rows if row['id'] != left['id']]
            right = random.choice(opponents)
        else:
            leaders = db.execute('SELECT * FROM images INDEXED BY image_rating WHERE active=1 ORDER BY rating DESC,id LIMIT 10').fetchall()
            left = random.choice(leaders)
            above = db.execute('SELECT * FROM images INDEXED BY image_rating WHERE active=1 AND rating>=? ORDER BY rating,id DESC LIMIT 16', (left['rating'],)).fetchall()
            below = db.execute('SELECT * FROM images INDEXED BY image_rating WHERE active=1 AND rating<=? ORDER BY rating DESC,id LIMIT 16', (left['rating'],)).fetchall()
            near = {row['id']:row for row in above + below if row['id'] != left['id']}
            opponents = sorted(near.values(), key=lambda row:(abs(row['rating']-left['rating']),row['id']))[:8]
            if not opponents:
                opponents = [row for row in rows if row['id'] != left['id']]
            right = random.choice(opponents)
        last = db.execute("SELECT value FROM meta WHERE key='last_pair'").fetchone()
        if last and {str(left['id']), str(right['id'])} == set(last['value'].split(',')) and len(rows) >= 3:
            right = random.choice([row for row in rows if row['id'] not in (left['id'], right['id'])])
        identity = uuid.uuid4().hex
        db.execute('INSERT INTO pairs(id,left_id,right_id,created) VALUES(?,?,?,?)',
                   (identity,left['id'],right['id'],time.time()))
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('pair',?)", (identity,))
        return db.execute('SELECT * FROM pairs WHERE id=?', (identity,)).fetchone()

    def _safe_path(self, path):
        path = Path(path)
        try:
            relative = path.relative_to(self.archive)
            if path.resolve().is_relative_to(self.project) or not path.resolve().is_relative_to(self.archive):
                return None
            current = self.archive
            excluded = {name.lower() for name in self.config['excluded_dirs']}
            for part in relative.parts:
                if part.startswith('.') or part.lower() in excluded:
                    return None
                current = current / part
                info = current.lstat()
                if stat_module.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    return None
            return path
        except (OSError, ValueError, RuntimeError):
            return None

    def _available(self, row):
        if not row or not row['active']:
            return False
        path = self._safe_path(row['path'])
        if path is None:
            return False
        try:
            info = path.stat()
            return (stat_module.S_ISREG(info.st_mode) and info.st_size == row['size']
                    and info.st_mtime_ns == row['mtime']
                    and max(info.st_mtime,info.st_ctime) <= time.time() - self.config['settle_seconds'])
        except OSError:
            return False

    @contextmanager
    def video_source(self, video_id):
        if not isinstance(video_id, str) or not video_id.isascii() or not video_id.isdigit() or len(video_id) > 18:
            raise APIError(404, 'Video not found.')
        with self.connection() as db:
            row = db.execute('SELECT * FROM images WHERE id=?', (video_id,)).fetchone()
        if not row or self._kind(row) != 'video':
            raise APIError(404, 'Video not found.')
        if not self._available(row):
            raise APIError(410, 'Video source unavailable.')
        if row['size'] > self.config['max_video_bytes']:
            raise APIError(413, 'Video exceeds safe source size limit.')
        path = Path(row['path'])
        try:
            stream = path.open('rb')
        except OSError:
            raise APIError(410, 'Video source unavailable.') from None
        with stream:
            info = os.fstat(stream.fileno())
            if (not stat_module.S_ISREG(info.st_mode) or info.st_size != row['size']
                    or info.st_mtime_ns != row['mtime'] or not self._safe_path(path)):
                raise APIError(410, 'Video source changed.')
            yield stream, info.st_size, 'video/mp4' if path.suffix.lower() == '.mp4' else 'video/webm'

    @staticmethod
    def _decoder_output(command, limit, timeout):
        """Capture bounded decoder output, killing and reaping even on timeout/disconnect."""
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
        except OSError:
            raise APIError(503, 'Video previews require ffmpeg and ffprobe.') from None
        expired = threading.Event()
        def kill():
            expired.set()
            try:
                process.kill()
            except OSError:
                pass
        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            output = process.stdout.read(limit + 1)
            if len(output) > limit:
                raise APIError(413, 'Video preview exceeds output limit.')
            code = process.wait(timeout=timeout)
            if expired.is_set():
                raise APIError(503, 'Video preview decoder timed out.')
            if code != 0 or not output:
                raise APIError(415, 'Video cannot be decoded.')
            return output
        except subprocess.TimeoutExpired:
            raise APIError(503, 'Video preview decoder timed out.') from None
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            timer.join()

    def _video_poster(self, path):
        dimensions = self._decoder_output([
            'ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(path)], 1024, 10)
        try:
            width, height = (int(value) for value in dimensions.decode('ascii').strip().split(','))
        except (ValueError, UnicodeError):
            raise APIError(415, 'Video cannot be decoded.') from None
        if width <= 0 or height <= 0 or width * height > self.config['max_source_pixels']:
            raise APIError(413, 'Video frame exceeds safe decoding limit.')
        maximum = self.config['thumbnail_max_pixels']
        output_limit = min(32 * 1024 * 1024, maximum * maximum * 3 + 65536)
        body = self._decoder_output([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-protocol_whitelist', 'file', '-threads', '2', '-i', str(path),
            '-map', '0:v:0', '-frames:v', '1', '-an', '-sn', '-dn', '-filter_threads', '1',
            '-vf', f'scale={maximum}:{maximum}:force_original_aspect_ratio=decrease',
            '-threads', '2', '-c:v', 'mjpeg', '-q:v', '3', '-f', 'image2pipe', 'pipe:1'],
            output_limit, 20)
        return body, 'image/jpeg'

    def image_bytes(self, image_id):
        if not isinstance(image_id,str) or not image_id.isascii() or not image_id.isdigit() or len(image_id) > 18:
            raise APIError(404, 'Image not found.')
        with self.connection() as db:
            row = db.execute('SELECT * FROM images WHERE id=?', (image_id,)).fetchone()
        if not row:
            raise APIError(404, 'Image not found.')
        if not self._available(row):
            raise APIError(410, 'Image source unavailable.')
        path = Path(row['path'])
        key = (row['id'],row['path'],row['mtime'],row['size'],self.config['thumbnail_max_pixels'])
        with self.thumbnail_lock:
            if key in self.thumbnail_cache:
                self.thumbnail_cache.move_to_end(key)
                return self.thumbnail_cache[key]
            size_limit = self.config['max_video_bytes'] if self._kind(row) == 'video' else self.config['max_source_bytes']
            if row['size'] > size_limit:
                raise APIError(413, 'Media exceeds safe decoding limit.')
            try:
                with path.open('rb') as stream:
                    info = os.fstat(stream.fileno())
                    if info.st_size != row['size'] or info.st_mtime_ns != row['mtime'] or not self._safe_path(path):
                        raise APIError(410, 'Image source changed.')
                    if self._kind(row) == 'video':
                        result = self._video_poster(path)
                        if not self._available(row):
                            raise APIError(410, 'Video source changed.')
                    elif pyvips is not None:
                        source = pyvips.Source.new_from_descriptor(stream.fileno())
                        header = pyvips.Image.new_from_source(source, '', access='sequential')
                        if header.width * header.height > self.config['max_source_pixels']:
                            raise APIError(413, 'Image exceeds safe decoding limit.')
                        maximum = self.config['thumbnail_max_pixels']
                        frame = pyvips.Image.thumbnail_source(source, maximum, height=maximum,
                                                             size='down', fail_on='error')
                        result = (frame.jpegsave_buffer(Q=85, background=[255,255,255]), 'image/jpeg')
                        del frame, header, source
                    elif Image is None:
                        if info.st_size > 16 * 1024 * 1024:
                            raise APIError(413, 'Large images require Pillow thumbnails.')
                        return stream.read(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                    else:
                        with warnings.catch_warnings():
                            warnings.simplefilter('error', Image.DecompressionBombWarning)
                            with Image.open(stream) as source:
                                if source.width * source.height > self.config['max_source_pixels']:
                                    raise APIError(413, 'Image exceeds safe decoding limit.')
                                if source.width * source.height > 40_000_000:
                                    raise APIError(503, 'Large image previews require pyvips and libvips.')
                                maximum = self.config['thumbnail_max_pixels']
                                source.draft('RGB', (maximum,maximum))
                                source.thumbnail((maximum,maximum), Image.Resampling.LANCZOS)
                                frame = ImageOps.exif_transpose(source).convert('RGB')
                                output = io.BytesIO()
                                frame.save(output, format='JPEG', quality=85)
                                result = (output.getvalue(),'image/jpeg')
            except APIError:
                raise
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) if Image else ():
                raise APIError(413, 'Image exceeds safe decoding limit.') from None
            except (pyvips.Error,) if pyvips is not None else ():
                raise APIError(415, 'Image cannot be decoded.') from None
            except (OSError, ValueError, SyntaxError):
                raise APIError(415, 'Image cannot be decoded.') from None
            self.thumbnail_cache[key] = result
            self.thumbnail_bytes += len(result[0])
            while len(self.thumbnail_cache) > 128 or self.thumbnail_bytes > 32 * 1024 * 1024:
                _, evicted = self.thumbnail_cache.popitem(last=False)
                self.thumbnail_bytes -= len(evicted[0])
            return result

    def vote(self, pair_id, winner):
        with self.lock, self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT value FROM meta WHERE key='pair'").fetchone()
            pair = db.execute('SELECT * FROM pairs WHERE id=?', (pair_id,)).fetchone()
            if not pair or pair['winner'] is not None or not current or current['value'] != pair_id:
                raise APIError(409, 'Stale or already answered pair.')
            if winner not in (str(pair['left_id']), str(pair['right_id'])):
                raise APIError(400, 'Winner must belong to the pending pair.')
            left = db.execute('SELECT * FROM images WHERE id=?', (pair['left_id'],)).fetchone()
            right = db.execute('SELECT * FROM images WHERE id=?', (pair['right_id'],)).fetchone()
            if not self._available(left) or not self._available(right):
                raise APIError(409, 'Pending pair source unavailable.')
            expected = 1 / (1 + 10 ** ((right['rating'] - left['rating']) / 400))
            delta = 32 * ((1 if winner == str(left['id']) else 0) - expected)
            db.execute('UPDATE images SET rating=rating+?,matches=matches+1 WHERE id=?', (delta,left['id']))
            db.execute('UPDATE images SET rating=rating-?,matches=matches+1 WHERE id=?', (delta,right['id']))
            db.execute('UPDATE pairs SET winner=?,voted=? WHERE id=?', (winner,time.time(),pair_id))
            db.execute("DELETE FROM meta WHERE key='pair'")
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_pair',?)",
                       (f"{pair['left_id']},{pair['right_id']}",))
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_vote',?)",
                       (json.dumps({'pair_id': pair_id, 'winner': winner}),))
        return self.state()

    def export_chunks(self):
        # A WAL snapshot and cursor iteration keep large exports bounded in memory.
        with self.connection() as db:
            db.execute('BEGIN')
            yield b'{"version":1,"ratings":['
            first = True
            for row in db.execute('SELECT id,name,rating,matches,active FROM images ORDER BY id'):
                item = dict(row)
                item['id'] = str(item['id'])
                yield (('' if first else ',') + json.dumps(item,ensure_ascii=True)).encode('utf-8')
                first = False
            yield b'],"votes":['
            first = True
            for row in db.execute('SELECT id,left_id,right_id,winner,created,voted FROM pairs WHERE winner IS NOT NULL ORDER BY voted,id'):
                item = dict(row)
                item['left_id'], item['right_id'] = str(item['left_id']),str(item['right_id'])
                yield (('' if first else ',') + json.dumps(item,ensure_ascii=True)).encode('utf-8')
                first = False
            yield b']}'

    def state(self):
        with self.lock, self.connection() as db:
            pair = self._pair(db)
            if not db.in_transaction:
                db.execute('BEGIN')
            counters = db.execute('SELECT images,rated,votes FROM stats WHERE id=1').fetchone()
            result = {'pair':None, 'top':[], 'stats':dict(counters, scanning=self.scanning)}
            receipt = db.execute("SELECT value FROM meta WHERE key='last_vote'").fetchone()
            result['last_vote'] = json.loads(receipt['value']) if receipt else None
            result['top'] = [self._image(row) for row in db.execute(
                'SELECT * FROM images INDEXED BY rated_leaderboard WHERE active=1 AND matches>0 ORDER BY rating DESC,id LIMIT 5')]
            if pair:
                left = db.execute('SELECT * FROM images WHERE id=?', (pair['left_id'],)).fetchone()
                right = db.execute('SELECT * FROM images WHERE id=?', (pair['right_id'],)).fetchone()
                result['pair'] = {'id':pair['id'], 'left':self._image(left), 'right':self._image(right)}
                if not self._available(left) or not self._available(right):
                    result['error'] = 'Pending pair source unavailable; restore the source to continue.'
            if self.scan_error:
                result['error'] = self.scan_error
            return result


class DeadlineReader:
    """Bounded HTTP reads with an interruptible deadline, including on Windows.

    Socket.shutdown alone does not wake Python's timed select/recv path reliably
    on Windows. Short readiness waits let shutdown interrupt incomplete requests.
    """
    def __init__(self, connection, closing):
        self.connection = connection
        self.closing = closing
        self.deadline = time.monotonic() + 10
        self.buffer = bytearray()

    def _receive(self):
        while True:
            if self.closing.is_set():
                raise ConnectionError('Server shutting down.')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('HTTP request deadline exceeded.')
            readable, _, _ = select.select([self.connection], [], [], min(.1,remaining))
            if readable:
                return self.connection.recv(8192)

    def _take(self, count):
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def readline(self, size=-1):
        limit = 65537 if size < 0 else min(size,65537)
        while True:
            newline = self.buffer.find(b'\n',0,limit)
            if newline >= 0:
                return self._take(newline+1)
            if len(self.buffer) >= limit:
                return self._take(limit)
            chunk = self._receive()
            if not chunk:
                return self._take(min(limit,len(self.buffer)))
            self.buffer.extend(chunk)

    def read(self, size):
        if size < 0 or size > 4096:
            raise ValueError('HTTP body read must be bounded.')
        while len(self.buffer) < size:
            chunk = self._receive()
            if not chunk:
                break
            self.buffer.extend(chunk)
        return self._take(min(size,len(self.buffer)))

    def close(self):
        self.buffer.clear()


class Handler(BaseHTTPRequestHandler):
    server_version = 'ImageRanker'
    sys_version = ''

    def log_message(self, format, *args):
        # Base handler logs request URLs, which contain the private token.
        pass

    def handle(self):
        try:
            super().handle()
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            if not getattr(self,'headers_sent',False):
                try:
                    self._json(503, {'error':'Request could not be completed; retry safely.'})
                except OSError:
                    pass
            self.close_connection = True

    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self.rfile.close()
        self.rfile = DeadlineReader(self.connection,self.server.closing)

    def parse_request(self):
        if not super().parse_request():
            return False
        hosts = self.headers.get_all('Host', [])
        allowed = {'127.0.0.1', 'localhost', f'127.0.0.1:{self.server.server_port}',
                   f'localhost:{self.server.server_port}'}
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            self._json(403, {'error':'Only loopback Host is allowed.'})
            return False
        if len(self.path) > 4096:
            self._json(414, {'error':'Request target too long.'})
            return False
        return True

    def send_error(self, code, message=None, explain=None):
        self._json(405 if code == 501 else code, {'error':'Request rejected.'})

    def end_headers(self):
        if getattr(self, 'path', '').startswith('/api/'):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, Range')
            self.send_header('Access-Control-Expose-Headers', 'Accept-Ranges, Content-Length, Content-Range')
        self.send_header('Referrer-Policy', 'no-referrer')
        super().end_headers()
        self.headers_sent = True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _authenticated(self, query):
        tokens = parse_qs(query).get('token', [])
        authorization = self.headers.get('Authorization', '')
        candidate = authorization[7:] if authorization.startswith('Bearer ') else (
            tokens[0] if len(tokens) == 1 else '')
        return hmac.compare_digest(candidate.encode('utf-8'), self.server.ranker.token.encode('utf-8'))

    @staticmethod
    def _unique_object(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field.')
            result[key] = value
        return result

    @staticmethod
    def _invalid_constant(value):
        raise ValueError('Non-finite JSON constant.')

    def do_POST(self):
        try:
            route = urlsplit(self.path)
            if not self._authenticated(route.query):
                raise APIError(401, 'Authentication required.')
            if route.path != '/api/vote':
                raise APIError(404, 'Not found.')
            if self.headers.get('Transfer-Encoding'):
                raise APIError(400, 'Transfer-Encoding is not supported.')
            lengths = self.headers.get_all('Content-Length', [])
            if not lengths:
                raise APIError(411, 'Content-Length required.')
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise APIError(400, 'Invalid Content-Length.')
            if len(lengths[0]) > 10:
                raise APIError(413, 'Request body too large.')
            size = int(lengths[0])
            if size > 4096:
                raise APIError(413, 'Request body too large.')
            if self.headers.get_content_type() != 'application/json':
                raise APIError(415, 'Content-Type must be application/json.')
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise APIError(400, 'Incomplete request body.')
                payload = json.loads(raw.decode('utf-8'), object_pairs_hook=self._unique_object,
                                     parse_constant=self._invalid_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise APIError(400, 'Invalid JSON.') from None
            if not isinstance(payload,dict) or set(payload) != {'pair_id','winner'} or any(
                    not isinstance(payload[k],str) or not payload[k] for k in ('pair_id','winner')):
                raise APIError(400, 'Expected pair_id and winner strings.')
            self._json(200, self.server.ranker.vote(payload['pair_id'],payload['winner']))
        except APIError as error:
            self._json(error.status, {'error':str(error)})

    def _static(self, raw_path):
        origin = self.headers.get('Origin')
        if (origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',
                                     f'http://localhost:{self.server.server_port}')) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise APIError(403, 'Cross-origin preview access denied.')
        path = unquote(raw_path)
        if path == '/':
            path = '/index.html'
        if path.startswith('/widget/'):
            path = path[len('/widget'):]
        parts = path.lstrip('/').split('/')
        if any(not part or part.startswith('.') or ':' in part or '\\' in part or '%' in part for part in parts):
            raise APIError(404, 'Not found.')
        root = self.server.ranker.project / 'widget'
        file = root.joinpath(*parts)
        allowed = {'.html','.js','.css','.json','.svg','.png','.ico','.woff','.woff2'}
        try:
            if file.suffix.lower() not in allowed or not file.resolve().is_relative_to(root.resolve()):
                raise APIError(404, 'Not found.')
            current = root
            for part in ('', *parts):
                if part:
                    current = current / part
                info = current.lstat()
                if stat_module.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                    raise APIError(404, 'Not found.')
            if not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
                raise APIError(404, 'Not found.')
            body = file.read_bytes()
        except OSError:
            raise APIError(404, 'Not found.') from None
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _byte_range(values, size):
        if not values:
            return 0, size - 1, False
        if len(values) != 1 or not values[0].startswith('bytes='):
            return None
        parts = values[0][6:].split('-')
        if len(parts) != 2 or not any(parts) or size == 0:
            return None
        if any(part and (not part.isascii() or not part.isdigit() or len(part) > 20) for part in parts):
            return None
        first, last = parts
        if not first:
            suffix = int(last)
            if suffix == 0:
                return None
            return max(0, size - suffix), size - 1, True
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or start > end:
            return None
        return start, end, True

    def _video(self, video_id):
        with self.server.ranker.video_source(video_id) as (stream, size, content_type):
            byte_range = self._byte_range(self.headers.get_all('Range', []), size)
            if byte_range is None:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                return
            start, end, partial = byte_range
            remaining = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header('Content-Type', content_type)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(remaining))
            if partial:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if self.command == 'HEAD':
                return
            stream.seek(start)
            try:
                while remaining and not self.server.closing.is_set():
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except OSError:
                self.close_connection = True

    def do_HEAD(self):
        try:
            route = urlsplit(self.path)
            if not route.path.startswith('/api/video/'):
                raise APIError(405, 'Method not allowed.')
            if not self._authenticated(route.query):
                raise APIError(401, 'Authentication required.')
            self._video(route.path[len('/api/video/'):])
        except APIError as error:
            self._json(error.status, {'error':str(error)})

    def do_GET(self):
        try:
            route = urlsplit(self.path)
            if not route.path.startswith('/api/'):
                self._static(route.path)
                return
            if not self._authenticated(route.query):
                raise APIError(401, 'Authentication required.')
            if route.path == '/api/state':
                self._json(200, self.server.ranker.state())
            elif route.path.startswith('/api/video/'):
                self._video(route.path[len('/api/video/'):])
            elif route.path.startswith('/api/image/'):
                body, content_type = self.server.ranker.image_bytes(route.path[len('/api/image/'):])
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)
            elif route.path == '/api/export':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Disposition', 'attachment; filename="image-rankings.json"')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Connection', 'close')
                self.end_headers()
                for chunk in self.server.ranker.export_chunks():
                    self.wfile.write(chunk)
                self.close_connection = True
            elif route.path == '/api/health':
                self._json(200, {'ok':True, 'scanning':self.server.ranker.scanning})
            else:
                raise APIError(404, 'Not found.')
        except APIError as error:
            self._json(error.status, {'error':str(error)})


class RankingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32
    max_workers = 16

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(self.max_workers)
        self.worker_lock = threading.Lock()
        self.worker_count = 0
        self.active_requests = set()
        self.closing = threading.Event()
        super().__init__(address,handler)

    def server_bind(self):
        # Windows SO_REUSEADDR permits two live servers on the same endpoint,
        # which can send a request to an older companion unpredictably.
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        with self.worker_lock:
            self.worker_count += 1
            self.active_requests.add(request)
        try:
            super().process_request(request,client_address)
        except BaseException:
            self._release_worker(request)
            raise

    def _release_worker(self, request):
        with self.worker_lock:
            self.worker_count -= 1
            self.active_requests.discard(request)
        self.slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            self._release_worker(request)

    def server_close(self):
        # Interrupt slow/incomplete HTTP reads before joining owned worker threads.
        # Any SQLite transaction already in flight still commits or rolls back normally.
        self.closing.set()
        with self.worker_lock:
            requests = list(self.active_requests)
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().server_close()

    def handle_error(self, request, client_address):
        # Do not leak archive paths or tokens in unexpected exception tracebacks.
        pass


def make_server(ranker, port=None):
    server = RankingHTTPServer(('127.0.0.1', ranker.config['port'] if port is None else port), Handler)
    server.ranker = ranker
    return server



def main(argv=None, *, project_dir=None, shutdown_event=None):
    project = Path(project_dir) if project_dir is not None else Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='Loopback-only persistent image ranking backend.')
    parser.add_argument('--config', type=Path, default=project / 'config.json', help='JSON configuration path')
    parser.add_argument('--port', type=int, help='Override configuration port (1-65535)')
    args = parser.parse_args(argv)
    ranker = server = serving = None
    stop = shutdown_event if shutdown_event is not None else threading.Event()
    handlers = {}
    try:
        with args.config.open('r', encoding='utf-8') as file:
            config = json.load(file)
        if not isinstance(config,dict):
            raise ValueError('Configuration must be an object.')
        if args.port is not None:
            config['port'] = args.port
        ranker = Ranker(config, project)
        server = make_server(ranker)
        if threading.current_thread() is threading.main_thread():
            for name in ('SIGINT','SIGTERM','SIGBREAK'):
                if hasattr(signal,name):
                    number = getattr(signal,name)
                    handlers[number] = signal.signal(number, lambda signum, frame:stop.set())
        ranker.start_scanner()
        serving = threading.Thread(target=server.serve_forever, name='image-ranker-http', daemon=False)
        serving.start()
        print(f'Image Ranker listening on http://127.0.0.1:{server.server_port} (token required)', flush=True)
        stop.wait()
        return 0
    except (OSError, ValueError, sqlite3.Error):
        # Never echo config, request URLs, tokens, or private archive paths.
        print('Image Ranker could not start: check configuration, port, and project permissions.', file=sys.stderr)
        return 1
    finally:
        if ranker:
            ranker.stop.set()
        if server:
            if serving:
                server.shutdown()
                serving.join()
            server.server_close()
        if ranker:
            ranker.close()
        for number, handler in handlers.items():
            signal.signal(number,handler)


if __name__ == '__main__':
    raise SystemExit(main())
