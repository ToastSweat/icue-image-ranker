"""Phone gateway + real isolated ranker integration; no live rankings are used."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
spec = importlib.util.spec_from_file_location('phone_integration_backend', PROJECT / 'server.py')
assert spec is not None and spec.loader is not None
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class PhoneIntegrationTests(unittest.TestCase):
    def test_phone_pairing_real_media_vote_peer_sync_logout_and_key_isolation(self):
        with tempfile.TemporaryDirectory(prefix='ranker-phone-integration-', dir=SCRATCH) as tmp:
            root = Path(tmp)
            archive, project = root / 'archive', root / 'project'
            archive.mkdir()
            project.mkdir()
            shutil.copytree(PROJECT / 'widget', project / 'widget')
            Image.new('RGB', (640, 480), '#477c92').save(archive / 'first.png')
            shutil.copy2(PROJECT / 'tests/fixtures/muted-test.mp4', archive / 'clip.mp4')
            config = {'archive_root': str(archive), 'api_token': 'phone-test-only-core-key-0123456789abcdef',
                      'settle_seconds': 0, 'port': 47831, 'include_videos': True}
            ranker = backend.Ranker(config, project)
            ranker.scan()
            first = ranker.state()['pair']
            for i in range(4):
                Image.new('RGB', (320, 240), (40 + i * 20, 80, 100)).save(archive / f'extra-{i}.png')
            ranker.scan()
            self.assertEqual(ranker.state()['pair']['id'], first['id'])
            core = backend.make_server(ranker, 0)
            core_thread = threading.Thread(target=core.serve_forever)
            core_thread.start()
            config['port'] = core.server_port
            config_file = project / 'config.json'
            config_file.write_text(json.dumps(config))
            state_file = project / 'data/phone-access.json'
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                phone_port = reservation.getsockname()[1]
            process = None
            try:
                process = subprocess.Popen(['node', str(PROJECT / 'tools/phone-server.cjs'),
                    '--bind', '127.0.0.1', '--subnet', '127.0.0.0/8', '--port', str(phone_port),
                    '--config', str(config_file), '--state-file', str(state_file)],
                    cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                deadline = time.monotonic() + 15
                while not state_file.is_file() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                if not state_file.is_file():
                    if process.poll() is None:
                        process.terminate()
                    output, errors = process.communicate(timeout=5)
                    self.fail('Phone gateway did not become ready: ' + output + errors)
                status = json.loads(state_file.read_text())
                env = dict(os.environ, PHONE_TEST_URL=f'http://127.0.0.1:{phone_port}',
                    CORE_TEST_URL=f'http://127.0.0.1:{core.server_port}',
                    CORE_TEST_TOKEN=config['api_token'], PHONE_TEST_CODE=str(status['pairing_code']))
                result = subprocess.run(['node', str(PROJECT / 'tests/phone-integration.cjs')],
                    cwd=PROJECT, env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout.strip())
                self.assertEqual(ranker.state()['stats']['votes'], 1)
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)
                core.shutdown()
                core_thread.join(5)
                core.server_close()
                ranker.close()


if __name__ == '__main__':
    unittest.main()
