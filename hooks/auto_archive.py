#!/usr/bin/env python3
"""Codex hooks: session trajectory, observed file versions and complete GitHub repository exports."""
import argparse
try:
    import fcntl
except ImportError:
    from portable_lock import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('oss_archive', ROOT / 'skills/archive-to-oss/scripts/archive.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

sys.path.insert(0, str(ROOT / "hooks"))
import session_capture


def data_root():
    root = Path(os.environ.get('PLUGIN_DATA') or os.environ.get('CLAUDE_PLUGIN_DATA')
                or str(Path.home() / '.local/share/conversation-oss-archive'))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def session_dir(session_id):
    if not session_id: raise ValueError('session_id is required')
    root = data_root() / hashlib.sha256(session_id.encode()).hexdigest()[:32]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def save(path, value):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        temporary = Path(f.name)
    temporary.replace(path)


def read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def eligible(value, cwd):
    value = unquote(value.strip().removeprefix('file://'))
    value = re.sub(r':\d+(?::\d+)?$', '', value)
    if '://' in value or '\n' in value or not value: return None
    path = Path(value).expanduser()
    if not path.is_absolute(): path = Path(cwd) / path
    path = Path(os.path.abspath(path))
    if path == Path.home() or path == Path(path.anchor): return None
    if path.is_symlink() or any(p.is_symlink() for p in path.parents): return None
    data = data_root().resolve()
    if path == data or data in path.parents or path in data.parents: return None
    return str(path) if path.is_file() or path.is_dir() else None


def extract_paths(text, cwd):
    # Host environment blocks and injected instructions are not user path selections.
    text = re.sub(r'<(?:environment_context|skills_instructions|app-context)\b[^>]*>.*?</(?:environment_context|skills_instructions|app-context)>', '', text, flags=re.S)
    values = []
    # Markdown file links and inline code preserve paths containing spaces.
    values += re.findall(r'\]\(<?((?:/|~/|file://)[^\n]*?)>?\)', text)
    values += re.findall(r'(?<!`)`([^`\n]+)`(?!`)', text)
    # Plain absolute paths: require a token boundary, so HTTP URL paths do not match.
    values += re.findall(r'(?:^|[\s\(\["\'])((?:/|~/)[^\s`<>"\'\)\]，。；]+)', text)
    result = set()
    for value in values:
        if not (value.startswith(('/', '~/','file://', './', '../')) or '/' in value): continue
        path = eligible(value.rstrip('.,;'), cwd)
        if path: result.add(path)
    return result


def transcript_paths(path, cwd):
    paths = set()
    if not path: return paths
    # Read only the transcript handed to this hook, never search other chats.
    with Path(path).open(encoding='utf-8') as f:
        for line in f:
            try: record = json.loads(line)
            except json.JSONDecodeError: continue  # An in-progress final line is allowed.
            p = record.get('payload', {})
            if record.get('type') != 'response_item' or p.get('type') != 'message': continue
            if p.get('role') not in {'user', 'assistant'}: continue
            text = '\n'.join(c.get('text', '') for c in p.get('content', []) if isinstance(c, dict))
            paths.update(extract_paths(text, cwd))
    return paths


def register(session_id, values, cwd):
    root = session_dir(session_id)
    with (root / 'registry.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read(root / 'paths.json', [])
        paths = sorted(set(old) | {p for v in values if (p := eligible(v, cwd))})
        save(root / 'paths.json', paths)
    return {'registered_paths': len(paths)}


def context(event):
    root = session_dir(event['session_id'])
    session_capture.capture_user_directories(root, event.get('prompt', ''),
        event.get('cwd') or os.getcwd(), event.get('turn_id'))
    latest = read(root / 'latest.json', {})
    message = ('Session OSS 归档已启用：PreToolUse/PostToolUse 自动采集可识别的读写文件版本，'
               'Stop 在每次完整回复后后台上传原始轨迹、文件版本和涉及的 GitHub 仓库完整工作区及历史。'
               '无需手动登记常规文件调用。脚本动态生成的路径若不能从工具参数中识别，'
               '可以调用 hooks/auto_archive.py register 显式补充；不要宣称工具 Hook 能跟踪任意程序的所有系统文件访问。'
               '不要额外手动 upload 造成重复。')
    message += '用户明确提供的目录会递归保存子文件，包含图片、Markdown和ZIP，并在files/latest下保留路径。'
    if latest.get('status') in {'error', 'uploaded_partial'}:
        message += '最近归档存在失败或采集缺口，可用 status 查看并准确告知用户。'
    return {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': message}}


def capture_event(event):
    root = session_dir(event['session_id'])
    with (root / 'capture.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        session_capture.capture(root, event)
    return {}


def stop(event):
    if event.get('hook_event_name') != 'Stop': return {}
    root = session_dir(event['session_id'])
    turn = event.get('turn_id')
    if not turn: raise ValueError('turn_id is required for deduplication')
    turn_key = hashlib.sha256(turn.encode()).hexdigest()[:32]
    status_path = root / (turn_key + '.json')
    # Serialize per session; concurrent chats keep independent locks and receipts.
    with (root / 'upload.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read(status_path, {})
        if previous.get('status') in {'uploaded', 'uploaded_partial', 'skipped'}: return {}
        if previous.get('status') in {'uploading', 'error'}:
            return {'systemMessage': '本轮自动归档先前未确认成功，请检查状态；已跳过重复触发以避免重复上传。'}
        status = {'session_id': event['session_id'], 'turn_id': turn}
        export_dir = root / 'exports' / turn_key
        export_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = export_dir / ('session-' + os.urandom(6).hex() + '.zip')
        try:
            for p in read(root / 'paths.json', []):
                if Path(p).is_dir(): session_capture.capture_directory(root, p, turn, 'explicit-registration')
                else:
                    session_capture.capture(root, {'tool_name': 'registered-path',
                        'tool_input': {'path': p}, 'cwd': event.get('cwd'), 'turn_id': turn}, 'explicit-registration')
            report = session_capture.export(root, event, target)
            destination = os.environ.get('CONVERSATION_OSS_DESTINATION', archive.DEFAULT_DEST)
            # Keep the actual IDs visible so users can identify an archive without decoding hashes.
            session_component = re.sub(r'[^A-Za-z0-9_-]', '_', event['session_id'])
            turn_component = re.sub(r'[^A-Za-z0-9_-]', '_', turn)
            destination = destination.rstrip('/') + '/sessions/' + session_component + '/turns/' + turn_component + '/'
            status.update(status='uploading', archive=str(target))
            save(status_path, status)
            receipt = archive.upload(target, destination,
                os.environ.get('CONVERSATION_OSS_ENDPOINT', archive.DEFAULT_ENDPOINT))
            status.update(status='uploaded' if report['complete'] else 'uploaded_partial', receipt=receipt,
                          file_versions=report['file_versions'], repositories=report['repositories'],
                          issues=report['issues'])
            if not report['complete']:
                return {'systemMessage': 'Session 归档已上传，存在采集缺口；详见 MANIFEST.json 和插件 status。'}
            return {}
        except Exception as error:
            status.update(status='error', error=str(error), archive=str(target))
            return {'systemMessage': 'Session 自动归档失败：' + str(error)}
        finally:
            save(status_path, status)
            save(root / 'latest.json', status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['setup', 'context', 'capture', 'stop', 'register', 'status'])
    parser.add_argument('--session-id')
    parser.add_argument('--paths-json')
    parser.add_argument('--cwd', default=os.getcwd())
    args = parser.parse_args()
    if args.mode == 'setup':
        from setup_portable import setup
        result = setup(ROOT)
    elif args.mode == 'register':
        values = json.loads(Path(args.paths_json).read_text())
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise ValueError('paths-json must contain a JSON array of paths')
        result = register(args.session_id, values, args.cwd)
    elif args.mode == 'status': result = read(session_dir(args.session_id) / 'latest.json', {})
    else:
        event = json.load(sys.stdin)
        result = context(event) if args.mode == 'context' else capture_event(event) if args.mode == 'capture' else stop(event)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        # Never return Stop decision:block or exit 2: uploads must not restart the agent.
        print(json.dumps({'systemMessage': 'OSS 自动归档未完成：' + str(error)}, ensure_ascii=False))
