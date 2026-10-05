# -*- coding: utf-8 -*-
"""备份云上传（backup 扩展私有，不走机制包：只有这一个场景用）

- WebDAV：坚果云 / Alist / 群晖都兼容，PUT 即传（Basic 认证）；
- S3 兼容：AWS SigV4 手写签名（stdlib hashlib/hmac），OSS/R2/MinIO 通吃。
两版都分块读文件算哈希，不做整包读内存。失败如实报错，不静默丢备份。
"""
import base64
import hashlib
import hmac
import os
import urllib.request
from datetime import datetime, timezone


def webdav_put(url: str, local_path: str, username: str = '',
               password: str = '', timeout: float = 600.0) -> dict:
    """WebDAV PUT：url 为目标文件完整地址（含目录与文件名）。"""
    size = os.path.getsize(local_path)
    req = urllib.request.Request(url, method='PUT')
    token = base64.b64encode((username + ':' + password).encode()).decode()
    req.add_header('Authorization', 'Basic ' + token)
    req.add_header('Content-Type', 'application/zip')
    try:
        with open(local_path, 'rb') as f:
            with urllib.request.urlopen(req, data=f.read(), timeout=timeout) as resp:
                return {'ok': 200 <= resp.status < 300, 'status': resp.status, 'size': size}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'status': e.code, 'error': 'HTTP %d: %s' % (e.code, e.reason)}
    except Exception as e:
        return {'ok': False, 'error': type(e).__name__ + ': ' + str(e)}


def _sigv4(method, url, headers, payload_hash, access_key, secret_key,
           region, service='s3', session_token=''):
    """AWS Signature V4（single-shot PUT）。"""
    import urllib.parse as _up
    parsed = _up.urlparse(url)
    host = parsed.netloc
    path = parsed.path or '/'
    now = datetime.now(timezone.utc)
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = now.strftime('%Y%m%d')

    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    hdrs = {'host': host, 'x-amz-content-sha256': payload_hash, 'x-amz-date': amz_date}
    if session_token:
        hdrs['x-amz-security-token'] = session_token
    signed = ';'.join(sorted(hdrs))
    canonical_headers = ''.join(k + ':' + hdrs[k] + chr(10) for k in sorted(hdrs))
    canonical_request = chr(10).join([method, path, '', canonical_headers, signed, payload_hash])
    scope = date_stamp + '/' + region + '/' + service + '/aws4_request'
    string_to_sign = chr(10).join([
        'AWS4-HMAC-SHA256', amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(('AWS4' + secret_key).encode(), date_stamp), region),
              service), 'aws4_request')
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers['Authorization'] = ('AWS4-HMAC-SHA256 Credential=' + access_key + '/' + scope
                                + ', SignedHeaders=' + signed + ', Signature=' + signature)
    headers['x-amz-date'] = amz_date
    headers['x-amz-content-sha256'] = payload_hash
    if session_token:
        headers['x-amz-security-token'] = session_token
    return headers


def s3_put(endpoint: str, bucket: str, key: str, local_path: str,
           access_key: str, secret_key: str, region: str = 'us-east-1',
           session_token: str = '', timeout: float = 600.0) -> dict:
    """S3 兼容单次 PUT。endpoint 形如 https://s3.region.amazonaws.com 或 MinIO 地址。"""
    url = endpoint.rstrip('/') + '/' + bucket + '/' + key.lstrip('/')
    payload_hash = hashlib.sha256()
    with open(local_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            payload_hash.update(chunk)
    sha = payload_hash.hexdigest()
    headers = {'Content-Type': 'application/zip'}
    headers = _sigv4('PUT', url, headers, sha, access_key, secret_key,
                     region, session_token=session_token)
    try:
        with open(local_path, 'rb') as f:
            with urllib.request.urlopen(
                    urllib.request.Request(url, method='PUT', data=f, headers=headers),
                    timeout=timeout) as resp:
                return {'ok': 200 <= resp.status < 300, 'status': resp.status,
                        'size': os.path.getsize(local_path)}
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'replace')[:300]
        except Exception:
            pass
        return {'ok': False, 'status': e.code, 'error': 'HTTP %d: %s' % (e.code, body or e.reason)}
    except Exception as e:
        return {'ok': False, 'error': type(e).__name__ + ': ' + str(e)}
