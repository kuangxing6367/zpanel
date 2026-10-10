# -*- coding: utf-8 -*-
"""应用商店（官方扩展）

**装完面板第一眼「有东西」的来源。** 应用 = 单二进制/单目录的成品服务
（filebrowser、vaultwarden、rclone…），不绑 Docker —— 与面板「轻量 + Windows」
的定位一致。

- 应用清单从 **官方源** 拉（zkg.official_source 同一个 GitHub 仓库的
  appstore/index.json），本地 appstore/ 目录可放自定义应用；
- 安装 = 任务系统里的完整流水线：下载（支持 github_proxy）→ sha256 校验 →
  解包到 /opt/zpanel-apps/<id>/ → 生成 systemd 单元 → 启动 → 端口探测；
- 已装应用即一台机器上的 systemd 服务：启停走 svcmgr，卸载删目录删单元。
"""
import json
import logging
import shutil
import os

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "应用商店",
    "version": "0.1.0",
    "author": "ZPanel",
    "desc": "单二进制应用的一键安装/启停/卸载（官方源分发）",
    "priority": 34,
    "official": True,
}

_apps_dir = "/opt/zpanel-apps"
_svc = None


def _project_root() -> str:
    try:
        from core.kernel.paths import project_root
        return project_root()
    except Exception:
        return os.getcwd()


