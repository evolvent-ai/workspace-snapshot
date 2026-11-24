"""Session evidence capture: raw rollout, observed file versions and GitHub repositories."""
import ast
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()


def github_urls(text):
    return sorted(set('%s/%s' % (a, b.rstrip('.').removesuffix('.git')) for a, b in
                      re.findall(r'(?:https://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+)', text)))


def absolute(value, cwd):
    if not isinstance(value, str) or not value or any(x in value for x in ('\n', '\r', '\x00')): return None
    if '://' in value: return None
    try:
        p = Path(value).expanduser()
        # Shell arguments may be arbitrary prose/JSON. Never stat an overlong component.
        if any(len(part.encode()) > 255 for part in p.parts): return None
        result = str(Path(os.path.abspath(p if p.is_absolute() else Path(cwd) / p)))
        return result if len(result.encode()) <= 4096 else None
    except (OSError, ValueError, RuntimeError): return None


def shell_without_heredocs(command):
    lines, delimiter = [], None
    for line in command.splitlines():
        if delimiter:
            if line.strip() == delimiter: delimiter = None
            continue
        match = re.search(r'<<-?\s*[\x27\"]?([A-Za-z_]\w*)[\x27\"]?', line)
        if match:
            delimiter = match.group(1)
            line = line[:match.start()] + line[match.end():]
        lines.append(line)
    return '\n'.join(lines)


def access_records(root):
    path = root / 'accesses.jsonl'
    records = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict): records.append(item)
            except ValueError: continue
    return records


def literal(value):
    try: return json.loads(value)
    except (ValueError, TypeError):
        try: return ast.literal_eval(value)
        except (ValueError, SyntaxError): return None


