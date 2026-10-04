"""Real muted playback/range integration using isolated generated video fixtures."""
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
spec = importlib.util.spec_from_file_location('video_integration_backend', PROJECT / 'server.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class VideoIntegrationTests(unittest.TestCase):
    def test_real_http_video_playback_ranges_vote_and_hide(self):
        with tempfile.TemporaryDirectory(prefix='ranker-video-integration-', dir=SCRATCH) as tmp:
            root = Path(tmp)
            archive, project = root / 'archive', root / 'project'
            archive.mkdir()
            project.mkdir()
            shutil.copytree(PROJECT / 'widget', project / 'widget')
            (archive / 'tag-one').mkdir()
            (archive / 'tag-two').mkdir()
            shutil.copy2(PROJECT / 'tests/fixtures/muted-test.mp4', archive / 'tag-one/clip.mp4')
            os.link(archive / 'tag-one/clip.mp4', archive / 'tag-two/clip.mp4')
            shutil.copy2(PROJECT / 'tests/fixtures/muted-test.mp4', archive / 'other.mp4')
            config = json.loads((PROJECT / 'config.json').read_text())
            config.update(archive_root=str(archive), settle_seconds=0, include_videos=True)
            ranker = backend.Ranker(config, project)
            ranker.scan()
            self.assertEqual(ranker.state()['stats']['images'], 2)
            self.assertEqual(archive.joinpath('tag-one/clip.mp4').stat().st_nlink, 2)
            server = backend.make_server(ranker, 0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                env = dict(os.environ, RANKER_TEST_URL=f'http://127.0.0.1:{server.server_port}', RANKER_TEST_TOKEN=config['api_token'])
                result = subprocess.run(['node', str(PROJECT / 'tests/media-integration.cjs')], cwd=PROJECT, env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout.strip())
                state = ranker.state()
                self.assertEqual(state['stats']['votes'], 4)
                self.assertEqual(len(state['top']), 2)
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
