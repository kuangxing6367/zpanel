# -*- coding: utf-8 -*-
"""
SSL 证书管理（官方扩展）

两件事，边界说清楚：

1. **到期巡检**：
   - 本地 PEM 证书（data/ssl/*.pem / *.crt）：标准库 ssl 解析到期时间（不引第三方库）；
   - 远端站点：socket + ssl 直连目标 `host:port`，抓真实下发的证书看到期 ——
     这是巡检的真相（SNI 也会带上），不依赖本地文件。

2. **自签证书**：调系统 `openssl` 生成自签 PEM（有 openssl 才可用；没有就明说）。
   **不做 ACME 自动签发** —— Let's Encrypt 需要域名校验回调与续期守护，
   属于独立的服务，面板第一版不装这个假能耐。

到期阈值判定给规则用（alerts 扩展可以引用 ssl.check 做告警源）。
"""
import logging
import os
import ssl as _ssl
import socket
import time

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "SSL证书",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "证书到期巡检（本地 PEM + 远端直连）与自签证书生成（openssl）",
    "priority": 58,
    "official": True,
}

_svc = None

WARN_DAYS = 30          # 默认告警阈值


def _cert_expire(cert_der: bytes):
    """PEM/DER → notAfter 时间戳。只依赖标准库 ssl。"""
    import tempfile
    pem = cert_der
    if b'-----BEGIN' not in pem:
        b64 = __import__('base64').encodebytes(cert_der).decode()
        pem = (b'-----BEGIN CERTIFICATE-----\n' + b64.encode()
               + b'-----END CERTIFICATE-----\n')
    # 标准库 _ssl._test_decode_cert 需要文件；落临时文件解析
    with tempfile.NamedTemporaryFile('wb', suffix='.pem', delete=False) as f:
        f.write(pem)
        path = f.name
    try:
        info = _ssl._ssl._test_decode_cert(path) \
            if hasattr(_ssl, '_ssl') else _ssl._test_decode_cert(path)
        na = info.get('notAfter')
        if not na:
            return None, None
        expire = time.mktime(time.strptime(na, '%b %d %H:%M:%S %Y %Z'))
        return expire, info
    except Exception:
        return None, None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class SslService:
    def __init__(self, log=None, sandbox=None, fileops=None):
        if fileops is None:
            raise RuntimeError("SSL 扩展依赖 fileops 机制包（检查 manifest.toml）")
        self._fo = fileops
        self._log = log or (lambda m: logger.info(m))
        self.root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), 'data', 'ssl')
        os.makedirs(self.root, exist_ok=True)
        self.box = sandbox.Sandbox([self.root]) if sandbox else None

    # ── 本地证书 ────────────────────────────────────────────
    def local(self) -> dict:
        out = []
        for fn in sorted(os.listdir(self.root)):
            if not fn.lower().endswith(('.pem', '.crt', '.cer')):
                continue
            p = os.path.join(self.root, fn)
            try:
                expire, info = _cert_expire(self._fo.read_bytes(p)['data'])
            except (OSError, ValueError):
                continue
            days = None if expire is None else int((expire - time.time()) // 86400)
            out.append({'file': fn, 'subject': (info or {}).get('subject', ''),
                        'expire': expire or 0, 'expire_str':
                        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire)) if expire else '解析失败',
                        'days_left': days,
                        'warn': days is not None and days < WARN_DAYS})
        return {'count': len(out), 'dir': 'data/ssl', 'warn_days': WARN_DAYS, 'certs': out}

    # ── 远端巡检 ────────────────────────────────────────────
    def check(self, host: str, port: int = 443, timeout: float = 8.0) -> dict:
        host = str(host or '').strip()
        if not host:
            return {'ok': False, 'error': 'host 不能为空'}
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False       # 巡检目的是看证书本身，不是校验信任链
        ctx.verify_mode = _ssl.CERT_NONE
        t0 = time.time()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
            expire, info = _cert_expire(der)
            if expire is None:
                return {'ok': False, 'error': '证书解析失败'}
            days = int((expire - time.time()) // 86400)
            return {'ok': True, 'host': host, 'port': int(port),
                    'subject': info.get('subject', ''), 'issuer': info.get('issuer', ''),
                    'expire': expire,
                    'expire_str': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire)),
                    'days_left': days,
                    'warn': days < WARN_DAYS, 'latency_ms': int((time.time() - t0) * 1000)}
        except Exception as e:
            return {'ok': False, 'host': host, 'port': int(port),
                    'error': f'{type(e).__name__}: {e}', 'latency_ms': int((time.time() - t0) * 1000)}

    # ── 自签证书 ────────────────────────────────────────────
    @staticmethod
    def openssl_available() -> bool:
        import shutil
        return bool(shutil.which('openssl'))

    def self_signed(self, domain: str, days: int = 825, bits: int = 2048) -> dict:
        import shutil
        import subprocess
        domain = str(domain or '').strip().lower()
        if not domain:
            return {'ok': False, 'error': '域名不能为空'}
        exe = shutil.which('openssl')
        if not exe:
            return {'ok': False, 'error': '系统未安装 openssl，无法自签（可改用远端巡检监听到期）'}
        key = os.path.join(self.root, f'{domain}.key')
        crt = os.path.join(self.root, f'{domain}.pem')
        cmd = [exe, 'req', '-x509', '-newkey', f'rsa:{bits}', '-nodes',
               '-keyout', key, '-out', crt, '-days', str(int(days)),
               '-subj', f'/CN={domain}']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {'ok': False, 'error': (r.stderr or r.stdout)[:300]}
        self._log(f"自签证书 {domain}（{days} 天）→ {crt}")
        return {'ok': True, 'cert': crt, 'key': key, 'days': int(days),
                'note': '自签证书浏览器会告警，仅用于内网/回环加密'}

    def read(self, name: str) -> dict:
        """读证书/私钥文本（文件名白名单 = data/ssl 下真实存在的文件）。"""
        p = os.path.join(self.root, os.path.basename(name))
        if not os.path.isfile(p):
            return {'ok': False, 'error': f'文件不存在: {name}'}
        r = self._fo.read_bytes(p, max_bytes=200000)
        return {'ok': True, 'name': os.path.basename(name),
                'content': self._fo.decode(r['data'])}


