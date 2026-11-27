#!/usr/bin/env python3
"""Update the private marketplace's bundled portable ZIP and native bootstrap."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--payload',type=Path,required=True)
parser.add_argument('--version',required=True,help='Marketplace wrapper version')
args=parser.parse_args()
with zipfile.ZipFile(args.payload) as z:
    if z.testzip():raise SystemExit('Invalid portable ZIP')
    manifest=json.loads(z.read('plugin.json'))
    if manifest['name']!='workspace-snapshot':raise SystemExit('Unexpected portable plugin')
    if 'config/oss-upload.json' not in z.namelist():raise SystemExit('Portable upload configuration missing')
target=root/'plugins/workspace-snapshot'
digest=hashlib.sha256(args.payload.read_bytes()).hexdigest()
shutil.copy2(args.payload,target/'portable.zip')
(target/'PAYLOAD.json').write_text(json.dumps({'version':manifest['version'],'sha256':digest,
    'bytes':args.payload.stat().st_size,'private_distribution':True},indent=2)+'\n')
p=target/'.codex-plugin/plugin.json';data=json.loads(p.read_text());data['version']=args.version
p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
for system,arch in [('darwin','arm64'),('darwin','amd64'),('windows','amd64')]:
    binary=target/'bin'/(system+'-'+arch)/('session-archive.exe' if system=='windows' else 'session-archive')
    binary.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['go','build','-trimpath','-ldflags=-s -w -X main.payloadSHA='+digest,'-o',str(binary),'.'],
        cwd=root/'marketplace-bootstrap',env=dict(os.environ,CGO_ENABLED='0',GOOS=system,GOARCH=arch),check=True)
print(json.dumps({'marketplace_version':args.version,'payload_version':manifest['version'],'sha256':digest}))