def discover(name, args, cwd):
    """Do not execute tool text. Return observed/inferred accesses plus coverage gaps."""
    accesses, gaps = [], []
    if isinstance(args, str):
        parsed = literal(args)
        if isinstance(parsed, (dict, list)): args = parsed
    raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    repos = []
    if any(x in name.lower() for x in ('github', 'git_clone', 'clone_repository')):
        repos.extend(github_urls(raw))
        if isinstance(args, dict) and re.fullmatch(r'[\w.-]+', str(args.get('owner', ''))) and re.fullmatch(r'[\w.-]+', str(args.get('repo', ''))):
            repos.append(args['owner'] + '/' + args['repo'])
    def add(value, mode, evidence):
        p = absolute(value, cwd)
        if p: accesses.append({'path': p, 'access': mode, 'evidence': evidence})
    if isinstance(args, dict):
        cwd = args.get('workdir') or args.get('cwd') or cwd
        for key in ('file_path', 'path', 'filepath', 'filename'):
            if isinstance(args.get(key), str):
                mode = 'write' if any(x in name.lower() for x in ('write', 'edit', 'patch', 'save')) else 'read'
                add(args[key], mode, 'structured-tool-path')
        for key in ('paths', 'file_paths', 'files'):
            if isinstance(args.get(key), list):
                for value in args[key]:
                    if isinstance(value, str): add(value, 'read-write', 'structured-tool-path-list')
    for match in re.finditer(r'\*\*\* (?:Add|Update|Delete) File: ([^\n]+)', raw.replace('\\n', '\n')):
        operation = 'delete' if match.group(0).startswith('*** Delete') else 'write'
        add(match.group(1).strip(), operation, 'apply-patch')
    # Nested functions.exec code carries exec_command arguments as JavaScript string literals.
    strings = r'("(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\]|\\.)*\x27)'
    nested_cwd = re.search(r'\b(?:workdir|cwd)\s*:\s*' + strings, raw)
    if nested_cwd:
        value = literal(nested_cwd.group(1))
        if isinstance(value, str): cwd = absolute(value, cwd) or cwd
    for match in re.finditer(r'\b(?:file_path|path)\s*:\s*' + strings, raw):
        value = literal(match.group(1))
        if isinstance(value, str): add(value, 'read-write', 'nested-tool-path')
    commands = []
    if isinstance(args, dict):
        for key in ('cmd', 'command'):
            if isinstance(args.get(key), str): commands.append(args[key])
    for match in re.finditer(r'\b(?:cmd|command)\s*:\s*' + strings, raw):
        value = literal(match.group(1))
        if isinstance(value, str): commands.append(value)
    if isinstance(args, str) and any(x in name.lower() for x in ('bash', 'shell', 'exec_command')):
        commands.append(args)
    for command in commands:
        shell = shell_without_heredocs(command)
        if re.search(r'\b(?:git\b[^\n;]*\b(?:clone|fetch|ls-remote)|gh\s+repo\s+clone)\b', shell):
            repos.extend(github_urls(shell))
        try: tokens = shlex.split(shell, comments=True)
        except ValueError:
            gaps.append('unparsed-shell-command'); continue
        working = cwd
        for i, token in enumerate(tokens):
            if token == 'cd' and i + 1 < len(tokens): working = absolute(tokens[i + 1], working) or working
            if token in {'>', '>>', '<'} and i + 1 < len(tokens):
                add(absolute(tokens[i + 1], working), 'read' if token == '<' else 'write', 'shell-redirection')
            candidate = absolute(token.rstrip(';'), working)
            if candidate:
                try:
                    if Path(candidate).is_file() or Path(candidate).is_dir():
                        add(candidate, 'read-write', 'shell-file-argument')
                except OSError: gaps.append('shell-path-could-not-be-inspected')
        # Recognize common Python literal file access, including heredoc scripts.
        for match in re.finditer(r'(?:Path|open)\(\s*' + strings, command):
            value = literal(match.group(1))
            if isinstance(value, str): add(absolute(value, working), 'read-write', 'python-literal-path')
        if re.search(r'\b(?:python\d*|node|bash|sh|zsh|ruby|perl)\b', command):
            gaps.append('program-internal-file-access-not-observable-with-tool-hooks')
        if re.search(r'\b(?:rg|grep|find)\b', command):
            gaps.append('search-internal-file-access-not-observable-with-tool-hooks')
    if not accesses and any(x in name.lower() for x in ('exec', 'bash', 'python', 'node', 'repl')):
        gaps.append('opaque-execution-no-file-paths-extracted')
    unique = {(a['path'], a['access'], a['evidence']): a for a in accesses}
    return list(unique.values()), sorted(set(repos)), sorted(set(gaps))


def git(args, cwd=None, check=True):
    if os.environ.get('CONVERSATION_OSS_PORTABLE_GIT') == '1':
        import git_portable
        return git_portable.run(args, cwd=cwd, check=check)
    result = subprocess.run(['git'] + args, cwd=cwd, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    if result.returncode and check: raise RuntimeError('Git operation failed: ' + args[0])
    return result


def local_repository(path):
    p = Path(path)
    folder = p if p.is_dir() else p.parent
    if not folder.exists(): return None
    result = git(['-C', str(folder), 'rev-parse', '--show-toplevel'], check=False)
    if result.returncode: return None
    root = result.stdout.decode().strip()
    remotes = git(['-C', root, 'remote', '-v'], check=False).stdout.decode()
    urls = github_urls(remotes)
    return {'root': root, 'github': urls[0]} if urls else None


def append(root, value):
    with (root / 'accesses.jsonl').open('a', encoding='utf-8') as f:
        os.chmod(f.name, 0o600)
        f.write(json.dumps(value, ensure_ascii=False) + '\n')


def snapshot(root, path):
    p = Path(path)
    if p.name == 'oss-upload.json' and p.parent.name == 'config':
        return {'status': 'excluded-sensitive-source'}
    exclusions = root.parent / 'excluded-paths.json'
    if exclusions.exists():
        try:
            excluded = {str(Path(value).expanduser().resolve()) for value in json.loads(exclusions.read_text())}
            if str(p.resolve()) in excluded: return {'status': 'excluded-sensitive-source'}
        except (OSError, ValueError, TypeError): return {'status': 'exclusion-config-error'}
    try:
        if not p.exists(): return {'status': 'missing'}
        if not p.is_file(): return {'status': 'directory' if p.is_dir() else 'special-file'}
    except OSError as error: return {'status': 'unreadable', 'error': type(error).__name__}
    blobs = root / 'blobs'
    blobs.mkdir(exist_ok=True, mode=0o700)
    target = blobs / (uuid.uuid4().hex + '.partial')
    digest = hashlib.sha256()
    try:
        with p.open('rb') as source, target.open('xb') as output:
            os.chmod(target, 0o600)
            before = os.fstat(source.fileno())
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block); output.write(block)
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return {'status': 'changed-during-capture'}
        sha = digest.hexdigest()
        final = blobs / sha
        if not final.exists(): target.replace(final)
        return {'status': 'captured', 'sha256': sha, 'bytes': before.st_size,
                'blob': 'files/blobs/' + sha, 'resolved_path': str(p.resolve())}
    except OSError as error:
        return {'status': 'unreadable', 'error': type(error).__name__}
    finally: target.unlink(missing_ok=True)


