"""Build an allowlisted runtime archive. Never includes historical tasks or media."""
import hashlib
import json
from pathlib import Path
import tarfile

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
DEST = WORKSPACE / '.cache/daydreamer-deploy'


def build():
    DEST.mkdir(parents=True, exist_ok=True)
    files = {}
    for directory in ('src', 'config', 'prompts', 'schemas', 'resources/daydreamer-style-transfer'):
        for path in (PROJECT / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                files['project/' + path.relative_to(PROJECT).as_posix()] = path
    for name in ('daydreamer.py', 'pyproject.toml'):
        files['project/' + name] = PROJECT / name
    front = WORKSPACE / '网页交互端'
    for name in ('index.html', 'app.js', 'cloud-shape.js', 'styles.css'):
        files['web/' + name] = front / name
    for path in (front / '素材').rglob('*'):
        if path.is_file() and path.suffix.lower() in {'.png', '.svg', '.webp'}:
            files['web/' + path.relative_to(front).as_posix()] = path
    for path in (PROJECT / 'deploy/web').iterdir():
        if path.is_file():
            files['web/' + path.name] = path
    for path in (PROJECT / 'deploy').iterdir():
        if path.suffix in {'.service', '.timer', '.conf', '.sh'}:
            files['deploy/' + path.name] = path
    manifest = [{"path": name, "bytes": path.stat().st_size,
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for name, path in sorted(files.items())]
    (DEST / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    with tarfile.open(DEST / 'runtime.tar.gz', 'w:gz') as archive:
        for name, path in sorted(files.items()):
            archive.add(path, arcname=name, recursive=False)
        archive.add(DEST / 'manifest.json', arcname='manifest.json')
    print(json.dumps({"files": len(files), "archive_bytes": (DEST / 'runtime.tar.gz').stat().st_size,
                      "skill_files": [name for name in files if '/resources/daydreamer-style-transfer/' in name]}, ensure_ascii=False))


if __name__ == '__main__':
    build()
