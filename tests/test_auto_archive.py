import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('auto_archive', Path(__file__).resolve().parents[1] / 'hooks/auto_archive.py')
auto = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auto)


class AutomaticArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'PLUGIN_DATA': str(self.base / 'state')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.transcript = self.base / 'rollout.jsonl'
        self.transcript.write_text(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'text':'hello'}]}})+'\n')
        self.event = {'session_id':'test-chat','turn_id':'turn-1','hook_event_name':'Stop',
                      'cwd':str(self.base),'transcript_path':str(self.transcript)}

    def test_each_reply_uploads_even_with_no_files(self):
        with patch.object(auto.archive, 'upload', return_value={'oss_uri':'oss://test/key'}) as upload:
            self.assertEqual(auto.stop(self.event), {})
            self.assertEqual(auto.stop(self.event), {})
            self.event['turn_id']='turn-2'; auto.stop(self.event)
            self.assertEqual(upload.call_count, 2)
        status=auto.read(auto.session_dir('test-chat')/'latest.json',{})
        self.assertEqual(status['status'],'uploaded')
        self.assertTrue(Path(status['archive']).exists())
        import zipfile
        with zipfile.ZipFile(status['archive']) as z:
            self.assertEqual(z.read('trajectory/rollout.jsonl'), self.transcript.read_bytes())

    def test_upload_failure_preserves_archive_and_never_restarts_reply(self):
        with patch.object(auto.archive,'upload',side_effect=RuntimeError('failed')) as upload:
            result=auto.stop(self.event); auto.stop(self.event)
            self.assertEqual(upload.call_count,1)
        self.assertIn('systemMessage',result); self.assertNotIn('decision',result)
        status=auto.read(auto.session_dir('test-chat')/'latest.json',{})
        self.assertEqual(status['status'],'error'); self.assertTrue(Path(status['archive']).is_file())

    def test_missing_trajectory_is_reported_as_error(self):
        self.event['transcript_path']=None
        with patch.object(auto.archive,'upload') as upload:
            self.assertIn('systemMessage',auto.stop(self.event)); upload.assert_not_called()

    def test_capture_hook_preserves_read_and_written_versions(self):
        source=self.base/'中文 file.txt'; source.write_text('before')
        event={**self.event,'tool_name':'read_file','tool_input':{'path':str(source)},'tool_use_id':'call-1','hook_event_name':'PreToolUse'}
        auto.capture_event(event)
        source.write_text('after')
        event.update(tool_name='write_file',hook_event_name='PostToolUse',tool_use_id='call-2')
        auto.capture_event(event)
        blobs=auto.session_dir('test-chat')/'blobs'
        self.assertEqual({p.read_text() for p in blobs.iterdir()},{'before','after'})

    def test_chat_stores_are_isolated(self):
        self.assertNotEqual(auto.session_dir('one'),auto.session_dir('two'))

    def test_context_uses_collection_hooks_instead_of_required_registration(self):
        context=auto.context(self.event)['hookSpecificOutput']['additionalContext']
        self.assertIn('PreToolUse/PostToolUse',context)
        self.assertIn('无需手动登记',context)

    def test_prompt_captures_explicit_input_directory_without_file_tools(self):
        folder=self.base/'input';folder.mkdir();(folder/'a.md').write_text('data')
        self.event['prompt']='## Input: '+str(folder)+'/'
        auto.context(self.event)
        records=auto.session_capture.access_records(auto.session_dir('test-chat'))
        self.assertEqual(records[0]['file_count'],1)
        self.assertEqual(records[0]['phase'],'user-reference-before-turn')


if __name__=='__main__':unittest.main()