def referenced_directories(text, cwd):
    """Select only directories explicitly named in user text, not host context/tool output."""
    if not isinstance(text, str): return []
    text = re.sub(r'<(environment_context|skills_instructions|app-context)\b[^>]*>.*?</\1>', '', text, flags=re.S)
    values = []
    def collect(match):
        values.append(match.group(1).strip().strip('<>'))
        return ' '
    prefix = r'(?:/|~/|\./|\.\./|[A-Za-z]:[\\/]|\\\\)'
    for pattern in [r'(?m)^##\s+[^\n]*?:\s*(' + prefix + r'[^\n]+)$',
                    r'\]\(<?(' + prefix + r'[^\n]*?)>?\)',
                    r'(?<!`)`([^`\n]+)`(?!`)',
                    r'(?m)^\s*(' + prefix + r'[^\n]+)\s*$']:
        text = re.sub(pattern, collect, text)
    values.extend(re.findall(r'(?:^|[\s(\["\'])(' + prefix + r'[^\s`<>"\')\]，。；]+)', text))
    result = set()
    for value in values:
        if not value.startswith(('/', '~/', './', '../', '\\\\')) and not re.match(r'^[A-Za-z]:[\\/]', value): continue
        value = value.rstrip('.,;，。；')
        path = absolute(value, cwd)
        if not path: continue
        p = Path(path)
        if p in {Path(p.anchor), Path.home()}: continue
        try:
            if p.is_dir() or value.endswith('/'): result.add(path)
        except OSError: continue
    return sorted(result)


def capture_directory(root, path, turn_id, phase='referenced-directory-at-stop'):
    """Snapshot a user-selected input tree, including files never named by individual tools."""
    source = Path(path)
    store = root.parent.resolve()
    record = {'at': now(), 'phase': phase, 'tool': 'user-referenced-directory', 'turn_id': turn_id,
              'tool_call_id': None, 'directory_source': str(source), 'directory_entries': [],
              'accesses': [], 'github_repositories': [], 'repository_evidence_version': 2,
              'coverage_gaps': [], 'file_count': 0, 'total_bytes': 0}
    def access(p, **state):
        item = {'path': str(p), 'access': 'reference', 'evidence': 'explicit-user-directory-snapshot',
                'directory_source': str(source), **state}
        record['accesses'].append(item)
        if state.get('status') == 'captured':
            record['file_count'] += 1
            record['total_bytes'] += state['bytes']
    def blocked(p):
        return p == store or store in p.parents
    def walk_error(error):
        access(Path(error.filename or source), status='unreadable', error=type(error).__name__)
    if source in {Path(source.anchor), Path.home()} or blocked(source):
        access(source, status='excluded-unbounded-or-internal-directory')
    elif source.is_symlink() or any(p.is_symlink() for p in source.parents):
        access(source, status='symlink-not-expanded')
    elif not source.is_dir():
        access(source, status='missing-directory')
    else:
        for directory, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
            parent = Path(directory)
            record['directory_entries'].append(str(parent))
            for name in list(dirs):
                child = parent / name
                if blocked(child) or child.is_symlink():
                    dirs.remove(name)
                    access(child, status='excluded-internal-evidence-store' if blocked(child) else 'symlink-not-expanded')
            dirs.sort()
            for name in sorted(files):
                child = parent / name
                if child.is_symlink(): access(child, status='symlink-not-expanded')
                elif blocked(child): access(child, status='excluded-internal-evidence-store')
                else: access(child, **snapshot(root, child))
        try:
            repo = local_repository(source)
            if repo: record['local_repositories'] = [repo]
        except (OSError, subprocess.SubprocessError): record['coverage_gaps'].append('repository-detection-failed')
    append(root, record)
    return record


