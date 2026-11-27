#!/usr/bin/env python3
"""Build the private portable plugin; requires publisher Python 3.12+ and Go."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {'aarch64-apple-darwin':'darwin-arm64',
             'x86_64-apple-darwin':'darwin-amd64',
             'x86_64-pc-windows-msvc':'windows-amd64'}


def fetch_runtime(asset, cache):
    archive = cache / asset['name']
    if not archive.exists():
        temporary = archive.with_suffix('.partial')
        request = urllib.request.Request(asset['url'], headers={'User-Agent':'workspace-snapshot-build'})
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output)
        temporary.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if asset['digest'] != 'sha256:' + digest:
        raise ValueError('Runtime checksum mismatch: ' + asset['target'])
    destination = cache / PLATFORMS[asset['target']]
    if not (destination / 'python').exists():
        destination.mkdir(exist_ok=True)
        with tarfile.open(archive, 'r:gz') as source:
            source.extractall(destination, filter='data')
    return PLATFORMS[asset['target']], destination


def trim_runtime(platform, folder):
    home = folder / 'python'
    library = home / ('Lib' if platform.startswith('windows') else 'lib/python3.12')
    for name in ['ensurepip','idlelib','tkinter','turtledemo','test']:
        shutil.rmtree(library / name, ignore_errors=True)
    site = library / 'site-packages'
    if site.exists():
        for path in site.iterdir():
            if path.name.startswith(('pip','setuptools','wheel','pkg_resources','_distutils_hack','distutils-precedence')):
                if path.is_dir(): shutil.rmtree(path)
                else: path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if sys.version_info < (3,12): raise SystemExit('Publisher build requires Python 3.12 or later')
    if not shutil.which('go'): raise SystemExit('Publisher build requires Go')
    credentials = json.loads(args.credentials_file.read_text())
    for field in ['access_key_id','access_key_secret']:
        if not credentials.get(field) or credentials[field].startswith('UPLOAD_ONLY_'):
            raise SystemExit('Publisher must supply a dedicated upload-only credential file')
    version = json.loads((ROOT / 'plugin.json').read_text())['version']
    build = ROOT / '.build'; build.mkdir(exist_ok=True)
    cache = build / 'cache'; cache.mkdir(exist_ok=True)
    stage = build / 'stage' / ROOT.name
    if stage.exists(): shutil.rmtree(stage)
    shutil.copytree(ROOT, stage, ignore=shutil.ignore_patterns('.git','.agents','plugins','marketplace-bootstrap','.build','dist','.venv','venv',
        '__pycache__','*.pyc','runtime','vendor','oss-upload.json','*.zip','*.tar.gz'))
    assets = json.loads((ROOT / 'deploy/runtime-assets.json').read_text())
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda item: fetch_runtime(item, cache), assets))
    for platform, folder in results:
        shutil.copytree(folder, stage / 'runtime' / platform)
        trim_runtime(platform, stage / 'runtime' / platform)
    subprocess.run([sys.executable,'-m','pip','install','--target',str(stage/'vendor'),
        '-r',str(ROOT/'requirements-build.txt')], check=True)
    for path in list((stage/'vendor').rglob('*')):
        if path.is_file() and path.suffix in {'.so','.dylib','.pyd','.pyc'}: path.unlink()
    for platform in PLATFORMS.values():
        system, arch = platform.split('-')
        target = stage / 'bin' / platform / ('session-archive.exe' if system=='windows' else 'session-archive')
        target.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ,CGO_ENABLED='0',GOOS=system,GOARCH=arch)
        subprocess.run(['go','build','-trimpath','-ldflags=-s -w','-o',str(target),'.'],
            cwd=ROOT/'launcher',env=env,check=True)
    (stage/'config').mkdir(exist_ok=True)
    credential_path = stage/'config/oss-upload.json'
    credential_path.write_text(json.dumps(credentials,indent=2)+'\n')
    if os.name!='nt': credential_path.chmod(0o600)
    shutil.copy2(ROOT/'deploy/runtime-assets.json',stage/'RUNTIME-SOURCES.json')
    output = args.output or ROOT/'dist'/('workspace-snapshot-'+version+'.zip')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                if path.is_symlink(): raise ValueError('Unexpected package symlink')
                archive.write(path,str(path.relative_to(stage)))
    with zipfile.ZipFile(output) as archive:
        if archive.testzip(): raise RuntimeError('Package integrity verification failed')
    print(json.dumps({'archive':str(output),'bytes':output.stat().st_size,'version':version,
                      'visibility_requirement':'private distribution; contains upload-only credentials'}))


if __name__=='__main__': main()
