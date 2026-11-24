import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC=importlib.util.spec_from_file_location('session_capture',Path(__file__).resolve().parents[1]/'hooks/session_capture.py')
capture=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(capture)


class SessionCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.store=self.base/'state'/'session';self.store.mkdir(parents=True)

    def test_structured_read_write_and_shell_paths(self):
        source=self.base/'input.txt';source.write_text('data')
        accesses,_,_=capture.discover('read_file',{'path':str(source)},str(self.base))
        self.assertEqual(accesses[0]['access'],'read')
        accesses,_,_=capture.discover('write_file',{'path':'output.txt'},str(self.base))
        self.assertEqual(accesses[0]['path'],str(self.base/'output.txt'))
        accesses,_,_=capture.discover('exec_command',{'cmd':'cat input.txt','workdir':str(self.base)},'/')
        self.assertIn(str(source),{a['path'] for a in accesses})

    def test_nested_exec_and_patch(self):
        source=self.base/'input.txt';source.write_text('data')
        code='await tools.exec_command({cmd:"cat input.txt",workdir:'+json.dumps(str(self.base))+'});'
        accesses,_,_=capture.discover('functions.exec',code,'/')
        self.assertIn(str(source),{a['path'] for a in accesses})
        accesses,_,_=capture.discover('apply_patch','*** Begin Patch\n*** Add File: new.py\n+pass\n*** End Patch',str(self.base))
        self.assertEqual(accesses[0]['path'],str(self.base/'new.py'))

    def test_read_version_survives_delete(self):
        source=self.base/'deleted.txt';source.write_text('original')
        record=capture.capture(self.store,{'tool_name':'read_file','tool_input':{'path':str(source)},'cwd':str(self.base)},'PreToolUse')
        source.unlink()
        access=record['accesses'][0]
        self.assertEqual((self.store/'blobs'/access['sha256']).read_text(),'original')

    def test_raw_trajectory_and_stop_event_export(self):
        transcript=self.base/'rollout.jsonl'
        raw=(json.dumps({'type':'response_item','payload':{'type':'custom_tool_call_output','output':'full tool result'}})+'\n').encode()
        transcript.write_bytes(raw)
        output=self.base/'session.zip'
        report=capture.export(self.store,{'session_id':'test','turn_id':'one','transcript_path':str(transcript),'cwd':str(self.base),'last_assistant_message':'final'},output)
        self.assertTrue(report['complete'])
        with zipfile.ZipFile(output) as z:
            self.assertEqual(z.read('trajectory/rollout.jsonl'),raw)
            self.assertEqual(json.loads(z.read('trajectory/stop-event.json'))['last_assistant_message'],'final')

    def test_trajectory_does_not_include_next_turn(self):
        rows=[{'type':'event_msg','payload':{'type':'task_started','turn_id':'one'}},
              {'type':'response_item','payload':{'type':'message','role':'assistant','content':[]}},
              {'type':'event_msg','payload':{'type':'task_started','turn_id':'two'}}]
        p=self.base/'trace';p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        self.assertEqual(len(capture.rollout_bytes(p,'one').splitlines()),2)

    def test_recovery_uses_tool_calls_without_user_path_mentions(self):
        source=self.base/'hidden-read.txt';source.write_text('read me')
        row={'type':'response_item','payload':{'type':'function_call','name':'read_file','arguments':json.dumps({'path':str(source)}),'call_id':'old-call'}}
        raw=(json.dumps(row)+'\n').encode()
        capture.recover(self.store,raw,str(self.base))
        capture.recover(self.store,raw,str(self.base))
        log=[json.loads(l) for l in (self.store/'accesses.jsonl').read_text().splitlines()]
        self.assertEqual(len(log),1);self.assertEqual(log[0]['phase'],'recovered-at-stop')
        self.assertEqual(log[0]['accesses'][0]['status'],'captured')

    def test_repository_zip_has_full_worktree_and_restorable_history(self):
        repo=self.base/'repo';repo.mkdir()
        def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
        git('init');git('config','user.name','test');git('config','user.email','test@example.com')
        (repo/'main.py').write_text('v1');git('add','.');git('commit','-m','first')
        (repo/'main.py').write_text('v2');git('commit','-am','second')
        git('remote','add','origin','https://github.com/example/full-repository.git')
        (repo/'main.py').write_text('uncommitted')
        (repo/'.env').write_text('synthetic fixture only')
        (repo/'node_modules').mkdir();(repo/'node_modules/ignored.txt').write_text('included')
        (repo/'link').symlink_to('main.py')
        found=capture.local_repository(repo/'main.py')
        self.assertEqual(found['github'],'example/full-repository')
        out=self.base/'repository.zip'
        info=capture.repo_zip(repo,found['github'],out,self.base/'cache')
        self.assertEqual(info['issues'],[])
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.read('worktree/main.py'),b'uncommitted')
            self.assertEqual(z.read('worktree/.env'),b'synthetic fixture only')
            self.assertEqual(z.read('worktree/node_modules/ignored.txt'),b'included')
            self.assertEqual(z.read('worktree/link'),b'main.py')
            bundle=self.base/'history.bundle';bundle.write_bytes(z.read('git/history.bundle'))
        restore=self.base/'restored'
        subprocess.run(['git','clone',str(bundle),str(restore)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        commits=subprocess.check_output(['git','-C',str(restore),'rev-list','--count','HEAD']).strip()
        self.assertEqual(commits,b'2');self.assertEqual((restore/'main.py').read_text(),'v2')

    def test_opaque_program_accesses_are_explicit_gaps(self):
        _,_,gaps=capture.discover('exec_command',{'cmd':'python3 app.py'},str(self.base))
        self.assertIn('program-internal-file-access-not-observable-with-tool-hooks',gaps)

    def test_long_proposal_text_is_not_a_path(self):
        text='note:核验依据：'+('资产说明内容' * 200)+'\\n待补证据'
        accesses,_,_=capture.discover('Bash',{'command':'dpctl proposal update --note '+repr(text)},str(self.base))
        self.assertEqual(accesses,[])
        self.assertIsNone(capture.absolute('文'*1000,self.base))

    def test_heredoc_body_is_not_parsed_as_shell_arguments(self):
        unrelated=self.base/'unrelated.txt';unrelated.write_text('not read')
        command="python3 - <<'PY'\nprint('unrelated.txt')\nPY"
        accesses,_,_=capture.discover('Bash',{'command':command},str(self.base))
        self.assertNotIn(str(unrelated),{a['path'] for a in accesses})

    def test_test_code_github_url_is_not_a_repository_operation(self):
        command="python3 - <<'PY'\nremote='https://github.com/example/synthetic-test.git'\nPY"
        _,repos,_=capture.discover('Bash',{'command':command},str(self.base))
        self.assertEqual(repos,[])
        _,repos,_=capture.discover('Bash',{'command':'git clone https://github.com/example/real.git'},str(self.base))
        self.assertEqual(repos,['example/real'])

    def test_user_directory_is_recursive_and_keeps_zip_and_empty_directories(self):
        folder=self.base/'input folder 中文';folder.mkdir()
        (folder/'assets').mkdir();(folder/'empty').mkdir()
        (folder/'readme.md').write_text('input document')
        (folder/'assets/image.png').write_bytes(b'picture')
        (folder/'nested.zip').write_bytes(b'original zip content')
        text='# Files mentioned by the user:\n\n## Docs: '+str(folder)+'/\n\n## My request:\nRead these documents.'
        transcript=self.base/'trace';transcript.write_text(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'text':text}]}})+'\n')
        output=self.base/'result.zip'
        report=capture.export(self.store,{'session_id':'expected','turn_id':'one','transcript_path':str(transcript),'cwd':str(self.base)},output)
        self.assertEqual(report['referenced_directories'][0]['file_count'],3)
        self.assertTrue(report['complete'])
        with zipfile.ZipFile(output) as z:
            index=json.loads(z.read('files/paths.json'))
            self.assertEqual({Path(x['source']).relative_to(folder).as_posix() for x in index},{'readme.md','assets/image.png','nested.zip'})
            for x in index:self.assertEqual(z.read(x['archive']),Path(x['source']).read_bytes())
            self.assertIn('files/latest/'+str(folder).lstrip('/')+'/empty/',z.namelist())

    def test_environment_and_tool_directory_candidates_do_not_expand(self):
        folder=self.base/'source';folder.mkdir();(folder/'file.txt').write_text('data')
        text='<environment_context><cwd>'+str(folder)+'</cwd></environment_context>'
        self.assertEqual(capture.referenced_directories(text,str(self.base)),[])
        record=capture.capture(self.store,{'tool_name':'Bash','tool_input':{'command':'ls '+str(folder)},'cwd':str(self.base)},'PostToolUse')
        self.assertEqual(record['accesses'][0]['status'],'directory')
        self.assertFalse((self.store/'blobs').exists())

    def test_referenced_directory_does_not_follow_external_links(self):
        folder=self.base/'input';folder.mkdir()
        outside=self.base/'outside.txt';outside.write_text('external')
        (folder/'link.txt').symlink_to(outside)
        record=capture.capture_directory(self.store,folder,'one')
        self.assertEqual(record['file_count'],0)
        self.assertEqual(record['accesses'][0]['status'],'symlink-not-expanded')

    def test_explicit_directory_is_resampled_for_each_export(self):
        folder=self.base/'input';folder.mkdir();(folder/'a.txt').write_text('first')
        transcript=self.base/'trace';transcript.write_text(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'text':'Use `'+str(folder)+'`'}]}})+'\n')
        event={'session_id':'expected','turn_id':'one','transcript_path':str(transcript),'cwd':str(self.base)}
        capture.export(self.store,event,self.base/'first.zip')
        (folder/'b.txt').write_text('second');event['turn_id']='two'
        report=capture.export(self.store,event,self.base/'second.zip')
        self.assertEqual(report['referenced_directories'][0]['file_count'],2)

    def test_assistant_directory_mentions_are_not_input_selections(self):
        folder=self.base/'input';folder.mkdir();(folder/'a.txt').write_text('data')
        transcript=self.base/'trace';transcript.write_text(json.dumps({'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'text':'Example `'+str(folder)+'`'}]}})+'\n')
        report=capture.export(self.store,{'session_id':'expected','turn_id':'one','transcript_path':str(transcript),'cwd':str(self.base)},self.base/'result.zip')
        self.assertEqual(report['referenced_directories'],[])

    def test_bad_historical_tool_does_not_prevent_trajectory_export(self):
        row={'type':'response_item','payload':{'type':'function_call','name':'Bash','call_id':'bad-call','arguments':'{}'}}
        transcript=self.base/'trace';transcript.write_text(json.dumps(row)+'\n')
        output=self.base/'result.zip'
        with patch.object(capture,'capture',side_effect=OSError('bad path')):
            report=capture.export(self.store,{'session_id':'expected','turn_id':'one','transcript_path':str(transcript),'cwd':str(self.base)},output)
        self.assertFalse(report['complete'])
        with zipfile.ZipFile(output) as z:self.assertEqual(z.read('trajectory/rollout.jsonl'),transcript.read_bytes())

    def test_wrong_session_trajectory_is_rejected(self):
        transcript=self.base/'trace';transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':'wrong'}})+'\n')
        with self.assertRaisesRegex(ValueError,'session ID'):
            capture.export(self.store,{'session_id':'expected','turn_id':'one','transcript_path':str(transcript)},self.base/'result.zip')

    def test_future_turn_files_are_not_included_and_latest_has_original_names(self):
        current=self.base/'current.txt';current.write_text('current')
        future=self.base/'future.txt';future.write_text('future')
        for turn,path in [('one',current),('two',future)]:
            capture.capture(self.store,{'tool_name':'read_file','tool_input':{'path':str(path)},'cwd':str(self.base),'turn_id':turn},'PreToolUse')
        transcript=self.base/'trace';transcript.write_text(json.dumps({'type':'event_msg','payload':{'type':'task_started','turn_id':'one'}})+'\n')
        output=self.base/'result.zip'
        report=capture.export(self.store,{'session_id':'expected','turn_id':'one','transcript_path':str(transcript),'cwd':str(self.base)},output)
        self.assertEqual(report['file_versions'],1)
        with zipfile.ZipFile(output) as z:
            index=json.loads(z.read('files/paths.json'))
            self.assertEqual([x['source'] for x in index],[str(current)])
            self.assertEqual(z.read(index[0]['archive']),b'current')


if __name__=='__main__':unittest.main()
