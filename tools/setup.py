"""Generate ignored local configuration; never put per-machine secrets in public source."""
import json
import argparse
from pathlib import Path
import secrets
import shutil
import subprocess
import sys


def validate_local(config):
    if not isinstance(config, dict):
        raise ValueError('Local configuration must be a JSON object.')
    token = config.get('api_token')
    if (not isinstance(token, str) or not 32 <= len(token) <= 256
            or len(set(token)) < 8
            or not all(c.isascii() and (c.isalnum() or c in '-_') for c in token)):
        raise ValueError('Local configuration needs a strong URL-safe API key.')
    root = config.get('archive_root')
    if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
        raise ValueError('Archive root must be an existing absolute directory.')
    port = config.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('Port must be an integer between 1 and 65535.')


def initialize(project, archive_root=None):
    project = Path(project).resolve()
    config_path = project / 'config.json'
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))
    else:
        if archive_root is None:
            raise ValueError('Pass --archive-root for the first setup.')
        archive = Path(archive_root).expanduser().resolve()
        if not archive.is_dir():
            raise ValueError('Archive root must be an existing directory.')
        config = json.loads((project / 'config.example.json').read_text(encoding='utf-8'))
        config['archive_root'] = str(archive)
        config['api_token'] = secrets.token_urlsafe(32)
        validate_local(config)
        with config_path.open('x', encoding='utf-8') as file:
            json.dump(config, file, indent=2)
            file.write('\n')
    validate_local(config)
    defaults = {'endpoint': 'http://127.0.0.1:' + str(config['port']), 'apiToken': config['api_token']}
    widget = project / 'widget'
    widget.mkdir(exist_ok=True)
    target = widget / 'defaults.js'
    content = 'window.ImageRankerDefaults = ' + json.dumps(defaults) + ';\n'
    if not target.exists() or target.read_text(encoding='utf-8') != content:
        target.write_text(content, encoding='utf-8')
    return config


def generate_fixture(project):
    destination = Path(project) / 'tests/fixtures/muted-test.mp4'
    if destination.is_file():
        return destination
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        raise ValueError('Install FFmpeg on PATH to generate test fixtures.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('muted-test.part.mp4')
    try:
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'color=c=teal:s=160x90:r=10',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                        '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-movflags', '+faststart', str(temporary)],
                       check=True, capture_output=True, timeout=45)
        temporary.replace(destination)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Could not generate fixture; install an FFmpeg build with libx264 and AAC.') from None
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main(argv=None, *, project_dir=None):
    parser = argparse.ArgumentParser(description='Create private local ranker settings without overwriting existing configuration.')
    parser.add_argument('--archive-root', type=Path, help='Existing media archive directory, required for first setup')
    parser.add_argument('--fixtures-only', action='store_true', help='Generate synthetic test media without touching private configuration')
    args = parser.parse_args(argv)
    project = Path(project_dir) if project_dir is not None else Path(__file__).resolve().parents[1]
    try:
        if args.fixtures_only:
            generate_fixture(project)
            print('Synthetic test fixture ready; no archive media was used.')
            return 0
        initialize(project, args.archive_root)
        print('Local configuration ready; existing settings preserved. Private widget defaults generated.')
        return 0
    except (OSError, ValueError) as error:
        print('Setup failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
