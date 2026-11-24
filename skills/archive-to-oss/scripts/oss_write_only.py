"""OSS uploader using PutObject permission only; never HEAD/GET/list/abort."""
import base64
import email.utils
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PART_SIZE = 8 * 1024 * 1024


def load_credentials(path):
    value = json.loads(Path(path).read_text())
    for key in ('access_key_id', 'access_key_secret'):
        if not value.get(key): raise ValueError('Upload credential is missing: ' + key)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


class OSS:
    def __init__(self, credentials, endpoint):
        self.credentials = credentials
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != 'https' or not re.fullmatch(r'oss-[a-z0-9-]+\.aliyuncs\.com', parsed.hostname or ''):
            raise ValueError('Expected a verified HTTPS Alibaba OSS endpoint')
        self.endpoint = parsed.hostname
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, bucket, key, body=b'', query=None, headers=None):
        query = query or {}
        headers = {'Content-Type': 'application/octet-stream', **(headers or {})}
        headers['Date'] = email.utils.formatdate(usegmt=True)
        headers['Content-MD5'] = base64.b64encode(hashlib.md5(body).digest()).decode()
        if self.credentials.get('security_token'):
            headers['x-oss-security-token'] = self.credentials['security_token']
        canonical = '/' + bucket + '/' + key
        if query:
            canonical += '?' + '&'.join(k + ('=' + str(v) if v != '' else '') for k, v in sorted(query.items()))
        oss_headers = ''.join(k.lower() + ':' + str(v).strip() + '\n'
            for k, v in sorted(headers.items(), key=lambda pair: pair[0].lower()) if k.lower().startswith('x-oss-'))
        message = '\n'.join([method, headers['Content-MD5'], headers['Content-Type'], headers['Date'], oss_headers + canonical])
        signature = base64.b64encode(hmac.new(self.credentials['access_key_secret'].encode(), message.encode(), hashlib.sha1).digest()).decode()
        headers['Authorization'] = 'OSS ' + self.credentials['access_key_id'] + ':' + signature
        url = 'https://' + bucket + '.' + self.endpoint + '/' + urllib.parse.quote(key, safe='/')
        if query: url += '?' + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=180) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as error:
                payload = error.read(32768)
                try: code = ET.fromstring(payload).findtext('Code') or 'UnknownError'
                except ET.ParseError: code = 'HTTPError'
                if error.code >= 500 and method == 'PUT' and attempt < 2:
                    time.sleep(attempt + 1); continue
                # Never echo credential-bearing request objects, signed URLs or full service bodies.
                raise RuntimeError('OSS request failed: HTTP %s %s' % (error.code, code)) from None
            except urllib.error.URLError:
                if method == 'PUT' and attempt < 2:
                    time.sleep(attempt + 1); continue
                raise RuntimeError('OSS connection failed; check endpoint and network') from None

    def put_file(self, path, bucket, key, sha256, progress=None, part_size=PART_SIZE):
        path = Path(path)
        original = path.stat()
        size = original.st_size
        metadata = {'x-oss-object-acl': 'private', 'x-oss-forbid-overwrite': 'true',
                    'x-oss-meta-sha256': sha256, 'Content-Type': 'application/zip'}
        def check_unchanged():
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
                raise RuntimeError('Archive changed during upload')
        def check_etag(headers, data):
            etag = next((v for k, v in headers.items() if k.lower() == 'etag'), '').strip('"').lower()
            if etag and etag != hashlib.md5(data).hexdigest():
                raise RuntimeError('OSS response ETag did not match uploaded data')
        if size <= part_size:
            data = path.read_bytes(); check_unchanged()
            status, headers, _ = self.request('PUT', bucket, key, data, headers=metadata)
            if status != 200: raise RuntimeError('Unexpected OSS upload status')
            check_etag(headers, data)
            if progress: progress(size, size)
            return {'transfer': 'simple', 'checksum': 'Content-MD5 verified by OSS', 'parts': 1}
        _, _, body = self.request('POST', bucket, key, query={'uploads': ''}, headers=metadata)
        upload_id = ET.fromstring(body).findtext('UploadId')
        if not upload_id: raise RuntimeError('OSS did not return an upload ID')
        parts, offset = [], 0
        with path.open('rb') as source:
            while True:
                chunk = source.read(part_size)
                if not chunk: break
                number = len(parts) + 1
                status, headers, _ = self.request('PUT', bucket, key, chunk,
                    query={'partNumber': number, 'uploadId': upload_id})
                if status != 200: raise RuntimeError('Unexpected OSS part status')
                check_etag(headers, chunk)
                etag = next((v for k, v in headers.items() if k.lower() == 'etag'), '').strip('"')
                if not etag: raise RuntimeError('OSS part response was missing ETag')
                parts.append((number, etag)); offset += len(chunk)
                if progress: progress(offset, size)
        check_unchanged()
        document = ET.Element('CompleteMultipartUpload')
        for number, etag in parts:
            part = ET.SubElement(document, 'Part')
            ET.SubElement(part, 'PartNumber').text = str(number)
            ET.SubElement(part, 'ETag').text = etag
        status, _, body = self.request('POST', bucket, key, ET.tostring(document),
            query={'uploadId': upload_id}, headers={'Content-Type': 'application/xml'})
        if status != 200: raise RuntimeError('Unexpected OSS completion status')
        if ET.fromstring(body).tag == 'Error': raise RuntimeError('OSS completion returned an embedded error')
        return {'transfer': 'multipart', 'checksum': 'Content-MD5 and ETag verified per part', 'parts': len(parts)}
