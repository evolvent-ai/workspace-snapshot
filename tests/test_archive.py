import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('archive', Path(__file__).resolve().parents[1] / 'skills/archive-to-oss/scripts/archive.py')
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / '仓库 with spaces'
        self.repo.mkdir()
        (self.repo / '未提交.py').write_text('print("working tree")')

    def test_archive_contents_and_secret_exclusions(self):
        (self.repo / '.env').write_text('TOKEN=secret')
        (self.repo / '.env.example').write_text('TOKEN=')
        (self.repo / 'node_modules').mkdir()
        (self.repo / 'node_modules/x').write_text('skip')
        (self.repo / '.git').mkdir()
        (self.repo / '.git/HEAD').write_text('skip')
        (self.repo / 'link').symlink_to(self.base / 'outside')
        plan = archive.make_plan([str(self.repo), str(self.repo / '未提交.py')])
        result = archive.build(plan, self.base / 'result.zip')
        self.assertEqual(result['file_count'], 2)
        with zipfile.ZipFile(result['archive']) as z:
            names = z.namelist()
            self.assertTrue(any(n.endswith('未提交.py') for n in names))
            manifest = json.loads(z.read('MANIFEST.json'))
            self.assertEqual(len(manifest['skipped']), 4)
            for entry in manifest['files']:
                import hashlib
                self.assertEqual(hashlib.sha256(z.read(entry['archive'])).hexdigest(), entry['sha256'])

    def test_stale_plan_fails_without_partial_output(self):
        plan = archive.make_plan([str(self.repo)])
        (self.repo / '未提交.py').write_text('changed')
        with self.assertRaises(ValueError): archive.build(plan, self.base / 'result.zip')
        self.assertFalse((self.base / 'result.zip').exists())
        self.assertFalse(list(self.base.glob('*.partial')))

    def test_missing_source_fails(self):
        with self.assertRaises(ValueError): archive.make_plan([str(self.base / 'missing')])

    def test_symlink_root_fails(self):
        link = self.base / 'link'
        link.symlink_to(self.repo)
        with self.assertRaises(ValueError): archive.make_plan([str(link)])

    def test_output_cannot_be_inside_source(self):
        with self.assertRaises(ValueError):
            archive.build(archive.make_plan([str(self.repo)]), self.repo / 'result.zip')

    def test_upload_requires_private_acl_and_correct_metadata(self):
        result = archive.build(archive.make_plan([str(self.repo)]), self.base / 'result.zip')
        with patch.object(archive, 'load_credentials', return_value={'access_key_id':'test','access_key_secret':'test'}), patch.object(archive, 'OSS') as client:
            client.return_value.put_file.return_value={'transfer':'simple','checksum':'Content-MD5 verified by OSS','parts':1}
            receipt = archive.upload(result['archive'])
            self.assertEqual(receipt['acl'], 'private')
            self.assertTrue(receipt['oss_uri'].startswith('oss://seed-data/workspace/'))
            client.return_value.put_file.assert_called_once()

    def test_same_named_roots_remain_distinct(self):
        other = self.base / 'another' / self.repo.name
        other.mkdir(parents=True)
        (other / '未提交.py').write_text('second')
        plan = archive.make_plan([str(self.repo), str(other)])
        self.assertEqual(len({f['archive'] for f in plan['files']}), 2)


if __name__ == '__main__': unittest.main()
