"""Exercise real HTTP backend + packaged widget together without real archive votes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
spec = importlib.util.spec_from_file_location('integration_backend', PROJECT / 'server.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class IntegrationTests(unittest.TestCase):
    def test_real_http_widget_votes_privacy_export_and_restart(self):
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix='ranker-integration-', dir=SCRATCH) as tmp:
            root = Path(tmp)
            archive, project = root / 'archive', root / 'project'
            archive.mkdir()
            project.mkdir()
            shutil.copytree(PROJECT / 'widget', project / 'widget')
            for i, color in enumerate(['#426985', '#916d42', '#568264', '#886485', '#795b60', '#397e80', '#a38345', '#525a85']):
                Image.new('RGB', (640 + i * 13, 480 + i * 7), color).save(archive / f'fixture-{i}.png')
            config = json.loads((PROJECT / 'config.json').read_text())
            config.update(archive_root=str(archive), settle_seconds=0)
            ranker = backend.Ranker(config, project)
            ranker.scan()
            server = backend.make_server(ranker, 0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                env = dict(os.environ, RANKER_TEST_URL=f'http://127.0.0.1:{server.server_port}', RANKER_TEST_TOKEN=config['api_token'])
                result = subprocess.run(['node', str(PROJECT / 'tests' / 'integration-browser.cjs')], cwd=PROJECT, env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout.strip())
                state = ranker.state()
                self.assertEqual(state['stats']['votes'], 12)
                self.assertEqual(len(state['top']), min(state['stats']['rated'], 100))
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