class AppStore:
    def __init__(self, fw, log=None, procs=None, pkg=None, svcmgr=None):
        self.fw = fw
        self._log = log or (lambda m: logger.info(m))
        self.procs = procs
        self.pkg = pkg
        self.svcmgr = svcmgr

    # ── 清单 ──────────────────────────────────────────────
    def _local_apps(self) -> dict:
        d = os.path.join(_project_root(), 'appstore')
        out = {}
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if not fn.endswith('.json'):
                    continue
                try:
                    with open(os.path.join(d, fn), encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    logger.warning('[appstore] 本地应用 %s 解析失败: %s', fn, e)
                    continue
                # 两种形态都认：目录包装（{'apps': [...]}) 与单应用文件（{id:...}）
                entries = data.get('apps') if isinstance(data, dict) and 'apps' in data else [data]
                for a in entries:
                    if isinstance(a, dict) and a.get('id') and not a.get('disabled'):
                        out[a['id']] = a
        return out


    _catalog_cache = {'apps': None, 'at': 0.0}

    def catalog(self) -> dict:
        """官方源清单 + 本地 appstore/ 目录合并（本地覆盖同名官方项）。"""
        import time as _t
        cached = self._catalog_cache
        if cached['apps'] is not None and _t.time() - cached['at'] < 600:
            return cached['apps']
        remote, err = {}, ''
        src = (self.fw.config.get('zkg') or {}).get('official_source') or ''
        if src:
            try:
                import urllib.request
                url = src.rstrip('/') + '/../../appstore/index.json'
                # official_source 形如 .../main/repo → 应用清单在同仓库 appstore/ 下
                url = src.rstrip('/').rsplit('/repo', 1)[0] + '/appstore/index.json'
                with urllib.request.urlopen(urllib.request.Request(
                        url, headers={'User-Agent': 'zpanel'}), timeout=15) as r:
                    data = json.loads(r.read().decode('utf-8', 'replace'))
                    for a in data.get('apps') or []:
                        remote[a['id']] = a
            except Exception as e:
                err = f'官方源不可达: {e}'
        merged = {**remote, **self._local_apps()}
        out = {'apps': sorted(merged.values(), key=lambda x: x.get('name', '')),
               'remote_error': err, 'remote_count': len(remote)}
        if merged:                       # 只有真拿到应用才缓存（空结果不缓存，便于重试）
            self._catalog_cache['apps'] = out
            self._catalog_cache['at'] = _t.time()
        return out

    def get_app(self, app_id: str) -> dict:
        return next((a for a in self.catalog()['apps'] if a['id'] == app_id), None)
    # ── 已装状态 ──────────────────────────────────────────
    def _inst_dir(self, app_id):
        return os.path.join(_apps_dir, app_id)

    def installed(self, app_id: str = '') -> list:
        """已装应用（/opt/zpanel-apps/<id>/ 存在即已装；状态查 systemd）。"""
        ids = [app_id] if app_id else (
            [d for d in os.listdir(_apps_dir) if os.path.isdir(os.path.join(_apps_dir, d))]
            if os.path.isdir(_apps_dir) else [])
        out = []
        for i in ids:
            meta_path = os.path.join(self._inst_dir(i), 'zpanel-app.json')
            meta = {}
            try:
                with open(meta_path, encoding='utf-8') as f:
                    meta = json.load(f)
            except Exception:
                continue
            unit = meta.get('unit') or ('app-' + i)
            st = self.svcmgr.status(unit) if self.svcmgr else {}
            out.append({'id': i, 'name': meta.get('name', i),
                        'version': meta.get('version', ''), 'unit': unit,
                        'port': meta.get('port'), 'running': bool(st.get('running')),
                        'found': bool(st.get('found'))})
        return out

    # ── 安装流水线（任务 runner）──────────────────────────
    def install(self, app_id: str, params: dict, task=None, task_id=None,
                timeout: float = 1200.0) -> dict:
        app = self.get_app(app_id)
        if not app:
            return {'ok': False, 'error': f'未知应用: {app_id}'}

        def _tlog(m):
            try:
                if task is not None and task_id:
                    task.log(task_id, m)
            except Exception:
                pass

        params = params or {}
        port = int(params.get('port') or app.get('default_port') or 8080)
        inst = self._inst_dir(app_id)
        unit_name = 'app-' + app_id
        _tlog(f"① 下载 {app['name']}…")

        import urllib.request
        url = str(app.get('url') or '').replace('{arch}', _arch())
        proxy = (self.fw.config.get('github_proxy') or '').strip()
        if proxy and 'github' in url:
            url = proxy.rstrip('/') + '/' + url
        # 下载落到安装盘的临时目录 —— /tmp 常是 tmpfs，os.replace 跨设备会炸
        tmp_dir = os.path.join(_apps_dir, '.tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        # 临时文件保留真实后缀 —— 解包格式按它判断（曾经用无后缀名，
        # tar.gz 被当成裸二进制直接装，systemd 报 Exec format error）
        lower_url = url.lower()
        suffix = ('.tar.gz' if lower_url.endswith(('.tar.gz', '.tgz'))
                  else '.tar.xz' if lower_url.endswith('.tar.xz')
                  else '.zip' if lower_url.endswith('.zip') else '')
        dest = os.path.join(tmp_dir, app_id + '-dl' + suffix)
        last_err = ''
        # 下载源候选：**镜像优先** —— GitHub 直连在国内不是快速失败而是挂死，
        # 先试直连会把分钟级超时耗光（实测踩过）。官方地址留作最后的兜底。
        cands = []
        if proxy:
            cands.append(proxy.rstrip('/') + '/' + url)
        if 'github.com' in url:
            cands.append(url.replace('https://github.com', 'https://ghfast.top/https://github.com'))
            cands.append(url.replace('https://github.com', 'https://gh-proxy.com/https://github.com'))
        cands.append(url)
        # 单次下载超时收紧：60 秒拿不到就换源（原来 300 秒会吃光整个任务窗口）
        dl_timeout = 60
        for attempt, u in enumerate(cands):
            try:
                _tlog(f"   {u[:100]}")
                with urllib.request.urlopen(urllib.request.Request(
                        u, headers={'User-Agent': 'zpanel'}), timeout=dl_timeout) as r, open(dest, 'wb') as f:
                    f.write(r.read(500 * 1024 * 1024))
                last_err = ''
                break
            except Exception as e:
                last_err = str(e)
                _tlog(f"   下载失败: {e}（换源重试）")
        if last_err:
            return {'ok': False, 'error': f'下载失败: {last_err}'}

        if app.get('sha256'):
            import hashlib
            h = hashlib.sha256()
            with open(dest, 'rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(chunk)
            if h.hexdigest() != app['sha256']:
                return {'ok': False, 'error': 'sha256 校验失败（文件被篡改或版本变更）'}
            _tlog("   sha256 校验通过")

        _tlog("② 解包安装…")
        os.makedirs(inst, exist_ok=True)
        import tarfile
        import zipfile
        _tlog(f"   归档类型：{suffix or '裸二进制'}")
        if suffix == '.zip':
            with zipfile.ZipFile(dest) as z:
                z.extractall(inst)
        elif suffix in ('.tar.gz', '.tar.xz'):
            with tarfile.open(dest) as t:
                t.extractall(inst)
        else:
            import shutil as _sh
            _sh.move(dest, os.path.join(inst, app.get('binary_name') or app_id))
        try:
            _sh.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        os.chmod(inst, 0o755)

        # 找出真正的可执行文件（应用定义 binary 相对路径；找不到就挑目录里唯一可执行）
        bin_rel = app.get('binary') or ''
        bin_path = os.path.join(inst, bin_rel) if bin_rel else ''
        if not bin_path or not os.path.isfile(bin_path):
            cands = [os.path.join(dp, f) for dp, _, fs in os.walk(inst) for f in fs
                     if os.access(os.path.join(dp, f), os.X_OK)
                     and not f.endswith(('.txt', '.md', '.service'))]
            if not cands:
                return {'ok': False, 'error': '包里没有可执行文件'}
            bin_path = min(cands, key=len)
        os.chmod(bin_path, 0o755)

        _tlog("③ 生成 systemd 单元并启动…")
        args = ' '.join(str(a).format(port=port, install_dir=inst)
                        for a in (app.get('args') or []))
        unit_content = (f"[Unit]\nDescription={app.get('name', app_id)} (zpanel app)\n"
                f"After=network.target\n\n[Service]\n"
                f"WorkingDirectory={inst}\n"
                f"ExecStart={bin_path} {args}\n"
                f"Restart=on-failure\nRestartSec=5\n\n[Install]\n"
                f"WantedBy=multi-user.target\n")
        unit_path = f"/etc/systemd/system/{unit_name}.service"
        with open(unit_path, 'w', encoding='utf-8') as f:
            f.write(unit_content)
        self.procs.run(['systemctl', 'daemon-reload'], timeout=30, shell=False)
        r = self.procs.run(['systemctl', 'enable', '--now', unit_name], timeout=60, shell=False)

        with open(os.path.join(inst, 'zpanel-app.json'), 'w', encoding='utf-8') as f:
            json.dump({'id': app_id, 'name': app.get('name', app_id),
                       'version': app.get('version', ''), 'unit': unit_name,
                       'port': port if app.get('web', True) else None}, f)

        # 端口探测（web 应用给访问地址）
        url, port_ok = '', False
        if app.get('web', True):
            import time as _t
            import socket as _s
            for _ in range(12):
                _t.sleep(2)
                try:
                    with _s.create_connection(('127.0.0.1', port), timeout=1):
                        port_ok = True
                        break
                except OSError:
                    continue
            url = f"http://{{host}}:{port}"
            _tlog(f"④ 端口 {port} {'可达' if port_ok else '暂不可达'}")
        else:
            _tlog("④ 单元已注册（无 Web 端口）")
        # 成功判定：web 应用看端口可达（systemd 状态常慢半拍），否则看单元状态
        running = port_ok or (self.svcmgr.status(unit_name).get('running')
                              if self.svcmgr else False)
        return {'ok': bool(running), 'unit': unit_name, 'port': port, 'url': url,
                'install_dir': inst, 'version': app.get('version', ''),
                'error': '' if running else '单元已注册但服务未运行（看 journalctl -u %s）' % unit_name}

    def remove(self, app_id: str) -> dict:
        meta_path = os.path.join(self._inst_dir(app_id), 'zpanel-app.json')
        unit_name = 'app-' + app_id
        try:
            with open(meta_path, encoding='utf-8') as f:
                unit_name = json.load(f).get('unit') or unit_name
        except Exception:
            pass
        self.procs.run(['systemctl', 'disable', '--now', unit_name], timeout=60, shell=False)
        try:
            os.remove(f'/etc/systemd/system/{unit_name}.service')
        except OSError:
            pass
        self.procs.run(['systemctl', 'daemon-reload'], timeout=30, shell=False)
        import shutil
        shutil.rmtree(self._inst_dir(app_id), ignore_errors=True)
        return {'ok': True}


def _arch() -> str:
    import platform
    m = platform.machine().lower()
    return {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(m, m)


def register(ctx):
    global _svc
    fw = ctx._framework
    cfg = fw.config.get('appstore') or {}
    if cfg.get('enabled', True) is False:
        ctx.log("应用商店已禁用 (appstore.enabled: false)")
        return
    procs = ctx.zkg_tool('procs')
    pkg = ctx.zkg_tool('pkg')
    svcmgr = ctx.zkg_tool('svcmgr')
    if procs is None or svcmgr is None:
        raise RuntimeError("应用商店缺少机制包 procs/svcmgr")
    _svc = AppStore(fw, log=ctx.log, procs=procs, pkg=pkg, svcmgr=svcmgr)
    fw.services.register('appstore', _svc)

    def _ok(d):
        return {'ok': True, 'data': d}

    def _h_install(a):
        data = a or {}
        app_id = str(data.get('id') or '')
        if not _svc.get_app(app_id):
            return {'ok': False, 'data': {'error': f'未知应用: {app_id}'}}
        tq = getattr(fw, 'task_queue', None)
        if tq is None:
            return {'ok': False, 'data': {'error': '任务队列不可用'}}
        params = data.get('params') or {}

        def _run(task_id=None):
            return _svc.install(app_id, params, task=tq, task_id=task_id)

        tid = tq.submit(_run, name='安装应用 ' + app_id,
                        meta={'kind': 'app.install', 'app': app_id})
        return {'ok': True, 'data': {'task_id': tid, 'id': app_id, 'async': True}}

    for name, fn, desc in (
        ('app.catalog', lambda a: _ok(_svc.catalog()), '应用清单（官方源+本地）'),
        ('app.installed', lambda a: _ok(_svc.installed((a or {}).get('id', ''))), '已装应用与状态'),
        ('app.install', _h_install, '一键安装（后台任务）'),
        ('app.remove', lambda a: _ok(_svc.remove((a or {}).get('id', ''))), '卸载应用'),
        ('app.act', lambda a: _ok(_svcmgr.act(
            (a or {}).get('action', 'restart'),
            'app-' + str((a or {}).get('id', '')), timeout=40)), '应用启停'),
    ):
        fw.nodes.register_handler(name, fn, desc=desc, level='software')

    ctx.log(f"应用商店已就绪（官方源 {'已配置' if (fw.config.get('zkg') or {}).get('official_source') else '未配置，仅本地应用'}）")
