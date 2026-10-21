#!/usr/bin/env python3
"""Package current application sources for the existing independent T113 runtime."""

import argparse
import ast
import hashlib
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

DEPLOY = Path(__file__).resolve().parent
ROOT = DEPLOY.parents[1]
TOUCH_HOOK = '''    if os.environ.get("QT5COM_TOUCH_POLL") == "1":
        from touch_poll import FocalTechTouchPoller
        w.touch_poller = FocalTechTouchPoller(w)
        w.touch_poller.start()
        app.aboutToQuit.connect(w.touch_poller.stop)
        print("FocalTech touch polling fallback enabled", flush=True)
'''


def make_package(output_dir=None):
    sys.path.insert(0, str(ROOT))
    from app_meta import read_version

    version = read_version(ROOT / 'VERSION')
    output_dir = Path(output_dir) if output_dir is not None else ROOT / 'dist' / 't113'
    output_dir.mkdir(parents=True, exist_ok=True)
    name = 'DebugTool-{}-t113-update'.format(version)
    archive = output_dir / (name + '.tar.gz')
    files = (DEPLOY / 'app_files.txt').read_text(encoding='utf-8').splitlines()
    with tempfile.TemporaryDirectory(prefix='debugtool-t113-') as temporary:
        package = Path(temporary) / name
        app = package / 'app'
        app.mkdir(parents=True)
        for filename in files:
            if not filename or filename.startswith('#'):
                continue
            if Path(filename).name != filename:
                raise ValueError('Application manifest must contain plain file names: ' + filename)
            shutil.copy2(ROOT / filename, app / filename)

        # Keep the shared desktop sources unchanged. This exact board-only
        # overlay retains the touch entry already field-tested on this T113.
        main_file = app / 'main.py'
        source = main_file.read_text(encoding='utf-8')
        anchor = '    sys.exit(app.exec_())\n'
        if source.count(anchor) != 1 or 'from touch_poll import' in source:
            raise ValueError('main() changed; review the T113 touch overlay before packaging')
        main_file.write_text(source.replace(anchor, TOUCH_HOOK + anchor), encoding='utf-8')
        shutil.copy2(DEPLOY / 'touch_poll.py', app / 'touch_poll.py')

        for source_file in app.glob('*.py'):
            ast.parse(source_file.read_text(encoding='utf-8'), filename=str(source_file), feature_version=8)

        (package / 'bin').mkdir()
        for filename in ('qt5com', 'python'):
            shutil.copy2(DEPLOY / filename, package / 'bin' / filename)
        for filename in ('fonts.conf', 'qt5com.service', 'README.md',
                         'TOUCH_DIAGNOSTICS.md', 'update.sh', 'app_files.txt'):
            shutil.copy2(DEPLOY / filename, package / filename)
        (package / 'update.sh').chmod(0o755)
        checksums = []
        for file in sorted(path for path in package.rglob('*') if path.is_file()):
            checksum = hashlib.sha256(file.read_bytes()).hexdigest()
            checksums.append('{}  {}\n'.format(checksum, file.relative_to(package).as_posix()))
        (package / 'SHA256SUMS').write_text(''.join(checksums), encoding='utf-8')
        with tarfile.open(str(archive), 'w:gz') as output:
            output.add(str(package), arcname=name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + '.sha256').write_text(
        '{}  {}\n'.format(checksum, archive.name), encoding='utf-8')
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='Output directory (default: dist/t113)')
    args = parser.parse_args()
    print(make_package(args.output_dir))


if __name__ == '__main__':
    main()
