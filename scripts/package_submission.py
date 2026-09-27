"""Package committed sources and built Sphinx HTML without runtime state."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Create a delivery ZIP with commit and per-file SHA256 manifest."""
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip():
        raise SystemExit('Commit changes before packaging: worktree must be clean.')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    files = [ROOT / name for name in names if name]
    html = ROOT / 'docs/_build/html'
    if not (html / 'index.html').is_file():
        raise SystemExit('Build Sphinx HTML before packaging.')
    files.extend(p for p in html.rglob('*') if p.is_file() and '.doctrees' not in p.parts)
    manifest = {'commit': commit, 'files': {}}
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    archive = output / f'takt-submission-{commit[:12]}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(set(files)):
            relative = path.relative_to(ROOT)
            if path.is_symlink() or relative.parts[0] in {'state', 'dist', '.git', '.venv'}:
                raise SystemExit(f'Unexpected runtime/private path: {relative}')
            data = path.read_bytes()
            manifest['files'][relative.as_posix()] = hashlib.sha256(data).hexdigest()
            bundle.writestr('takt/' + relative.as_posix(), data)
        bundle.writestr('takt/SUBMISSION-MANIFEST.json', json.dumps(manifest, indent=2))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n')
    print(f'{archive}\nSHA256 {checksum}\nCommit {commit}')


if __name__ == '__main__':
    main()
