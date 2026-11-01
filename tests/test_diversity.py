"""Scratch-only diversity regressions; never open the real catalog or decode media."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('diversity_backend', PROJECT / 'server.py')
assert spec is not None and spec.loader is not None
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
SCRATCH = Path(os.environ['TMPDIR'])
TOKEN = 'diversity-test-token-0123456789abcdef'


class DiversityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ranker-diversity-', dir=SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.config = {'archive_root': str(self.archive), 'api_token': TOKEN,
                       'settle_seconds': 0, 'include_videos': False}
        state = backend.random.getstate()
        self.addCleanup(backend.random.setstate, state)
        backend.random.seed(47)

    def ranker(self, count=120, **settings):
        ranker = backend.Ranker(dict(self.config, **settings), self.project)
        self.addCleanup(ranker.close)
        if count:
            rows = []
            for index in range(count):
                path = self.archive / f'{index:04d}.png'
                path.write_bytes(b'fixture metadata only; no decode')
                info = path.stat()
                rows.append((path.name, path.name, str(path), 'fixture', info.st_size, info.st_mtime_ns))
            ranker._scan_batch(rows)
        return ranker

    @staticmethod
    def ids(pair):
        return frozenset((pair['left']['id'], pair['right']['id']))

    def test_history_blocks_unordered_repeats_for_260_real_votes_across_restart(self):
        ranker = self.ranker(exploration_fraction=0)
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=10,rating=1000+id')
        state = ranker.state()
        recent = []
        for turn in range(260):
            pair = state['pair']
            ids = self.ids(pair)
            self.assertNotIn(ids, recent[-500:], f'repeat on comparison {turn}')
            recent.append(ids)
            state = ranker.vote(pair['id'], pair['left']['id'])
            if turn == 129:
                ranker.close()
                ranker = self.ranker(count=0, exploration_fraction=0)
                self.assertEqual(ranker.state()['pair'], state['pair'])
        self.assertEqual(state['stats']['votes'], 260)

    def test_each_image_rests_for_12_presentations_when_pool_allows(self):
        ranker = self.ranker(exploration_fraction=0)
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=10,rating=1000+id')
        state = ranker.state()
        recent = []
        for turn in range(260):
            pair = state['pair']
            ids = self.ids(pair)
            unavailable = set().union(*recent[-12:])
            self.assertTrue(ids.isdisjoint(unavailable), f'image cooldown on comparison {turn}')
            recent.append(ids)
            state = ranker.vote(pair['id'], pair['right']['id'])

    def test_refinement_uses_configured_broad_pool_not_only_top_ten(self):
        ranker = self.ranker(exploration_fraction=0, pair_repeat_cooldown=0,
                             image_repeat_cooldown=0, refinement_pool_size=100)
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=10,rating=1000+id')
        state = ranker.state()
        anchors = []
        for _ in range(260):
            pair = state['pair']
            anchors.append(int(pair['left']['id']))
            self.assertGreaterEqual(int(pair['left']['id']), 21)
            state = ranker.skip(pair['id'], [])
        self.assertGreater(len(set(anchors)), 75)
        self.assertLess(sum(image_id > 110 for image_id in anchors), 52)

    def test_discovery_covers_newcomers_and_blends_diverse_known_opponents(self):
        ranker = self.ranker(count=160, exploration_fraction=1)
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=10,rating=1000+id WHERE id>120')
        state = ranker.state()
        newcomers, known = set(), set()
        for _ in range(80):
            pair = state['pair']
            for image_id in map(int, self.ids(pair)):
                (newcomers if image_id <= 120 else known).add(image_id)
            state = ranker.vote(pair['id'], pair['left']['id'])
        self.assertGreaterEqual(len(newcomers), 100)
        self.assertGreaterEqual(len(known), 4)

    def test_exhausted_three_image_catalog_uses_least_recent_pair(self):
        ranker = self.ranker(count=3)
        state = ranker.state()
        recent = []
        for turn in range(40):
            pair = state['pair']
            ids = self.ids(pair)
            if turn < 3:
                self.assertNotIn(ids, recent)
            else:
                self.assertEqual(ids, recent[-3], f'not the oldest unavoidable pair at {turn}')
            recent.append(ids)
            state = ranker.skip(pair['id'], [])
        self.assertEqual(state['stats']['votes'], 0)
        self.assertEqual(state['stats']['rated'], 0)

    def test_image_cooldown_relaxes_only_as_far_as_small_pool_requires(self):
        ranker = self.ranker(count=20)
        state = ranker.state()
        recent = []
        for turn in range(80):
            pair = state['pair']
            ids = self.ids(pair)
            forbidden = set(recent[-500:])
            for keep in range(12, -1, -1):
                resting = set().union(*recent[-keep:]) if keep else set()
                cold = [str(value) for value in range(1,21) if str(value) not in resting]
                possible = any(frozenset((first,second)) not in forbidden
                               for index, first in enumerate(cold) for second in cold[index+1:])
                if possible:
                    self.assertTrue(ids.isdisjoint(resting), f'cooldown relaxed excessively at {turn}')
                    self.assertNotIn(ids, forbidden)
                    break
            recent.append(ids)
            state = ranker.skip(pair['id'], [])

    def test_discovery_counts_skipped_presentations_as_exposure(self):
        ranker = self.ranker(exploration_fraction=1)
        state = ranker.state()
        exposed = set()
        for turn in range(60):
            pair = state['pair']
            ids = self.ids(pair)
            self.assertTrue(ids.isdisjoint(exposed), f'ignored under-exposed media at {turn}')
            exposed.update(ids)
            state = ranker.skip(pair['id'], [])
        self.assertEqual(len(exposed), 120)
        self.assertEqual(state['stats']['rated'], 0)
        self.assertEqual(state['stats']['votes'], 0)

    def test_cooldowns_do_not_rotate_a_pending_pair_on_poll_restart_or_scan(self):
        from unittest.mock import patch
        ranker = self.ranker(count=30)
        first = ranker.state()['pair']
        with ranker.connection() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM images ORDER BY id')]
            history = [tuple(row) for row in db.execute('SELECT * FROM pairs ORDER BY rowid')]
        with patch.object(backend.random, 'random', side_effect=AssertionError('pending pair reselected')):
            for _ in range(3):
                self.assertEqual(ranker.state()['pair'], first)
            restarted = self.ranker(count=0, pair_repeat_cooldown=10000,
                                    image_repeat_cooldown=1000, exploration_fraction=0)
            self.assertEqual(restarted.state()['pair'], first)
            (self.archive / first['left']['name']).unlink()
            restarted.scan()
            state = restarted.state()
            self.assertEqual(state['pair'], first)
            self.assertIn('unavailable', state['error'])
        with restarted.connection() as db:
            after = [tuple(row) for row in db.execute('SELECT * FROM images ORDER BY id')]
            self.assertEqual([(row[0],row[4],row[5]) for row in before],
                             [(row[0],row[4],row[5]) for row in after])
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM pairs ORDER BY rowid')], history)

    def test_only_unused_edge_wins_over_cooldowns_with_scope_and_quarantine(self):
        ranker = self.ranker(count=120, exploration_fraction=0)
        with ranker.connection() as db:
            db.execute('UPDATE images SET active=0 WHERE id>30')
            db.execute('''INSERT INTO media_failures(image_id,size,mtime,reason,created)
                SELECT id,size,mtime,415,0 FROM images WHERE id=30''')
            # Viewed but unanswered historical rows count too, independent of
            # timestamps and without pretending they were real votes or skips.
            records = [(f'view-{first}-{second}', first, second, -first)
                       for first in range(1,30) for second in range(first+1,30)
                       if (first,second) != (28,29)]
            db.executemany('INSERT INTO pairs(id,left_id,right_id,created) VALUES(?,?,?,?)', records)
        state = ranker.state()
        self.assertEqual(self.ids(state['pair']), frozenset(('28','29')))
        self.assertEqual(state['stats']['votes'], 0)
        self.assertEqual(state['stats']['rated'], 0)
        self.assertEqual(state['stats']['images'], 30)
        with ranker.connection() as db:
            self.assertEqual(db.execute('SELECT reason FROM media_failures WHERE image_id=30').fetchone()[0], 415)

    def test_history_window_expires_by_presentation_order_not_time(self):
        from unittest.mock import patch
        ranker = self.ranker(count=3, exploration_fraction=0,
                             pair_repeat_cooldown=2, image_repeat_cooldown=0)
        with ranker.connection() as db:
            db.executemany('INSERT INTO pairs(id,left_id,right_id,created) VALUES(?,?,?,?)',
                           [('old',1,2,999999), ('middle',1,3,-1), ('new',2,3,-2)])
        with patch.object(backend.random, 'choice', side_effect=lambda rows:rows[0]):
            first = ranker.state()['pair']
            self.assertEqual(self.ids(first), frozenset(('1','2')))
            second = ranker.skip(first['id'], [])['pair']
            self.assertEqual(self.ids(second), frozenset(('1','3')))

    def test_bounded_selection_queries_use_catalog_and_reverse_history_btrees(self):
        from contextlib import contextmanager
        ranker = self.ranker(count=160)
        with ranker.connection() as db:
            db.execute('UPDATE images SET matches=5 WHERE id>120')
            db.executemany('INSERT INTO pairs(id,left_id,right_id,created) VALUES(?,?,?,?)',
                           ((f'prior-{index}',1,2,0) for index in range(5000)))
        original = ranker.connection
        queries = []
        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(queries.append)
                yield db
        ranker.connection = traced
        for exploration in (1,0):
            ranker.config['exploration_fraction'] = exploration
            with original() as db:
                db.execute("DELETE FROM meta WHERE key='pair'")
            queries.clear()
            ranker.state()
            self.assertFalse(any('random(' in sql.lower() or 'count(' in sql.lower() for sql in queries))
            history = [sql for sql in queries if 'ORDER BY rowid DESC' in sql]
            self.assertEqual(len(history), 1)
            self.assertTrue(history[0].endswith('LIMIT 500'), history)
            with original() as db:
                opcodes = {row['opcode'] for row in db.execute('EXPLAIN ' + history[0])}
                self.assertIn('Last', opcodes)
                self.assertIn('Prev', opcodes)
                self.assertIn('DecrJumpZero', opcodes)
                for sql in queries:
                    if 'FROM images INDEXED BY' not in sql:
                        continue
                    self.assertIn('LIMIT', sql)
                    plan = [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN ' + sql)]
                    self.assertFalse(any('TEMP B-TREE' in detail for detail in plan), (sql,plan))
                    self.assertTrue(any('image_matches' in detail or 'image_rating' in detail or
                                        'rated_leaderboard' in detail for detail in plan), (sql,plan))

    def test_two_images_keep_voting_and_one_image_has_no_pair(self):
        ranker = self.ranker(count=2)
        state = ranker.state()
        for _ in range(20):
            pair = state['pair']
            self.assertEqual(self.ids(pair), frozenset(('1','2')))
            state = ranker.vote(pair['id'], pair['left']['id'])
        self.assertEqual(state['stats']['votes'], 20)
        with ranker.connection() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT matches FROM images ORDER BY id')], [20,20])
            db.execute("DELETE FROM meta WHERE key='pair'")
            db.execute('UPDATE images SET active=0 WHERE id=2')
        self.assertIsNone(ranker.state()['pair'])

    def test_defaults_and_finite_integer_configuration(self):
        validated = backend.validate_config(self.config)
        self.assertEqual(validated['exploration_fraction'], .9)
        self.assertEqual(validated['pair_repeat_cooldown'], 500)
        self.assertEqual(validated['image_repeat_cooldown'], 12)
        self.assertEqual(validated['refinement_pool_size'], 100)
        for key, lower, upper in [('pair_repeat_cooldown', 0, 10000),
                                  ('image_repeat_cooldown', 0, 1000),
                                  ('refinement_pool_size', 2, 1000)]:
            for value in [True, False, None, '12', [], {}, 1.5, float('nan'),
                          float('inf'), lower - 1, upper + 1]:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    backend.validate_config(dict(self.config, **{key: value}))
            for value in [lower, upper]:
                self.assertEqual(backend.validate_config(dict(self.config, **{key:value}))[key], value)
        self.assertEqual(backend.validate_config(dict(self.config, exploration_fraction=.65))['exploration_fraction'], .65)


if __name__ == '__main__':
    unittest.main()