def capture_user_directories(root, text, cwd, turn_id, phase='user-reference-before-turn'):
    return [capture_directory(root, path, turn_id, phase) for path in referenced_directories(text, cwd)]


def capture(root, event, phase=None):
    name = event.get('tool_name', '')
    phase = phase or event.get('hook_event_name', 'recovered-at-stop')
    accesses, repos, gaps = discover(name, event.get('tool_input', {}), event.get('cwd') or os.getcwd())
    record = {'at': now(), 'phase': phase, 'tool': name, 'tool_call_id': event.get('tool_use_id'),
              'turn_id': event.get('turn_id'), 'accesses': [], 'github_repositories': repos,
              'repository_evidence_version': 2, 'coverage_gaps': gaps}
    if phase == 'recovered-at-stop' and accesses:
        record['coverage_gaps'].append('recovered-current-file-content-not-historical-read-write-version')
    for access in accesses:
        p = Path(access['path'])
        # Never recursively ingest the plugin evidence store into its own archive.
        store = root.parent.resolve()
        if p == store or store in p.parents:
            access.update(status='excluded-internal-evidence-store')
        else:
            access.update(snapshot(root, str(p)))
            if access['status'] == 'missing' and phase == 'PreToolUse' and access['access'] in {'write', 'read-write'}:
                access['status'] = 'not-yet-created'
            if access['status'] == 'missing' and phase == 'PostToolUse' and access['access'] == 'delete':
                access['status'] = 'deleted-after-tool'
        record['accesses'].append(access)
        try:
            repo = local_repository(str(p))
            if repo: record.setdefault('local_repositories', []).append(repo)
        except (OSError, subprocess.SubprocessError): record['coverage_gaps'].append('repository-detection-failed')
    if accesses or any(x in name.lower() for x in ('exec', 'bash', 'shell', 'git')):
        try:
            repo = local_repository(event.get('cwd') or os.getcwd())
            if repo: record.setdefault('local_repositories', []).append(repo)
        except (OSError, subprocess.SubprocessError): record['coverage_gaps'].append('cwd-repository-detection-failed')
    append(root, record)
    return record


def rollout_bytes(path, turn_id):
    if not path: raise ValueError('Current session transcript_path is unavailable')
    data = Path(path).read_bytes()
    output, found = [], False
    for line in data.splitlines(keepends=True):
        try: row = json.loads(line)
        except ValueError:
            output.append(line); continue
        p = row.get('payload', {})
        if row.get('type') == 'event_msg' and p.get('type') in {'task_started', 'turn_started'}:
            if found and p.get('turn_id') != turn_id: break
            if p.get('turn_id') == turn_id: found = True
        output.append(line)
    return b''.join(output)


