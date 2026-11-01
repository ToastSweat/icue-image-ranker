"""Recovery UI + real HTTP integration; all votes/media are isolated fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
spec = importlib.util.spec_from_file_location('recovery_integration_backend', PROJECT / 'server.py')
assert spec is not None and spec.loader is not None
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class RecoveryIntegrationTests(unittest.TestCase):
    def test_two_clients_recover_without_votes_or_source_deletion(self):
        with tempfile.TemporaryDirectory(prefix='ranker-recovery-integration-', dir=SCRATCH) as tmp:
            root = Path(tmp)
            archive, project = root / 'archive', root / 'project'
            archive.mkdir()
            project.mkdir()
            shutil.copytree(PROJECT / 'widget', project / 'widget')
            Image.new('RGB', (672, 1000), '#477c92').save(archive / 'bitmap-named-jpeg.jpg', format='BMP')
            (archive / 'unreadable.jpg').write_bytes(b'unreadable synthetic fixture')
            config = json.loads((PROJECT / 'config.json').read_text())
            config.update(archive_root=str(archive), settle_seconds=0)
            ranker = backend.Ranker(config, project)
            ranker.scan()
            original = ranker.state()['pair']
            Image.new('RGB', (640, 480), '#916d42').save(archive / 'second-good.png')
            ranker.scan()
            self.assertEqual(ranker.state()['pair']['id'], original['id'])
            server = backend.make_server(ranker, 0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                env = dict(os.environ, RANKER_TEST_URL=f'http://127.0.0.1:{server.server_port}', RANKER_TEST_TOKEN=config['api_token'])
                result = subprocess.run(['node', str(PROJECT / 'tests/recovery-integration.cjs')], cwd=PROJECT, env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout.strip())
                state = ranker.state()
                self.assertEqual(state['stats']['votes'], 0)
                self.assertEqual(state['stats']['rated'], 0)
                self.assertEqual((archive / 'unreadable.jpg').read_bytes(), b'unreadable synthetic fixture')
                with ranker.connection() as db:
                    self.assertEqual(db.execute('SELECT sum(matches) FROM images').fetchone()[0], 0)
                restored = backend.Ranker(config, project)
                try:
                    self.assertEqual(restored.state(), state)
                finally:
                    restored.close()
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()
                ranker.close()


if __name__ == '__main__':
    unittest.main()
