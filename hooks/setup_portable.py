"""One-time local setup; no system packages, OSS settings, or hook-trust edits."""
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile


def setup(plugin_root):
    plugin_root = Path(plugin_root)
    version = json.loads((plugin_root / 'plugin.json').read_text())['version']
    base = Path.home() / '.local/share/conversation-oss-archive'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = base / 'data'; data.mkdir(exist_ok=True, mode=0o700)
    target = base / ('portable-runtime-' + version)
    if target.resolve() != plugin_root.resolve() and not target.exists():
        shutil.copytree(plugin_root, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    config_home = Path(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex'))
    config_home.mkdir(parents=True, exist_ok=True)
    file = config_home / 'hooks.json'
    document = json.loads(file.read_text()) if file.exists() else {'hooks': {}}
    if file.exists():
        shutil.copy2(file, file.with_name('hooks.json.backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    for event, mode in [('UserPromptSubmit','context'),('PreToolUse','capture'),('PostToolUse','capture'),('Stop','stop')]:
        if os.name == 'nt':
            launcher = target / 'bin/windows-amd64/session-archive.exe'
            command = 'set "PLUGIN_DATA=%s" && "%s" %s' % (data, launcher, mode)
        else:
            command = shlex.join(['env','PLUGIN_DATA='+str(data),'/bin/sh',str(target/'bin/launch.sh'),mode])
        entries = document.setdefault('hooks', {}).setdefault(event, [])
        kept = [group for group in entries if not any('conversation-oss-archive' in handler.get('command','') for handler in group.get('hooks',[]))]
        handler = {'type':'command','command':command,'timeout':3600 if event=='Stop' else 120}
        if event=='Stop': handler.update({'async':True,'statusMessage':'正在归档 session 到 seed-data/workspace'})
        kept.append({'hooks':[handler]}); document['hooks'][event]=kept
    fd, temporary = tempfile.mkstemp(dir=config_home, prefix='.archive-hooks-')
    with os.fdopen(fd,'w') as f: json.dump(document, f, ensure_ascii=False, indent=2)
    os.replace(temporary,file)
    if os.name!='nt': os.chmod(file,0o600)
    return {'status':'configured','version':version,'runtime':str(target),'hooks_file':str(file),
            'destination':'oss://seed-data/workspace/','hook_trust_modified':False,
            'next_step':'Review and trust the four hooks in Codex Settings > Hooks'}