def recover(root, data, cwd, snapshot_turn=None):
    existing = access_records(root)
    seen = {r.get('tool_call_id') for r in existing if r.get('tool_call_id')}
    repos = set()
    current_cwd = cwd
    current_turn = None
    directories = set()
    for line in data.splitlines():
        try: row = json.loads(line)
        except ValueError: continue
        p = row.get('payload', {})
        if row.get('type') == 'event_msg' and p.get('type') in {'task_started', 'turn_started'}:
            current_turn = p.get('turn_id')
        if row.get('type') == 'turn_context': current_cwd = p.get('cwd') or current_cwd
        if row.get('type') != 'response_item': continue
        if p.get('type') == 'message' and p.get('role') in {'user', 'assistant'}:
            for c in p.get('content', []):
                repos.update(github_urls(c.get('text', '')))
                if p.get('role') == 'user':
                    directories.update(referenced_directories(c.get('text', ''), current_cwd))
        if p.get('type') not in {'function_call', 'custom_tool_call'}: continue
        call_id = p.get('call_id') or p.get('id')
        if call_id in seen:
            try:
                _, operations, _ = discover(p.get('name', ''), p.get('arguments', p.get('input', {})), current_cwd)
                repos.update(operations)
            except Exception: pass
            continue
        try:
            record = capture(root, {'tool_name': p.get('name', ''), 'tool_input': p.get('arguments', p.get('input', {})),
                             'tool_use_id': call_id, 'cwd': current_cwd, 'turn_id': current_turn}, 'recovered-at-stop')
        except Exception as error:
            record = {'at': now(), 'phase': 'recovered-at-stop', 'tool_call_id': call_id,
                      'turn_id': current_turn, 'accesses': [], 'github_repositories': [],
                      'coverage_gaps': ['tool-recovery-failed:' + type(error).__name__]}
            append(root, record)
        repos.update(record['github_repositories'])
    # Re-snapshot each selected input tree once per export, not once per old message.
    for path in sorted(directories):
        capture_directory(root, path, snapshot_turn or current_turn)
    return repos


