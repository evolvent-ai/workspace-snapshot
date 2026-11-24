import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('setup_portable',Path(__file__).resolve().parents[1]/'hooks/setup_portable.py')
module=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(module)


class PortableSetupTests(unittest.TestCase):
    def test_setup_preserves_other_hooks_and_trust_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);home=base/'home';home.mkdir()
            plugin=base/'plugin';plugin.mkdir();(plugin/'plugin.json').write_text(json.dumps({'version':'0.4.0'}))
            config=home/'.codex';config.mkdir()
            original={'hooks':{'PreToolUse':[{'hooks':[{'type':'command','command':'keep-my-other-hook'}]}]}}
            (config/'hooks.json').write_text(json.dumps(original))
            (config/'config.toml').write_text('trust-marker-do-not-change\n')
            with patch.object(module.Path,'home',return_value=home),patch.dict(os.environ,{'CODEX_HOME':str(config)}):
                first=module.setup(plugin);second=module.setup(plugin)
            self.assertFalse(first['hook_trust_modified']);self.assertFalse(second['hook_trust_modified'])
            self.assertEqual((config/'config.toml').read_text(),'trust-marker-do-not-change\n')
            result=json.loads((config/'hooks.json').read_text())
            self.assertEqual(len(result['hooks']['PreToolUse']),2)
            self.assertEqual(result['hooks']['PreToolUse'][0],original['hooks']['PreToolUse'][0])
            self.assertEqual(len(list(config.glob('hooks.json.backup-*'))),2)

    def test_archive_paths_support_windows_drive_and_unc(self):
        spec=importlib.util.spec_from_file_location('session_capture',Path(__file__).resolve().parents[1]/'hooks/session_capture.py')
        capture=importlib.util.module_from_spec(spec);spec.loader.exec_module(capture)
        self.assertEqual(capture.archive_relative('C:\\Users\\Alice\\new\\a.md'),'C/Users/Alice/new/a.md')
        self.assertEqual(capture.archive_relative('\\\\server\\share\\a.md'),'UNC/server/share/a.md')


if __name__=='__main__':unittest.main()
