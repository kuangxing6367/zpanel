# -*- coding: utf-8 -*-
"""
站点子系统 —— 站点注册表、配置下发与后端编排

一条链路讲清楚它做什么：

    建站/改站 → 落库 → ① 更新内置服务的虚拟主机路由表
                      → ② 渲染 Nginx server 块并写入 data/sites/<id>.conf
                      → ③ 若 Nginx 可用则 reload（不可用则跳过，内置服务继续扛）

也就是**双后端**：Nginx 在就交给 Nginx，不在就内置服务兜底，
面板上的操作永远是一致的。这样「建站」不需要先装一堆东西才能用。
"""
from __future__ import annotations

import logging
import shutil
import os
import sys
import threading

import zkg                                  # 由 zkg loader 注入（见 service/zkg/api.py）

from software.extensions.sites.model import Site, KIND_PROXY, KIND_PHP

logger = logging.getLogger('zernus')


def _ngx():
    """ngxconf 机制包：Nginx 配置渲染 / 落盘 / 重载。"""
    mod = zkg.tool('ngxconf')
    if mod is None:
        raise RuntimeError(
            "站点托管依赖 ngxconf 机制包，但未被加载 —— "
            "检查 software/extensions/sites/manifest.toml 的 dependencies")
    return mod


def _vhost():
    """vhost 机制包：内置虚拟主机（可选兜底后端，默认关闭）。"""
    mod = zkg.tool('vhost')
    if mod is None:
        raise RuntimeError(
            "站点托管依赖 vhost 机制包，但未被加载 —— "
            "检查 software/extensions/sites/manifest.toml 的 dependencies")
    return mod

_DDL = """
CREATE TABLE IF NOT EXISTS sites (
    id           VARCHAR(64)  NOT NULL PRIMARY KEY,
    name         VARCHAR(100) NOT NULL,
    domains      VARCHAR(500) DEFAULT '',
    kind         VARCHAR(16)  DEFAULT 'static',
    root_dir     VARCHAR(500) DEFAULT '',
    target       VARCHAR(255) DEFAULT '',
    index_files  VARCHAR(255) DEFAULT 'index.html index.htm index.php',
    remark       VARCHAR(255) DEFAULT '',
    enable_ssl   INTEGER      DEFAULT 0,
    ssl_cert     VARCHAR(500) DEFAULT '',
    ssl_key      VARCHAR(500) DEFAULT '',
    php_fastcgi  VARCHAR(64)  DEFAULT '127.0.0.1:9000',
    php_version  VARCHAR(16)  DEFAULT '',
    created_at   VARCHAR(32)  DEFAULT NULL,
    updated_at   VARCHAR(32)  DEFAULT NULL
)
"""