def add_file(z, path, name, entries):
    p = Path(path)
    if p.is_symlink():
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, os.readlink(p))
        entries.append({'archive': name, 'symlink': os.readlink(p)})
        return
    h = hashlib.sha256()
    info = zipfile.ZipInfo.from_file(p, name, strict_timestamps=False)
    info.compress_type = zipfile.ZIP_DEFLATED
    with p.open('rb') as f, z.open(info, 'w', force_zip64=True) as out:
        before = os.fstat(f.fileno())
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block); out.write(block)
        after = os.fstat(f.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise RuntimeError('Repository changed during export: ' + str(p))
    entries.append({'archive': name, 'bytes': before.st_size, 'sha256': h.hexdigest()})


def repo_zip(root, name, output, cache, excluded_root=None):
    issues, entries = [], []
    remote_only = not root
    if remote_only:
        cache.mkdir(parents=True, exist_ok=True)
        root = cache / (name.replace('/', '--') + '-' + uuid.uuid4().hex)
        git(['clone', '--mirror', 'https://github.com/' + name + '.git', str(root)])
    root = Path(root)
    bare = git(['-C', str(root), 'rev-parse', '--is-bare-repository']).stdout.strip() == b'true'
    shallow = git(['-C', str(root), 'rev-parse', '--is-shallow-repository']).stdout.strip() == b'true'
    if shallow:
        # Hydrate a separate mirror; never rewrite the user's checkout.
        mirror = cache / ('full-' + uuid.uuid4().hex)
        cache.mkdir(parents=True, exist_ok=True)
        git(['clone', '--mirror', 'https://github.com/' + name + '.git', str(mirror)])
        history_root = mirror
    else: history_root = root
    head = git(['-C', str(root), 'rev-parse', 'HEAD'], check=False).stdout.decode().strip()
    with tempfile.TemporaryDirectory(prefix='session-git-bundle-') as temporary:
        bundle = Path(temporary) / 'history.bundle'
        refs = git(['-C', str(history_root), 'show-ref'], check=False).stdout
        if refs:
            git(['-C', str(history_root), 'bundle', 'create', str(bundle), '--all'])
            git(['-C', str(history_root), 'bundle', 'verify', str(bundle)])
        with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            if refs: add_file(z, bundle, 'git/history.bundle', entries)
            if bare:
                if head:
                    tracked = Path(temporary) / 'HEAD.zip'
                    git(['-C', str(root), 'archive', '--format=zip', '-o', str(tracked), 'HEAD'])
                    with zipfile.ZipFile(tracked) as source:
                        for item in source.infolist():
                            item.filename = 'worktree/' + item.filename
                            z.writestr(item, source.read(item.filename.removeprefix('worktree/')))
            else:
                for directory, dirs, files in os.walk(root, followlinks=False):
                    # Include ignored files, dependencies and untracked files; history is in the bundle.
                    dirs[:] = [d for d in dirs if d != '.git']
                    if excluded_root:
                        for d in list(dirs):
                            p = Path(directory) / d
                            if p == excluded_root or excluded_root in p.parents:
                                dirs.remove(d)
                                issues.append({'path': str(p), 'reason': 'internal-evidence-store-omitted-to-prevent-recursion'})
                    for d in list(dirs):
                        p = Path(directory) / d
                        if p.is_symlink():
                            add_file(z, p, 'worktree/' + str(p.relative_to(root)), entries); dirs.remove(d)
                    for filename in files:
                        p = Path(directory) / filename
                        if filename == '.git': continue
                        if p.is_file() or p.is_symlink(): add_file(z, p, 'worktree/' + str(p.relative_to(root)), entries)
                        else: issues.append({'path': str(p), 'reason': 'special-file'})
                sub = git(['-C', str(root), 'submodule', 'status', '--recursive'], check=False).stdout.decode()
                if sub.strip(): issues.append({'reason': 'submodule-history-not-exported', 'details': sub})
                lfs = git(['-C', str(root), 'lfs', 'ls-files'], check=False)
                if lfs.stdout.strip(): issues.append({'reason': 'lfs-object-history-not-exported', 'details': lfs.stdout.decode()})
            info = {'github': name, 'local_root': None if remote_only else str(root), 'head': head,
                    'history': 'git/history.bundle' if refs else None, 'shallow_source': shallow,
                    'issues': issues, 'files': entries}
            z.writestr('REPOSITORY.json', json.dumps(info, ensure_ascii=False, indent=2))
    return info


def archive_relative(path):
    value = str(path).replace('\\', '/')
    value = re.sub(r'^([A-Za-z]):/', r'\1/', value)
    if value.startswith('//'): value = 'UNC/' + value.lstrip('/')
    return value.lstrip('/')


def export(root, event, output):
    data = rollout_bytes(event.get('transcript_path'), event['turn_id'])
    allowed_turns, allowed_calls, trajectory_session_id = {event['turn_id']}, set(), None
    for line in data.splitlines():
        try: row = json.loads(line)
        except ValueError: continue
        payload = row.get('payload', {})
        if row.get('type') == 'session_meta':
            trajectory_session_id = payload.get('session_id') or payload.get('id')
            if trajectory_session_id and trajectory_session_id != event['session_id']:
                raise ValueError('Transcript session ID does not match Stop event; refusing wrong-session archive')
        if payload.get('turn_id'): allowed_turns.add(payload['turn_id'])
        if payload.get('call_id'): allowed_calls.add(payload['call_id'])
        if payload.get('type') == 'item_completed':
            item = payload.get('item', {})
            if item.get('id'): allowed_calls.add(item['id'])
    mentioned = recover(root, data, event.get('cwd') or os.getcwd(), event['turn_id'])
    records = [r for r in access_records(root)
               if r.get('turn_id') in allowed_turns or r.get('tool_call_id') in allowed_calls
               or (not r.get('turn_id') and not r.get('tool_call_id'))]
    repositories = {}
    for r in records:
        if r.get('repository_evidence_version') == 2:
            mentioned.update(r.get('github_repositories', []))
        for repo in r.get('local_repositories', []): repositories[(repo['github'], repo['root'])] = repo
    for name in mentioned:
        if not any(k[0] == name for k in repositories): repositories[(name, '')] = {'github': name, 'root': None}
    report = {'format': 4, 'session_id': event['session_id'], 'trajectory_session_id': trajectory_session_id,
              'turn_id': event['turn_id'], 'exported_at': now(),
              'trajectory': 'trajectory/rollout.jsonl', 'tool_events': 'files/accesses.jsonl',
              'repositories': [], 'issues': [], 'file_versions': 0,
              'coverage': 'tool-hooks-and-static-tool-input-analysis; not operating-system file auditing'}
    directories = {r['directory_source']: {'source': r['directory_source'], 'file_count': r['file_count'],
                   'total_bytes': r['total_bytes'], 'captured_at': r['at']}
                   for r in records if r.get('directory_source')}
    report['referenced_directories'] = list(directories.values())
    for r in records:
        for issue in r.get('coverage_gaps', []): report['issues'].append({'tool_call_id': r.get('tool_call_id'), 'reason': issue})
        for a in r.get('accesses', []):
            if a.get('status') not in {'captured', 'directory', 'not-yet-created', 'deleted-after-tool'}:
                report['issues'].append({'path': a['path'], 'reason': a.get('status')})
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        os.chmod(output, 0o600)
        z.writestr('trajectory/rollout.jsonl', data)
        z.writestr('trajectory/stop-event.json', json.dumps(event, ensure_ascii=False, indent=2))
        z.writestr('files/accesses.jsonl', '\n'.join(json.dumps(r, ensure_ascii=False) for r in records) + '\n')
        blobs = root / 'blobs'
        hashes = {a['sha256'] for r in records for a in r.get('accesses', []) if a.get('status') == 'captured'}
        for sha in sorted(hashes):
            p = blobs / sha
            if re.fullmatch(r'[a-f0-9]{64}', p.name):
                z.write(p, 'files/blobs/' + p.name); report['file_versions'] += 1
        latest = {}
        for record in records:
            for a in record.get('accesses', []):
                if a.get('status') == 'captured': latest[a['path']] = a
        path_index = []
        for directory in sorted({p for r in records for p in r.get('directory_entries', [])}):
            relative = archive_relative(directory)
            if '..' not in Path(relative).parts: z.writestr('files/latest/' + relative.rstrip('/') + '/', b'')
        for path, a in sorted(latest.items()):
            relative = archive_relative(path)
            if '..' in Path(relative).parts: continue
            name = 'files/latest/' + relative
            z.write(blobs / a['sha256'], name)
            path_index.append({'source': path, 'archive': name, 'sha256': a['sha256'],
                               **({'directory_source': a['directory_source']} if a.get('directory_source') else {})})
        z.writestr('files/paths.json', json.dumps(path_index, ensure_ascii=False, indent=2))
        for i, repo in enumerate(repositories.values(), 1):
            name = 'repositories/%02d-%s.zip' % (i, repo['github'].replace('/', '--'))
            try:
                with tempfile.TemporaryDirectory(prefix='session-repository-') as temp:
                    nested = Path(temp) / 'repository.zip'
                    info = repo_zip(repo['root'], repo['github'], nested, Path(temp) / 'git-cache', root.parent.resolve())
                    z.write(nested, name)
                report['repositories'].append({'archive': name, **{k: v for k, v in info.items() if k != 'files'}})
                report['issues'].extend(info['issues'])
            except Exception as error:
                report['issues'].append({'github': repo['github'], 'reason': 'repository-export-failed', 'error': str(error)})
        report['complete'] = not report['issues']
        z.writestr('MANIFEST.json', json.dumps(report, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(output) as z:
        if z.testzip(): raise RuntimeError('Session ZIP integrity check failed')
    return report