def register(ctx):
    global _svc
    fw = ctx._framework
    sandbox = ctx.zkg_tool('sandbox')
    fileops = ctx.zkg_tool('fileops')
    if fileops is None:
        ctx.log("SSL：缺少 fileops 机制包，已跳过")
        return
    _svc = SslService(log=ctx.log, sandbox=sandbox, fileops=fileops)
    fw.ssl_svc = _svc
    fw.services.register('ssl', _svc)

    def _ok(d):
        return {'ok': True, 'data': d}

    # ── ACME 签发（后台任务：网络轮询分钟级，进度逐段写日志）──
    def _issue_run(domains, staging, email, site_id, task=None, task_id=None):
        """签发全流程。挑战文件放 data/ssl/acme-challenge/（各站点 conf 已放行
        /.well-known/acme-challenge/）；签成 → 证书落 data/ssl/<主域名>/ →
        给了 site_id 就经命令通道回写站点 ssl 配置并重载引擎。"""
        def _tlog(m):
            try:
                if task is not None and task_id:
                    task.log(task_id, m)
            except Exception:
                pass

        import importlib.util as _iu
        from core.kernel.paths import project_root
        root = project_root()
        ssl_dir = os.path.join(root, 'data', 'ssl')
        acme_root = os.path.join(ssl_dir, 'acme-challenge')
        os.makedirs(acme_root, exist_ok=True)
        main_domain = str(domains[0])
        key_dir = os.path.join(ssl_dir, main_domain)
        os.makedirs(key_dir, exist_ok=True)
        account_key = os.path.join(ssl_dir, 'acme-account.key')

        spec = _iu.spec_from_file_location('ssl_acme', os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'acme.py'))
        acme = _iu.module_from_spec(spec)
        spec.loader.exec_module(acme)

        directory = acme.LETSENCRYPT_STAGING if staging else acme.LETSENCRYPT_DIR
        _tlog(f"① ACME 目录（{'staging 测试' if staging else '生产'}）")
        client = acme.AcmeClient(directory, account_key, log=_tlog)
        _tlog("② 注册/复用 ACME 账号…")
        client.ensure_account(email or '', terms_agreed=True)
        _tlog(f"③ 新建订单：{', '.join(domains)}")
        order = client.new_order(domains)
        order_url = client.order_url

        authz_urls = order.get('authorizations') or []
        if not authz_urls:
            return {'ok': False, 'error': '订单无 authorizations'}
        thumb = acme._thumbprint(client.jwk)
        placed = []
        _tlog(f"④ 放置 {len(authz_urls)} 个 http-01 挑战文件…")
        for authz_url in authz_urls:
            authz = client.get_authz(authz_url)
            ident = ((authz.get('identifier') or {}).get('value')) or ''
            for ch in authz.get('challenges') or []:
                if ch.get('type') == 'http-01':
                    token = ch['token']
                    with open(os.path.join(acme_root, token), 'w', encoding='utf-8') as f:
                        f.write(token + '.' + thumb)
                    placed.append((authz_url, ch['url'], ident))
                    _tlog('   ' + (ident or '?') + ' 挑战文件就位')
        if not placed:
            return {'ok': False, 'error': '没有 http-01 挑战可响应'}

        _tlog("⑤ 响应挑战，等待 CA 验证（CA 会回源访问 http://域名/.well-known/…）")
        for authz_url, ch_url, ident in placed:
            client.respond_challenge(ch_url)
        for authz_url, ch_url, ident in placed:
            authz = client.poll(authz_url, 'valid', timeout=120.0, log=_tlog)
            _tlog('   ' + (ident or '?') + ' 验证通过')

        _tlog("⑥ 生成站点密钥与 CSR…")
        domain_key = os.path.join(key_dir, 'privkey.pem')
        csr_path = os.path.join(key_dir, 'csr.der')
        acme.make_domain_key_and_csr(domains, domain_key, csr_path)
        _tlog("⑦ finalize…")
        with open(csr_path, 'rb') as f:
            order = client.finalize(order['finalize'], f.read())
        order = client.poll(order_url, 'valid', timeout=60.0, log=_tlog)

        cert_url = order.get('certificate')
        if not cert_url:
            return {'ok': False, 'error': '订单完成但缺 certificate 下载地址'}
        _tlog("⑧ 下载证书链…")
        st, pem, _ = client._post(cert_url, None)
        fullchain = os.path.join(key_dir, 'fullchain.pem')
        with open(fullchain, 'w', encoding='utf-8') as f:
            f.write(pem if isinstance(pem, str) else json.dumps(pem))
        _tlog(f"⑨ 证书已落盘：{fullchain}")

        applied = False
        if site_id:
            _tlog("⑩ 回写站点 ssl 配置并重载引擎…")
            try:
                fw.nodes.send_cmd('localhost', 'sites.update',
                                  {'id': site_id,
                                   'enable_ssl': True,
                                   'ssl_cert': fullchain,
                                   'ssl_key': domain_key}, 30)
                fw.nodes.send_cmd('localhost', 'sites.apply', {}, 60)
                applied = True
                _tlog('   站点已启用 HTTPS 并重载')
            except Exception as e:
                _tlog(f'   站点回写失败（证书已签出，可手动启用）: {e}')
        return {'ok': True, 'domains': domains, 'fullchain': fullchain,
                'privkey': domain_key, 'staging': staging, 'site_applied': applied}

    def _h_issue(a):
        data = a or {}
        domains = data.get('domains') or []
        if isinstance(domains, str):
            domains = [d.strip() for d in domains.replace(';', ',').split(',') if d.strip()]
        domains = [d for d in domains if d]
        if not domains:
            return {'ok': False, 'data': {'error': '域名不能为空'}}
        tq = getattr(fw, 'task_queue', None)
        if tq is None:
            return {'ok': False, 'data': {'error': '任务队列不可用，签发必须走任务'}}

        def _run(task_id=None):
            return _issue_run(domains,
                              staging=bool(data.get('staging')),
                              email=str(data.get('email') or ''),
                              site_id=str(data.get('site_id') or ''),
                              task=tq, task_id=task_id)

        tid = tq.submit(_run, name='签发证书 ' + domains[0],
                        meta={'kind': 'ssl.issue', 'domain': domains[0]})
        return {'ok': True, 'data': {'task_id': tid, 'domains': domains, 'async': True}}

    for name, fn, desc in (
        ('ssl.issue', _h_issue, 'ACME 签发（后台任务，http-01 webroot）'),
        ('ssl.local', lambda a: _ok(_svc.local()), '本地证书清单与到期'),
        ('ssl.check', lambda a: _ok(_svc.check((a or {}).get('host', ''),
                                           int((a or {}).get('port') or 443))), '远端证书巡检'),
        ('ssl.self_signed', lambda a: _ok(_svc.self_signed(
            (a or {}).get('domain', ''), int((a or {}).get('days') or 825))), '自签证书'),
        ('ssl.read', lambda a: _ok(_svc.read((a or {}).get('name', ''))), '读取证书文本'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    from flask import jsonify, request as _req

    def _body():
        return _req.get_json(silent=True) or {}

    ctx.register_api('/api/ssl/local', lambda: jsonify(_ok(_svc.local())), methods=['GET'])
    ctx.register_api('/api/ssl/check', lambda: jsonify(_svc.check(
        _body().get('host', ''), int(_body().get('port') or 443))), methods=['POST'])
    ctx.register_api('/api/ssl/self-signed', lambda: jsonify(_svc.self_signed(
        _body().get('domain', ''), int(_body().get('days') or 825))), methods=['POST'])

    ctx.log(f"SSL 证书已就绪（本地 {_svc.local()['count']} 张，"
            f"openssl {'可用' if _svc.openssl_available() else '不可用（无法自签）'}）")


def unregister():
    global _svc
    _svc = None