class SiteSubsystem:
    """站点注册表 + 后端编排（内置服务 / Nginx）。"""

    def __init__(self, fw, log=None):
        self.fw = fw
        self._log = log or (lambda m: logger.info(m))
        self._lock = threading.RLock()
        self._sites = {}                    # id -> Site
        self._ready = False

        cfg = (fw.config.get('sites') or {}) if isinstance(fw.config, dict) else {}
        self.cfg = cfg
        # conf_dir 必须尊重 sites.nginx_conf —— 指向系统 nginx 的 include 目录
        # （Debian/CentOS 的 conf.d、或用户自定义）时，面板渲染的站点配置直接被
        # 系统 nginx 加载，这才是「管 nginx」而不是「另起炉灶」。
        # 之前这里硬编码 data/sites，nginx_conf 配置读了却没人用（死配置）。
        self.conf_dir = (str(cfg.get('nginx_conf') or '').strip()
                         or os.path.join(_project_root(), 'data', 'sites'))
        self.log_dir = os.path.join(_project_root(), 'data', 'logs', 'sites')
        # conf 里引用的 access/error 日志都落在这里 —— 目录不存在则 nginx -t 直接失败
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except OSError as e:
            self._log(f"[sites] 日志目录创建失败 {self.log_dir}: {e}")

        # 内置服务默认**关闭**。
        # 面板的职责是「管理 Nginx」，不是「替代 Nginx」——所以它不该去占 80 端口，
        # 更不该和系统里已有的 Nginx 抢端口。这里的内置服务只保留一个用途：
        # 没装 Nginx 时想先在浏览器里看一眼页面，显式开启即可（默认 127.0.0.1:8080）。
        self.builtin_enabled = bool(cfg.get('builtin_enabled', False))
        self.nginx_reload = bool(cfg.get('nginx_reload', True))
        # web 服务器适配器选择：auto = 按优先级探测（nginx → apache…），
        # 或显式指定 adapter_id（sites.webserver: apache）
        self.ws_pref = str(cfg.get('webserver') or 'auto').strip().lower()
        # 内置虚拟主机来自 vhost 机制包（面板自己不含 HTTP 服务实现）
        self.server = _vhost().VHostServer(
            host=str(cfg.get('builtin_host', '127.0.0.1')),
            port=int(cfg.get('builtin_port', 8080)),
            log=self._log,
            server_name='zpanel',
        )

    # ══════════════════════════════════════════════════════
    # 持久化
    # ══════════════════════════════════════════════════════
    def ensure_table(self) -> bool:
        if self._ready:
            return True
        try:
            ddl = _DDL
            if getattr(self.fw.db, 'db_type', 'sqlite') == 'mysql':
                ddl = ddl.rstrip() + ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
            self.fw.db.execute(ddl)
            self._migrate()
            self._ready = True
            return True
        except Exception as e:
            if 'exist' in str(e).lower():
                self._migrate()
                self._ready = True
                return True
            logger.error("[sites] 建表失败: %s", e)
            return False

    def _migrate(self) -> None:
        """老库加列（php_version）。不同数据库报错文案不同，按关键字吞掉。"""
        for col in ('php_version',):
            try:
                self.fw.db.execute(f"ALTER TABLE sites ADD COLUMN {col} VARCHAR(16) DEFAULT ''")
            except Exception as e:
                msg = str(e).lower()
                if 'duplicate' in msg or 'exist' in msg or 'already' in msg \
                        or 'no such' in msg:
                    continue
                logger.warning("[sites] 迁移列 %s 跳过: %s", col, e)

    def load_all(self) -> int:
        self.ensure_table()
        try:
            rows = self.fw.db.query("SELECT * FROM sites ORDER BY created_at") or []
        except Exception as e:
            logger.error("[sites] 读取站点失败: %s", e)
            return 0
        with self._lock:
            for row in rows:
                cfg = dict(row)
                cfg['domains'] = [d for d in str(cfg.get('domains') or '').split(',') if d]
                cfg['index'] = cfg.get('index_files') or ''
                self._sites[cfg['id']] = Site(cfg)
        self._log(f"已加载 {len(rows)} 个站点")
        return len(rows)

    def _save(self, site: Site) -> None:
        c = site.to_config()
        vals = (c['name'], ','.join(c['domains']), c['kind'], c['root_dir'],
                c['target'], c['index'], c['remark'], int(c['enable_ssl']),
                c['ssl_cert'], c['ssl_key'], c['php_fastcgi'], c['php_version'],
                c['updated_at'])
        try:
            if self.fw.db.query_one("SELECT id FROM sites WHERE id=?", (c['id'],)):
                self.fw.db.execute(
                    "UPDATE sites SET name=?, domains=?, kind=?, root_dir=?, target=?, "
                    "index_files=?, remark=?, enable_ssl=?, ssl_cert=?, ssl_key=?, "
                    "php_fastcgi=?, php_version=?, updated_at=? WHERE id=?", vals + (c['id'],))
            else:
                self.fw.db.execute(
                    "INSERT INTO sites (name, domains, kind, root_dir, target, "
                    "index_files, remark, enable_ssl, ssl_cert, ssl_key, php_fastcgi, "
                    "php_version, updated_at, id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    vals + (c['id'], c['created_at']))
        except Exception as e:
            logger.error("[sites] 保存站点 %s 失败: %s", c['id'], e)

    # ══════════════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════════════
    def list(self) -> list:
        with self._lock:
            out = []
            for s in self._sites.values():
                d = s.to_dict()
                d['root_exists'] = bool(s.root_dir and os.path.isdir(s.root_dir))
                d['backend'] = (self.webserver_select().get('active') or {}).get('id') \
                    or 'builtin'
                out.append(d)
            return out

    def get(self, sid: str) -> Site:
        with self._lock:
            return self._sites.get(str(sid))

    def count(self) -> dict:
        with self._lock:
            items = list(self._sites.values())
        return {
            'total': len(items),
            'static': sum(1 for s in items if s.kind == 'static'),
            'proxy': sum(1 for s in items if s.kind == KIND_PROXY),
            'php': sum(1 for s in items if s.kind == KIND_PHP),
            'ssl': sum(1 for s in items if s.enable_ssl),
        }

    def php_versions(self) -> list:
        """本机探测到的 PHP 版本（建站下拉用）。

        复用 compat（版本→事实）与 probe（多版本清点）两个机制包——
        不在这里重写探测逻辑。版本号按 compact 去重、按版本号倒序。
        """
        probe = zkg.tool('probe')
        compat = zkg.tool('compat')
        if not probe or not compat:
            return []
        plat = 'win' if sys.platform.startswith('win') else 'nix'
        try:
            items = probe.versions('php', exes=('php', 'php-cgi', 'php-fpm'),
                                    dirs=compat.expand(compat.roots('php', plat)),
                                    depth=3)
        except Exception as e:
            logger.warning("[sites] PHP 版本清点失败: %s", e)
            items = []
        seen, out = set(), []
        for it in items:
            v = str(it.get('version') or '').strip()
            if not v or v in seen:
                continue
            seen.add(v)
            lay = compat.resolve('php', v)
            out.append({
                'version': v,
                'ver': compat.compact(v),
                'layer': lay['id'],
                'layer_title': lay.get('title', ''),
                'socket': compat.php_fastcgi(v, plat),
                'exe': it.get('exe', ''),
                'path': it.get('path', ''),
            })
        out.sort(key=lambda x: compat.compare(x['version'], '0'), reverse=True)
        return out

    # ══════════════════════════════════════════════════════
    # 增删改
    # ══════════════════════════════════════════════════════
    def create(self, spec: dict) -> dict:
        site = Site(spec or {})
        err = site.validate()
        if err:
            return {'ok': False, 'error': err}
        with self._lock:
            clash = [d for d in site.domains
                     if any(d.lower() in [x.lower() for x in other.domains]
                            for other in self._sites.values())]
        if clash:
            return {'ok': False, 'error': f'域名已被其它站点占用: {", ".join(clash)}'}

        self.ensure_table()
        with self._lock:
            self._sites[site.id] = site
        self._save(site)
        self.apply()
        self._log(f"已创建站点 {site.name}（{' '.join(site.domains)}，{site.kind}）")
        return {'ok': True, 'site': site.to_dict()}

    def update(self, sid: str, data: dict) -> dict:
        site = self.get(sid)
        if site is None:
            return {'ok': False, 'error': f'站点 {sid} 不存在'}
        site.update(data or {})
        err = site.validate()
        if err:
            return {'ok': False, 'error': err}
        self._save(site)
        self.apply()
        return {'ok': True, 'site': site.to_dict()}

    def remove(self, sid: str) -> dict:
        site = self.get(sid)
        if site is None:
            return {'ok': False, 'error': f'站点 {sid} 不存在'}
        with self._lock:
            self._sites.pop(sid, None)
        try:
            self.fw.db.execute("DELETE FROM sites WHERE id=?", (sid,))
        except Exception as e:
            logger.error("[sites] 删除站点记录失败: %s", e)
        # 配置与日志一并清掉
        for p in (os.path.join(self.conf_dir, f'{sid}.conf'),
                  os.path.join(self.conf_dir, f'{sid}.access.log'),
                  os.path.join(self.conf_dir, f'{sid}.error.log')):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
        self.apply()
        self._log(f"已删除站点 {site.name}")
        return {'ok': True}

    # ══════════════════════════════════════════════════════
    # 下发
    # ══════════════════════════════════════════════════════
    def apply(self) -> dict:
        """把当前站点集合下发给 web 服务器适配器。

        顺序与优先级讲清楚：
          ① **适配器是主路径** —— 选定的引擎（auto 探测或显式指定）渲染+落盘，
             并（按配置）先 -t 再 reload。面板管引擎，不变成引擎。
          ② 内置服务是**可选兜底**，默认关闭；只有显式开启且没有可用引擎时才起，
             且只监听 127.0.0.1:8080 —— 绝不碰 80，绝不与系统 web 服务争端口。
          ③ 两者都没有时，站点依然可以「建」——配置照常生成，供你手工部署。
        """
        with self._lock:
            sites = list(self._sites.values())

        sel = self.webserver_select()
        active = sel.get('active')

        # ① 适配器：渲染+落盘并 reload（有可用引擎才做）
        written = self._deploy(sites)
        reloaded = ({'ok': False, 'note': '未检测到 web 服务器引擎，配置已生成待部署'}
                    if not active else
                    (self.reload_webserver() if self.nginx_reload else {'ok': True, 'note': '已跳过 reload'}))

        # ② 内置服务：仅显式开启时启动，且有引擎时不抢活
        served = [s for s in sites if s.kind != KIND_PHP]
        self.server.router.set_sites(served)
        if self.builtin_enabled and served and not active and not self.server.running:
            self.server.start()
        elif self.server.running and (active or not served or not self.builtin_enabled):
            self.server.stop()

        return {
            'ok': True,
            'backend': (active or {}).get('id') or ('builtin' if self.server.running else 'none'),
            'webserver': {'active': active, 'available': sel.get('available') or []},
            'confs': written,
            'nginx_reload': reloaded,
            'builtin_enabled': self.builtin_enabled,
            'builtin_running': self.server.running,
            'builtin_domains': self.server.router.size(),
        }

    def _deploy(self, sites: list) -> dict:
        """经 web 服务器适配器下发（渲染+落盘+清理托管文件都在实现包里做）。

        返回 {'written': [...], 'removed': [...], 'kept': [...], 'total': n}；
        kept 是**人手写的**配置文件（无托管标记），面板不碰它们。
        没有可用引擎时退回「仅生成配置」：用 ngxconf 渲染成文本落到 conf_dir，
        供手工部署 —— 承诺过的兜底，不是静默失败。
        """
        specs = [s.to_ngx_spec(log_dir=self.log_dir) for s in sites]
        try:
            ws = self._ws()
            if ws is not None:
                active = ws.select(self.ws_pref).get('active')
                if active:
                    impl = ws.impl_for(active['id'])
                    r = impl.deploy(specs, self.conf_dir)
                    r['adapter'] = active['id']
                    return r
            # 兜底：无引擎也把配置生成出来（nginx 文本，供手工部署）
            return _ngx().dump(self.conf_dir, specs)
        except Exception as e:
            logger.error("[sites] 下发站点配置失败: %s", e)
            return {'written': [], 'removed': [], 'kept': [], 'total': 0,
                    'error': f'{type(e).__name__}: {e}'}

    def preview_nginx(self, sid: str):
        """预览某站点的配置（跟随当前生效适配器；无引擎时按 nginx 文本）。"""
        site = self.get(sid)
        if site is None:
            return None
        sel = self.webserver_select()
        active = sel.get('active')
        if active:
            try:
                impl = ws_impl(self, active['id'])
                files = impl.render([site.to_ngx_spec(log_dir=self.log_dir)])
                return '\n\n'.join(files.values()) or None
            except Exception as e:
                logger.warning("[sites] 适配器预览失败，回落 nginx 文本: %s", e)
        return site.render_nginx(log_dir=self.log_dir)

    # ══════════════════════════════════════════════════════
    # Web 服务器适配器
    # ══════════════════════════════════════════════════════
    def _ws(self):
        return zkg.tool('webserver')

    def webserver_select(self) -> dict:
        """当前适配器选择：{active, available}；机制包不可用时如实空。"""
        ws = self._ws()
        if ws is None:
            return {'active': None, 'available': []}
        try:
            return ws.select(self.ws_pref)
        except Exception as e:
            logger.warning("[sites] webserver 选择失败: %s", e)
            return {'active': None, 'available': []}

    def webserver_available(self) -> bool:
        return bool(self.webserver_select().get('active'))

    def reload_webserver(self) -> dict:
        """重载当前引擎。**先 test 再 reload** —— 信号发出 ≠ 配置生效，
        语法不过就拒绝重载并如实报错（对 nginx 和 apache 一视同仁）。"""
        sel = self.webserver_select()
        active = sel.get('active')
        if not active:
            return {'ok': False, 'note': '未检测到 web 服务器引擎'}
        impl = ws_impl(self, active['id'])
        t = impl.test()
        if not t.get('ok'):
            logger.warning("[sites] %s -t 未通过，拒绝 reload: %s",
                           active['id'], str(t.get('output'))[:300])
            return {'ok': False, 'tested': True,
                    'error': f"{active['name']} 语法校验未通过，未重载",
                    'output': str(t.get('output') or '')[:800]}
        res = impl.reload()
        res['tested'] = True
        if not res.get('ok'):
            logger.warning("[sites] %s reload 失败: %s",
                           active['id'], str(res.get('output'))[:300])
        return res

    def adopt_scan(self) -> dict:
        """收养扫描：解析 nginx -T 全量配置，列出每台机器上**真实存在**的 server 块。

        只读不改任何文件 —— 「收养」（接管配置生成）是显式的第二步。
        managed=True 表示该 conf 在面板托管目录里（本来就是面板建的）。
        """
        ws = self._ws()
        if ws is None:
            return {'ok': False, 'error': 'webserver 机制包未加载'}
        import subprocess as _sp
        exe = shutil.which('nginx', path='/usr/sbin:/usr/local/nginx/sbin')
        if not exe:
            return {'ok': False, 'error': 'nginx 未安装'}
        r = _sp.run([exe, '-T'], capture_output=True, timeout=30)
        text = (r.stdout or b'').decode('utf-8', 'replace')
        if r.returncode != 0:
            return {'ok': False, 'error': 'nginx -T 失败: ' + (r.stderr or b'').decode('utf-8', 'replace')[:300]}
        items = []
        # nginx -T 会在每段配置前打 "# configuration file <path>:"，据此知道 conf 归属
        parts = text.split('# configuration file ')
        for part in parts[1:]:
            if ':' not in part:
                continue
            conf_path, body = part.split(':', 1)
            conf_path = conf_path.strip()
            # 按大括号配对切 server 块（location 有嵌套，不能用正则硬切）
            idx = body.find('server')
            while idx != -1:
                depth, i, start = 0, idx, -1
                while i < len(body):
                    if body[i] == '{':
                        depth += 1
                        if depth == 1:
                            start = i + 1
                    elif body[i] == '}':
                        depth -= 1
                        if depth == 0:
                            block = body[start:i]
                            if 'server_name' in block and 'listen' in block:
                                names = []
                                for ln in block.splitlines():
                                    ln = ln.strip()
                                    if ln.startswith('server_name'):
                                        names += ln.split(None, 1)[1].rstrip(';').split()
                                root = ''
                                proxy = ''
                                for ln in block.splitlines():
                                    ln = ln.strip()
                                    if ln.startswith('root ') and not root:
                                        root = ln.split(None, 1)[1].rstrip(';').strip('"')
                                    if ln.startswith('proxy_pass') and not proxy:
                                        proxy = ln.split(None, 1)[1].rstrip(';').strip()
                                items.append({
                                    'domains': names[:8], 'root': root, 'proxy': proxy,
                                    'conf': conf_path,
                                    'managed': conf_path.startswith(self.conf_dir),
                                    'ssl': 'ssl_certificate' in block,
                                })
                            break
                    i += 1
                idx = body.find('server', i)
        return {'ok': True, 'items': items, 'count': len(items),
                'managed': sum(1 for x in items if x['managed'])}

    def snapshot(self) -> dict:
        sel = self.webserver_select()
        active = sel.get('active')
        return {
            # 当前实际生效的后端：适配器引擎（有）→ builtin（显式开启兜底）→ none（仅生成配置）
            'backend': (active or {}).get('id') or ('builtin' if self.server.running else 'none'),
            'webserver': {
                'active': active,
                'available': sel.get('available') or [],
                'pref': self.ws_pref,
                'reload_cmd': (active or {}).get('id') and ws_reload_cmd(self, active['id']),
            },
            'nginx_reload': self.nginx_reload,
            'builtin': self.server.snapshot(),
            'builtin_enabled': self.builtin_enabled,
            'conf_dir': self.conf_dir,
            'count': self.count(),
        }


def ws_impl(subsystem, adapter_id: str):
    """取当前生效的适配器实现（sites 扩展不 import 门面细节，全走 zkg 句柄）。"""
    ws = subsystem._ws()
    if ws is None:
        raise RuntimeError("webserver 机制包未加载")
    impl = ws.impl_for(adapter_id)
    if impl is None:
        raise RuntimeError(f"适配器 {adapter_id} 不可用")
    return impl


def ws_reload_cmd(subsystem, adapter_id: str) -> str:
    try:
        return ws_impl(subsystem, adapter_id).reload_cmd()
    except Exception:
        return ''


def _project_root() -> str:
    try:
        from core.kernel.paths import project_root
        return project_root()
    except Exception:
        return os.getcwd()
