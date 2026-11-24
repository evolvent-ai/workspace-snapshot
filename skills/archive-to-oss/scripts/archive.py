#!/usr/bin/env python3
"""Archive explicit conversation paths and upload using existing ossutil credentials."""
import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oss_write_only import OSS, load_credentials

DEFAULT_DEST = 'oss://seed-data/workspace/'
DEFAULT_ENDPOINT = 'https://oss-cn-hangzhou.aliyuncs.com'
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.cache',
             '.pytest_cache', '.mypy_cache', '.next', '.turbo', '.ssh', '.aws', '.aliyun'}
SECRET_NAMES = {'.env', '.npmrc', '.pypirc', '.netrc', '.ossutilconfig',
                'credentials', 'id_rsa', 'id_ed25519', 'id_ecdsa'}
SECRET_PATTERNS = ['*.pem', '*.key', '*.p12', '*.pfx', '.env.*', '*credentials*.json',
                   '*service-account*.json', '*secret*.env']


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def secret(path):
    name = path.name.lower()
    if name in {'.env.example', '.env.sample', '.env.template'}:
        return False
    return (name in SECRET_NAMES or any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)
            or any(p.lower() in {'.ssh', '.aws', '.aliyun'} for p in path.parts))


def fingerprint(s):
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def roots_from(values):
    roots = []
    for value in values:
        p = Path(os.path.abspath(os.path.expanduser(value)))
        if p.is_symlink() or any(a.is_symlink() for a in p.parents):
            raise ValueError('Source symlink is not followed: ' + str(p))
        if not p.exists():
            raise ValueError('Source does not exist: ' + str(p))
        if not (p.is_file() or p.is_dir()):
            raise ValueError('Unsupported source: ' + str(p))
        if p == Path(p.anchor) or p == Path.home():
            raise ValueError('Choose specific files or repositories rather than root/home')
        roots.append(p)
    roots = sorted(set(roots), key=lambda p: (len(p.parts), str(p)))
    return [p for i, p in enumerate(roots) if not any(q in p.parents for q in roots[:i])]


def make_plan(values, include_git=False, excludes=()):
    roots = roots_from(values)
    files, skipped, sources = [], [], []
    def visit(p, root, label):
        relative = p.relative_to(root) if root.is_dir() else Path(p.name)
        arc = str(Path('sources') / label / relative)
        reason = None
        if p.is_symlink(): reason = 'symlink'
        elif secret(p): reason = 'credential filename'
        elif p.name in SKIP_DIRS and not (p.name == '.git' and include_git): reason = 'dependency/cache/git'
        elif any(fnmatch.fnmatch(str(relative), x) for x in excludes): reason = 'user exclusion'
        if reason:
            skipped.append({'path': str(p), 'reason': reason})
        elif p.is_dir():
            for child in sorted(p.iterdir()): visit(child, root, label)
        elif p.is_file():
            files.append({'source': str(p), 'archive': arc, 'fingerprint': fingerprint(p.stat())})
        else:
            skipped.append({'path': str(p), 'reason': 'not a regular file'})
    for i, root in enumerate(roots, 1):
        label = '%02d-%s' % (i, root.name)
        sources.append({'path': str(root), 'archive_root': 'sources/' + label})
        visit(root, root, label)
    if not files: raise ValueError('No eligible files to archive')
    return {'format': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'sources': sources, 'files': files, 'skipped': skipped,
            'file_count': len(files), 'total_bytes': sum(f['fingerprint'][2] for f in files),
            'include_git': include_git}


