"""Read-only check of publishable files and reachable Git history; never print secret values.

This detects known credential formats and accidental state files, not arbitrary private facts.
Run from the repository root: python -E -m scripts.check_secrets
"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess


PATTERNS = {
    'private_key': rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----',
    'model_api_key': rb'\bsk-(?:or-v1-|proj-|ant-api\d+-)?[A-Za-z0-9_-]{32,}',
    'github_token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})',
    'aws_access_key': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
}
MAX_BYTES = 20 * 1024 * 1024


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


def private_path(path):
    parts = PurePosixPath(path).parts
    return any(p in {'data', '.aws', '.ssh', '.codex', '.venv', '.bootstrap'} for p in parts) or any(
        (p == '.env' or p.startswith('.env.')) and p != '.env.example' for p in parts
    ) or path.endswith(('.sqlite', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm'))


def local_credentials(root):
    # Compare the configured literal without including it in reports or subprocess arguments.
    values = [os.environ.get('APP_MODEL_API_KEY', '')]
    env = root / '.env'
    if env.is_file():
        for line in env.read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?APP_MODEL_API_KEY\s*=\s*(.*)', line)
            if match:
                words = shlex.split(match[1], comments=True)
                if words:
                    values.append(words[0])
    return [value.encode() for value in values if len(value) >= 8]


def scan(root, *, credentials=()):
    root = Path(root).resolve()
    findings = []

    def inspect(data, location):
        if len(data) > MAX_BYTES:
            findings.append(dict(location=location, rule='file_too_large_to_check'))
            return
        for rule, pattern in PATTERNS.items():
            if re.search(pattern, data):
                findings.append(dict(location=location, rule=rule))
        if any(value in data for value in credentials):
            findings.append(dict(location=location, rule='configured_credential'))
        if data.startswith(b'SQLite format 3\x00'):
            findings.append(dict(location=location, rule='database_content'))

    files = set(git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''}
    for encoded in sorted(files):
        name = os.fsdecode(encoded)
        if private_path(name):
            findings.append(dict(location=name, rule='private_path'))
        path = root / name
        if path.is_symlink():
            inspect(os.readlink(path).encode(), name)  # Never follow a link outside the checkout.
        elif path.is_file():
            with path.open('rb') as stream:
                inspect(stream.read(MAX_BYTES + 1), name)

    commits = git(root, 'rev-list', '--all').splitlines()
    blobs = {}
    for row in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if row:
            metadata, path = row.split(b'\t', 1)
            mode, oid, _ = metadata.split()
            if mode != b'160000' and oid.strip(b'0'):
                blobs.setdefault(oid, 'index:' + os.fsdecode(path))
    for commit in commits:
        for row in git(root, 'ls-tree', '-r', '-z', commit.decode()).split(b'\0'):
            if not row:
                continue
            metadata, path = row.split(b'\t', 1)
            _, kind, oid = metadata.split()
            name = os.fsdecode(path)
            location = commit.decode()[:12] + ':' + name
            if private_path(name):
                findings.append(dict(location=location, rule='private_path'))
            if kind == b'blob':
                blobs.setdefault(oid, location)
    for oid, location in blobs.items():
        if int(git(root, 'cat-file', '-s', oid.decode())) > MAX_BYTES:
            findings.append(dict(location=location, rule='file_too_large_to_check'))
        else:
            inspect(git(root, 'cat-file', 'blob', oid.decode()), location)
    return dict(files=len(files), commits=len(commits), git_blobs=len(blobs), findings=findings)


def main():
    try:
        root = Path(os.fsdecode(git(Path.cwd(), 'rev-parse', '--show-toplevel')).strip())
        result = scan(root, credentials=local_credentials(root))
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('Secret check could not complete; no input contents were printed.')
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result['findings']))


if __name__ == '__main__':
    raise SystemExit(main())
