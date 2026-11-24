"""The archiver's limited Git operations, implemented without a Git executable."""
import io
import os
from pathlib import Path
import stat
import subprocess
import zipfile


def run(arguments, cwd=None, check=True):
    from dulwich.repo import Repo
    from dulwich import porcelain
    from dulwich.bundle import Bundle, read_bundle, write_bundle
    args = list(arguments)
    if args[:1] == ['-C']:
        cwd = args[1]; args = args[2:]
    cwd = cwd or os.getcwd()
    output = b''
    try:
        command = args[0]
        if command == 'clone':
            # A local independent clone needs no SSH client, git helper or hard links.
            remote, target = args[-2:]
            remote = ('https://github.com/' + remote.split(':', 1)[1]) if remote.startswith('git@github.com:') else remote
            kwargs = {}
            if os.environ.get('GITHUB_TOKEN') and remote.startswith('https://github.com/'):
                kwargs.update(username='x-access-token', password=os.environ['GITHUB_TOKEN'])
            with porcelain.clone(remote, target, bare=True, checkout=False, errstream=io.BytesIO(), **kwargs): pass
            return subprocess.CompletedProcess(arguments, 0, b'', b'')
        with Repo.discover(cwd) as repo:
            if command == 'rev-parse':
                value = args[-1]
                if value == '--show-toplevel':
                    if repo.bare: raise ValueError('Bare repository has no worktree')
                    output = os.fsencode(repo.path)
                elif value == '--is-bare-repository': output = b'true' if repo.bare else b'false'
                elif value == '--is-shallow-repository': output = b'true' if repo.get_shallow() else b'false'
                elif value == 'HEAD': output = repo.head()
                else: raise ValueError('Unsupported rev-parse option')
                output += b'\n'
            elif command == 'remote':
                lines = []
                config = repo.get_config()
                for section in config.sections():
                    if section[0] == b'remote':
                        try: url = config.get(section, b'url')
                        except KeyError: continue
                        lines.extend([section[1]+b'\t'+url+b' (fetch)', section[1]+b'\t'+url+b' (push)'])
                output = b'\n'.join(lines)
            elif command == 'show-ref':
                output = b'\n'.join(sha+b' '+ref for ref, sha in repo.get_refs().items() if ref != b'HEAD')
            elif command == 'bundle' and args[1] == 'create':
                refs = repo.get_refs()
                # Use raw tag refs and stream pack objects; avoid materializing an entire repo in RAM.
                count, objects = repo.generate_pack_data(have=set(), want=set(refs.values()))
                class StreamingPack:
                    object_format = repo.object_format
                    def __len__(self): return count
                    def iter_unpacked(self): return iter(objects)
                    def close(self): pass
                bundle = Bundle()
                bundle.version = 3 if repo.object_format.name != 'sha1' else 2
                bundle.references = refs
                bundle.prerequisites = []
                bundle.capabilities = {} if bundle.version == 2 else {'object-format': repo.object_format.name}
                bundle.pack_data = StreamingPack()
                try:
                    with Path(args[2]).open('wb') as f: write_bundle(f, bundle)
                finally: bundle.close()
            elif command == 'bundle' and args[1] == 'verify':
                with Path(args[2]).open('rb') as f:
                    with read_bundle(f) as bundle:
                        if bundle.prerequisites: raise ValueError('Incomplete repository bundle')
                        if bundle.pack_data is not None:
                            for _ in bundle.pack_data.iter_unpacked(): pass
            elif command == 'archive':
                target = Path(args[args.index('-o') + 1])
                commit = repo[repo.head()]
                with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
                    def walk(tree_id, prefix=''):
                        for entry in repo[tree_id].iteritems():
                            name = prefix + entry.path.decode('utf-8')
                            if stat.S_ISDIR(entry.mode): walk(entry.sha, name+'/')
                            elif entry.mode == 0o160000: continue
                            else:
                                info=zipfile.ZipInfo(name); info.create_system=3
                                info.external_attr=entry.mode << 16
                                z.writestr(info, repo[entry.sha].data)
                    walk(commit.tree)
            elif command == 'submodule':
                try: index = repo.open_index()
                except (FileNotFoundError, OSError): index = {}
                output = b'\n'.join(path for path, entry in index.items() if entry.mode == 0o160000)
            elif command == 'lfs':
                try: attributes=(Path(repo.path)/'.gitattributes').read_bytes()
                except OSError: attributes=b''
                output=b'LFS history requires a separate export' if b'filter=lfs' in attributes else b''
            else: raise ValueError('Unsupported portable Git operation')
        return subprocess.CompletedProcess(arguments, 0, output, b'')
    except Exception:
        if check: raise RuntimeError('Portable Git operation failed: '+str(args[0])) from None
        return subprocess.CompletedProcess(arguments, 1, b'', b'Portable Git operation failed')
