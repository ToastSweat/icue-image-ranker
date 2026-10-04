"""Verify repository privacy rules in a disposable Git repository, not the user's index."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or tempfile.gettempdir())


class PrivacyTests(unittest.TestCase):
    def test_private_outputs_are_ignored_but_public_templates_remain_available(self):
        with tempfile.TemporaryDirectory(prefix='ranker-git-privacy-', dir=SCRATCH) as tmp:
            folder = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(folder)], check=True)
            shutil.copy2(PROJECT / '.gitignore', folder / '.gitignore')
            private = ['config.json', 'widget/defaults.js', 'data/rankings.sqlite3',
                       'rankings.sqlite3-wal', 'rankings.sqlite3-shm',
                       'data/backups/private.sqlite3', '.venv/Scripts/python.exe',
                       'node_modules/dependency/index.js', 'dist/local.icuewidget',
                       'build/widget/index.html', 'image-rankings.json',
                       'exports/preferences.json', 'binary.exe', 'library.dll',
                       '.env', 'nested/secrets.json', 'widget/resources/private.key',
                       'tests/fixtures/muted-test.mp4', 'verification/results.json',
                       'CONTRACT.md', 'MEDIA-CONTRACT.md', 'Start Image Ranker.cmd']
            public = ['.gitignore', 'README.md', 'config.example.json',
                      'widget/index.html', 'widget/defaults.example.js',
                      'widget/resources/icon.svg', 'widget/resources/preview.png',
                      'start.cmd', 'package-lock.json', 'requirements.txt',
                      'tests/fixtures/README.md', 'tools/setup.py']
            for name in private + public:
                result = subprocess.run(['git', '-C', str(folder), 'check-ignore', '-q', '--', name])
                expected = 0 if name in private else 1
                self.assertEqual(result.returncode, expected, name)


if __name__ == '__main__':
    unittest.main()