def build(plan, output):
    output = Path(output).expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never create an output inside a selected source tree.
    for src in plan['sources']:
        p = Path(src['path'])
        if p == output or p in output.parents:
            raise ValueError('Archive output must be outside all selected source paths')
    if output.exists(): raise ValueError('Output already exists: ' + str(output))
    manifest = {k: v for k, v in plan.items() if k != 'files'}
    manifest['files'] = []
    partial = output.with_name(output.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            os.chmod(partial, 0o600)
            for entry in plan['files']:
                p = Path(entry['source'])
                if p.is_symlink() or any(a.is_symlink() for a in p.parents):
                    raise ValueError('Source became a symlink: ' + str(p))
                fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or fingerprint(before) != entry['fingerprint']:
                        raise ValueError('Source changed after plan; re-plan: ' + str(p))
                    info = zipfile.ZipInfo.from_file(p, arcname=entry['archive'], strict_timestamps=False)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    digest = hashlib.sha256()
                    with z.open(info, 'w', force_zip64=True) as target:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block: break
                            digest.update(block)
                            target.write(block)
                    if fingerprint(os.fstat(source.fileno())) != entry['fingerprint']:
                        raise ValueError('Source changed during archive: ' + str(p))
                manifest['files'].append({'source': str(p), 'archive': entry['archive'],
                                          'bytes': before.st_size, 'sha256': digest.hexdigest()})
            z.writestr('MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(partial) as z:
            if z.testzip() is not None: raise ValueError('Archive verification failed')
        # Link is atomic and refuses to overwrite an existing destination.
        os.link(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return {'archive': str(output), 'bytes': output.stat().st_size, 'sha256': sha256(output),
            'file_count': plan['file_count'], 'skipped_count': len(plan['skipped'])}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def run(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        # Avoid echoing potentially sensitive CLI diagnostics into chat.
        raise RuntimeError('ossutil failed (exit %d); check local credentials, endpoint and permissions' % result.returncode)
    return result.stdout


def upload(archive, destination=DEFAULT_DEST, endpoint=DEFAULT_ENDPOINT):
    if not re.fullmatch(r'oss://[a-z0-9][a-z0-9-]{1,61}[a-z0-9](?:/[^\s?#]*)?/?', destination):
        raise ValueError('Destination must be oss://bucket/prefix/')
    if not re.fullmatch(r'https://[a-z0-9.-]+', endpoint):
        raise ValueError('Use a verified HTTPS OSS endpoint')
    config = Path(os.environ.get('CONVERSATION_OSS_CREDENTIAL_FILE',
        str(Path.home() / '.local/share/conversation-oss-archive/private/upload-credentials.json')))
    packaged = Path(__file__).resolve().parents[3] / 'config' / 'oss-upload.json'
    if packaged.is_file(): config = packaged
    credentials = load_credentials(config)
    archive = Path(archive).expanduser().resolve(strict=True)
    if not zipfile.is_zipfile(archive): raise ValueError('Expected a ZIP archive')
    digest = sha256(archive)
    day = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y/%m/%d')
    key = '%s/%s-%s.zip' % (day, dt.datetime.now(dt.timezone.utc).strftime('%H%M%S'), uuid.uuid4().hex)
    uri = destination.rstrip('/') + '/' + key
    parsed = urllib.parse.urlsplit(uri)
    verification = OSS(credentials, endpoint).put_file(archive, parsed.netloc, parsed.path.lstrip('/'), digest)
    receipt = {'oss_uri': uri, 'endpoint': endpoint, 'acl': 'private', 'sha256': digest,
               'bytes': archive.stat().st_size, 'archive': str(archive),
               'uploaded_at': dt.datetime.now(dt.timezone.utc).isoformat(), **verification}
    dump(archive.with_suffix('.upload.json'), receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--paths-json', required=True, help='JSON array of explicit absolute paths')
    p.add_argument('--output', required=True)
    p.add_argument('--include-git', action='store_true')
    p.add_argument('--exclude', action='append', default=[])
    b = sub.add_parser('build')
    b.add_argument('--plan', required=True)
    b.add_argument('--output', required=True)
    u = sub.add_parser('upload')
    u.add_argument('--archive', required=True)
    u.add_argument('--destination', default=os.environ.get('CONVERSATION_OSS_DESTINATION', DEFAULT_DEST))
    u.add_argument('--endpoint', default=os.environ.get('CONVERSATION_OSS_ENDPOINT', DEFAULT_ENDPOINT))
    args = parser.parse_args()
    if args.command == 'plan':
        values = json.loads(Path(args.paths_json).read_text(encoding='utf-8'))
        if not isinstance(values, list) or not values or not all(isinstance(p, str) and p for p in values):
            raise ValueError('paths-json must contain a nonempty JSON array of paths')
        result = make_plan(values, args.include_git, args.exclude)
        dump(Path(args.output), result)
        result = {k: v for k, v in result.items() if k != 'files'}
    elif args.command == 'build':
        result = build(json.loads(Path(args.plan).read_text(encoding='utf-8')), args.output)
    else:
        result = upload(args.archive, args.destination, args.endpoint)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
