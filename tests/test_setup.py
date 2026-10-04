"""Portable setup regression tests using isolated folders and synthetic configuration."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())
path = PROJECT / 'tools/setup.py'
spec = importlib.util.spec_from_file_location('ranker_setup', path) if path.exists() else None
setup = importlib.util.module_from_spec(spec) if spec else None
if spec:
    spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ranker-public-setup-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        (self.root / 'widget').mkdir()
        shutil.copy2(PROJECT / 'config.example.json', self.root / 'config.example.json')

    def test_command_line_setup_reports_success_without_printing_key(self):
        self.assertTrue(callable(getattr(setup, 'main', None)), 'setup CLI must exist')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = setup.main(['--archive-root', str(self.archive)], project_dir=self.root)
        self.assertEqual(result, 0)
        token = json.loads((self.root / 'config.json').read_text())['api_token']
        self.assertNotIn(token, output.getvalue())

    def test_generated_fixture_has_video_and_audio_without_private_config(self):
        self.assertTrue(callable(getattr(setup, 'generate_fixture', None)), 'fixture generator must exist')
        fixture = setup.generate_fixture(self.root)
        first = fixture.read_bytes()
        self.assertEqual(setup.generate_fixture(self.root).read_bytes(), first)
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(fixture)], capture_output=True, text=True, check=True)
        kinds = {stream['codec_type'] for stream in json.loads(result.stdout)['streams']}
        self.assertEqual(kinds, {'video', 'audio'})
        self.assertFalse((self.root / 'config.json').exists())

    def test_existing_configuration_is_not_overwritten_or_rotated(self):
        self.assertIsNotNone(setup)
        config = json.loads((self.root / 'config.example.json').read_text())
        config.update(archive_root=str(self.archive.resolve()), api_token='setup-test-only-private-key-012345abcdef', port=47900)
        encoded = json.dumps(config, indent=4) + '\n'
        (self.root / 'config.json').write_text(encoded)
        try:
            setup.initialize(self.root, None)
        except (TypeError, FileExistsError):
            self.fail('Setup must reuse existing configuration without requiring a new archive path')
        self.assertEqual((self.root / 'config.json').read_text(), encoded)
        generated = (self.root / 'widget/defaults.js').read_text()
        self.assertIn(config['api_token'], generated)
        self.assertIn('http://127.0.0.1:47900', generated)

    def test_invalid_existing_key_is_rejected_without_writing_defaults(self):
        self.assertIsNotNone(setup)
        config = {'archive_root': str(self.archive.resolve()), 'port': 47831, 'api_token': 'weak'}
        encoded = json.dumps(config)
        (self.root / 'config.json').write_text(encoded)
        with self.assertRaises(ValueError):
            setup.initialize(self.root)
        self.assertFalse((self.root / 'widget/defaults.js').exists())
        self.assertEqual((self.root / 'config.json').read_text(), encoded)

    def test_new_checkout_creates_private_config_and_local_widget_defaults(self):
        self.assertIsNotNone(setup, 'portable setup helper must exist')
        setup.initialize(self.root, self.archive)
        config = json.loads((self.root / 'config.json').read_text())
        self.assertEqual(config['archive_root'], str(self.archive.resolve()))
        self.assertGreaterEqual(len(config['api_token']), 32)
        self.assertNotEqual(config['api_token'], '')
        self.assertIn(config['api_token'], (self.root / 'widget/defaults.js').read_text())
        self.assertEqual(json.loads((self.root / 'config.example.json').read_text())['api_token'], '')


if __name__ == '__main__':
    unittest.main()
