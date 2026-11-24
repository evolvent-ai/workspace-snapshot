import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('write_only',Path(__file__).resolve().parents[1]/'skills/archive-to-oss/scripts/oss_write_only.py')
module=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(module)


class WriteOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.file=Path(self.temp.name)/'archive.zip';self.file.write_bytes(b'0123456789')
        self.client=module.OSS({'access_key_id':'fake','access_key_secret':'fake'},'https://oss-cn-hangzhou.aliyuncs.com')

    def test_simple_upload_uses_only_put_and_private_acl(self):
        calls=[]
        def request(method,bucket,key,body=b'',query=None,headers=None):
            calls.append((method,headers));return 200,{'ETag':hashlib.md5(body).hexdigest()},b''
        self.client.request=request
        self.client.put_file(self.file,'seed-data','workspace/test.zip','hash')
        self.assertEqual([c[0] for c in calls],['PUT'])
        self.assertEqual(calls[0][1]['x-oss-object-acl'],'private')

    def test_multipart_needs_no_read_list_or_abort_requests(self):
        calls=[]
        def request(method,bucket,key,body=b'',query=None,headers=None):
            calls.append((method,query or {}))
            if query and 'uploads' in query:return 200,{},b'<InitiateMultipartUploadResult><UploadId>test</UploadId></InitiateMultipartUploadResult>'
            if method=='PUT':return 200,{'ETag':hashlib.md5(body).hexdigest()},b''
            return 200,{},b'<CompleteMultipartUploadResult/>'
        self.client.request=request
        result=self.client.put_file(self.file,'seed-data','workspace/test.zip','hash',part_size=4)
        self.assertEqual(result['parts'],3)
        self.assertEqual([c[0] for c in calls],['POST','PUT','PUT','PUT','POST'])

    def test_bad_part_digest_fails(self):
        self.client.request=lambda *args,**kwargs:(200,{'ETag':'incorrect'},b'')
        with self.assertRaisesRegex(RuntimeError,'ETag'):self.client.put_file(self.file,'seed-data','workspace/test.zip','hash')

    def test_credentials_never_follow_redirects(self):
        redirect=module.NoRedirect()
        self.assertIsNone(redirect.redirect_request(None,None,302,'',{},'https://other.example'))


if __name__=='__main__':unittest.main()
