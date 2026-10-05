# -*- coding: utf-8 -*-
"""
运行时适配器 —— 探测本机运行时 + 按类型生成启动命令

边界：本模块**只管系统环境**（找可执行文件、问版本、拼命令）。
实例子系统只认 `kind` 与最终的 `start_command`，因此新增运行时支持
**不需要动实例模型**，在这里加一条探测/模板即可。

探测本身交给 `probe` 机制包（多候选名搜索、版本解析、TTL 缓存），
本模块不再 import shutil / subprocess —— 跨平台差异在机制包内部消化。
"""
from __future__ import annotations

import logging
import os
import sys
import time

logger = logging.getLogger('zernus')

# 报告级短缓存：前端 30s 轮询，但不必每次都冷启动一整轮子进程。
# env 20s、versions 60s —— 既让轮询瞬时返回，又不至于把"刚装的新运行时"长期藏住。
_REPORT_CACHE = {'env': (0.0, None), 'vers': (0.0, None)}
_ENV_TTL = 20.0
_VERS_TTL = 60.0

# (kind, 可执行文件候选, 版本参数, 显示名)
_PROBES = [
    ('node',   ['node', 'nodejs'],            ['--version'],          'Node.js'),
    ('npm',    ['npm'],                       ['--version'],          'npm'),
    ('java',   ['java'],                      ['-version'],           'Java'),
    ('php',    ['php', 'php8', 'php7'],       ['--version'],          'PHP'),
    ('python', ['python', 'python3', 'py'],   ['--version'],          'Python'),
    ('nginx',  ['nginx'],                     ['-v'],                 'Nginx'),
    ('mysql',  ['mysql', 'mysqld'],           ['--version'],          'MySQL'),
    ('redis',  ['redis-server'],              ['--version'],          'Redis'),
]

# 由扩展入口 register() 注入的机制包取用函数（ctx.zkg_tool）
_TOOL = None
_COMPAT = None


def bind(tool_getter):
    """注入机制包取用函数。必须在使用本模块任何探测函数前调用。"""
    global _TOOL, _COMPAT
    _TOOL = tool_getter
    _COMPAT = tool_getter('compat') if tool_getter else None


def compat():
    """版本兼容层：一个版本一个层（路径约定 / 服务名 / 能力开关 / 注意事项）。"""
    if _COMPAT is None:
        raise RuntimeError(
            "运行时管理依赖 compat 机制包，但未被加载 —— "
            "检查 software/extensions/runtime/manifest.toml 的 dependencies")
    return _COMPAT


def _probe():
    mod = _TOOL('probe') if _TOOL else None
    if mod is None:
        raise RuntimeError(
            "运行时管理依赖 probe 机制包，但未被加载 —— "
            "检查 software/extensions/runtime/manifest.toml 的 dependencies")
    return mod


def _procs():
    mod = _TOOL('procs') if _TOOL else None
    if mod is None:
        raise RuntimeError(
            "运行时管理依赖 procs 机制包，但未被加载 —— "
            "检查 software/extensions/runtime/manifest.toml 的 dependencies")
    return mod


# ── 安装根目录（多版本清点用）────────────────────────────
# 「某种软件通常装在哪」这件事**只由 compat 机制包说了算**（install 根、配置位置、
# 服务名都在那一份声明里），这里不再各自维护一份。机器上装了两个 PHP / 三个 JDK 时，
# 光看 PATH 只能看到当前生效的那个，所以要把已知位置都清点一遍。


def install_dirs(kind: str) -> list:
    """某类运行时的候选安装根（已展开通配与环境变量，只保留真实存在的目录）。"""
    c = compat()
    fam = kind
    roots = list(c.roots(fam))
    # MariaDB 与 MySQL 常混装在同一批目录下，清点 MySQL 时一并扫
    if kind == 'mysql':
        roots += list(c.roots('mariadb'))
    return c.expand(roots)


# 探测定义：kind -> (候选名, 版本参数, 显示名, 附加说明)
_PROBE_BY_KIND = {k: (c, a, l) for k, c, a, l in _PROBES}


def detect_runtimes(deep: bool = True) -> list:
    """探测本机可用运行时。

    :param deep: 是否实际执行 `--version`（关掉则只查可执行文件是否存在，更快）
    """
    p = _probe()
    result = []
    for kind, cands, vargs, label in _PROBES:
        if deep:
            item = p.probe(kind, exes=cands, args=vargs)
            result.append({'kind': kind, 'label': label, 'exe': item['exe'],
                           'available': item['available'], 'version': item['version'],
                           'cached': item.get('cached', False)})
        else:
            exe = p.find(cands) or ''
            result.append({'kind': kind, 'label': label, 'exe': exe,
                           'available': bool(exe), 'version': '', 'cached': False})
    return result


def which_runtime(kind: str) -> str:
    """按 kind 取可执行文件路径（找不到返回空串）。"""
    p = _probe()
    for k, cands, _, _ in _PROBES:
        if k == kind:
            return p.find(cands) or ''
    return ''


def command_template(kind: str, spec: dict) -> str:
    """按实例类型生成默认启动命令。

    生成的是**可编辑的初值**——用户填完表单还能改，所以这里只求「合理」，
    不追求覆盖所有启动方式。
    """
    kind = str(kind or 'generic')
    entry = str(spec.get('entry') or '').strip()      # 入口文件 / jar
    args = str(spec.get('args') or '').strip()
    port = spec.get('port') or 0
    # exe：用户在多版本清单里选定的解释器路径 —— 指明后命令就用它，
    # 而不是 PATH 上当前生效的那个（机器上并存两个 PHP / 三个 JDK 时靠这个切）
    exe = str(spec.get('exe') or '').strip()

    if kind == 'node':
        # 有 package.json 且用户没指定入口 → 走 npm start
        cwd = str(spec.get('cwd') or '')
        if not entry and cwd and os.path.isfile(os.path.join(cwd, 'package.json')):
            return f"npm start {args}".strip()
        return f"{exe or 'node'} {entry or 'index.js'} {args}".strip()

    if kind == 'java':
        jvm = str(spec.get('jvm_args') or '-Xms128m -Xmx512m').strip()
        return f"{exe or 'java'} {jvm} -jar {entry or 'app.jar'} {args}".strip()

    if kind == 'php':
        # 内置服务器便于单机验证；生产一般走 php-fpm + 反代
        return f"{exe or 'php'} -S 127.0.0.1:{port or 8080} -t {entry or '.'}"

    if kind == 'python':
        return f"{exe or sys.executable} {entry or 'main.py'} {args}".strip()

    return str(spec.get('start_command') or '').strip()


# ══════════════════════════════════════════════════════════
# 多版本清点 + 环境体检
# ══════════════════════════════════════════════════════════
def detect_versions(kinds=None) -> dict:
    """清点每种运行时的**所有**版本（不是只回当前生效的那个）。

    返回 ``{kind: {'label','installed','versions','current','current_exe','current_layer'}}``；
    没装的 kind 返回空列表（面板显示「未安装」而不是报错）。

    每个版本项都会带上它所属的**兼容层**（``layer`` / ``layer_title`` /
    ``traits``）—— 面板因此能直接说「这台是 PHP 8.4，注意 X」，
    而不必让运维去背版本差异（见 compat 机制包）。
    """
    # 报告级短缓存：60s 内同参数直接返回，省去重复扫目录 + 起子进程
    _ts, _data = _REPORT_CACHE['vers']
    if _data is not None and (time.time() - _ts) < _VERS_TTL:
        if kinds:
            return {k: v for k, v in _data.items() if k in kinds}
        return _data

    p, c = _probe(), compat()

    # 每种运行时的清点（扫安装目录 + 取版本 + 归层）互相独立，用线程池并行跑；
    # 否则 php / mysql 这类要 os.walk 大目录、npm 要起 npm 进程的 kind 会串行累加，
    # 整份报告从「逐个几百毫秒」堆到三秒多。并行后总耗时≈最慢那一个 kind。
    def _work(kind, cands, vargs, label):
        try:
            items = p.versions(kind, exes=cands, args=vargs,
                               dirs=install_dirs(kind), depth=3)
        except Exception as e:                     # 单个 kind 探测失败不该拖垮整份报告
            logger.warning("[runtime] 多版本清点失败 %s: %s", kind, e)
            items = []
        vers = []
        for it in items:
            row = dict(it)
            # 归层只对已登记的运行时家族做（npm 这类工具没有版本层，硬归会得到假层）
            if it.get('version') and c.knows(kind):
                try:
                    lay = c.resolve(kind, it['version'])
                    row['layer'] = lay['id']
                    row['layer_title'] = lay.get('title', '')
                    row['traits'] = lay.get('traits', [])
                except Exception:                  # 归层失败不影响版本本身可用
                    row['layer'], row['layer_title'], row['traits'] = '', '', []
            else:
                row['layer'], row['layer_title'], row['traits'] = '', '', []
            vers.append(row)
        # 该 kind「当前 PATH 生效的那个」落在哪一层（概览用）
        cur = p.probe(kind, exes=cands, args=vargs)
        cur_layer = ''
        if cur.get('version') and c.knows(kind):
            try:
                cur_layer = c.resolve(kind, cur['version'])['id']
            except Exception:
                cur_layer = ''
        return kind, {'label': label, 'installed': len(vers), 'versions': vers,
                     'current': cur.get('version') or '',
                     'current_exe': cur.get('exe') or '',
                     'current_layer': cur_layer}

    _planned = [(k, c2, a, l) for (k, c2, a, l) in _PROBES
               if not (kinds and k not in kinds)]
    _collected = {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(_planned))) as _tp:
        for _f in _tp.map(lambda a: _work(*a), _planned):
            _collected[_f[0]] = _f[1]

    # 维持 _PROBES 的稳定顺序，避免卡片乱序
    out = {k: _collected[k] for k, *_ in _PROBES if k in _collected}

    _REPORT_CACHE['vers'] = (time.time(), out)
    return out


# 体检附加项说明：每项 = (标签, 说明)
_ENV_HINTS = {
    'node': '包管理 npm、全局安装前缀',
    'python': 'pip 与 venv（虚拟环境）可用性',
    'java': 'JAVA_HOME 与 javac（只有 JRE 时编译不了）',
    'php': 'php.ini 位置、已启用扩展数、php-fpm 是否就位',
    'mysql': '客户端/服务端版本与系统服务状态',
}


def _run(argv, timeout=8.0, shell=False):
    """跑一条命令拿输出（一律经 procs 机制包，不自己开子进程）。"""
    try:
        r = _procs().run(argv, timeout=timeout, shell=shell, max_output=64 * 1024)
        return f"{r.stdout}\n{r.stderr}".strip()
    except Exception as e:
        return f"__err__{type(e).__name__}: {e}"


def env_report() -> dict:
    """环境体检：每种运行时给「装没装 / 哪些版本 / 包管理器与关键路径是否就位」。

    只做**只读**探测（跑 --version / --ini 这类），不改任何配置。
    结果按 20s 短缓存，避免前端 30s 轮询每次都冷启动一整轮子进程。
    """
    _ts, _data = _REPORT_CACHE['env']
    if _data is not None and (time.time() - _ts) < _ENV_TTL:
        return _data

    p = _probe()
    vers = detect_versions()
    items = []

    # 先按已有信息搭出每张卡（便宜）：各运行时的当前 exe / 版本直接复用
    # detect_versions 的结果，不再对每个 kind 重新 probe 一遍 PATH 版本
    # （旧实现这里又串行 probe 了一次，是当前页最大的冗余来源）。
    rows = {}
    for kind, info in vers.items():
        label = _PROBE_BY_KIND[kind][2]
        exe = info.get('current_exe') or ''
        row = {'kind': kind, 'label': label, 'available': bool(exe),
               'exe': exe, 'version': info.get('current') or '',
               'installed': info['installed'], 'versions': info['versions'],
               'layer': info.get('current_layer', ''),
               'hint': _ENV_HINTS.get(kind, ''), 'checks': []}
        # 这个版本要注意什么 —— 直接由兼容层给出，不用运维去背版本差异
        if row['version'] and _COMPAT.knows(kind):
            try:
                row['compat'] = _COMPAT.explain(kind, row['version'])
            except Exception:
                row['compat'] = None
        rows[kind] = row

    # 每种运行时的附加体检（npm / pip / php -m / javac / 系统服务等）互相独立，
    # 用线程池并行跑，避免串行等子进程把整页拖慢。
    def _kind_checks(kind, info):
        exe = info.get('current_exe') or ''
        out = []
        if kind == 'node' and exe:
            npm = p.find(['npm'])
            out.append(('npm', bool(npm),
                        p.version(npm, args=['--version']) if npm else '未找到 npm'))
            if npm:
                pre = _run([npm, 'prefix', '-g'], shell=True)
                out.append(('全局安装前缀', not pre.startswith('__err__'),
                            pre.splitlines()[-1] if pre else ''))
        elif kind == 'python' and exe:
            v = _run([exe, '-m', 'pip', '--version'])
            out.append(('pip', not v.startswith('__err__') and 'pip' in v.lower(),
                        v.splitlines()[0][:80] if v else ''))
            v = _run([exe, '-c', 'import venv,sys;print(venv.__name__, sys.version.split()[0])'])
            out.append(('venv 模块', not v.startswith('__err__') and 'venv' in v, v[:80]))
        elif kind == 'java' and exe:
            out.append(('JAVA_HOME', bool(os.environ.get('JAVA_HOME')),
                        os.environ.get('JAVA_HOME') or '未设置（多 JDK 共存时建议设置）'))
            javac = p.find(['javac'])
            out.append(('javac', bool(javac),
                        p.version(javac) if javac else '只有 JRE，无法编译'))
        elif kind == 'php' and exe:
            ini = _run([exe, '--ini'])
            ini_path = ''
            for line in ini.splitlines():
                if 'Loaded Configuration File' in line:
                    ini_path = line.split(':', 1)[-1].strip()
            out.append(('php.ini', bool(ini_path and ini_path.lower() != '(none)'),
                        ini_path or '未找到 php.ini'))
            mods = _run([exe, '-m'])
            modlist = [m for m in mods.splitlines() if m and not m.startswith('__err__')
                       and not m.startswith('[')]
            out.append(('已启用扩展', len(modlist) > 0, f'{len(modlist)} 个'))
            fpm = p.find(['php-fpm', 'php-fpm8', 'php-cgi'])
            out.append(('php-fpm / php-cgi', bool(fpm),
                        fpm or '未找到 —— 站点要用 php 需先装 php-fpm'))
        elif kind == 'mysql':
            cli = p.find(['mysql'])
            out.append(('客户端 mysql', bool(cli), cli or '未找到命令行客户端'))
            srv = p.find(['mysqld', 'mariadbd'])
            out.append(('服务端 mysqld', bool(srv), srv or '未安装服务端'))
            try:
                svc = _TOOL('svcmgr') if _TOOL else None
                if svc:
                    r = svc.resolve(['MySQL', 'MySQL80', 'MariaDB', 'mysql'])
                    out.append(('系统服务', bool((r or {}).get('name')),
                                (r or {}).get('name') or '未注册为系统服务'))
            except Exception as e:
                out.append(('系统服务', False, f'{type(e).__name__}'))
        return out

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as _tp:
        _futs = {_tp.submit(_kind_checks, kind, info): kind for kind, info in vers.items()}
        for _f in _futs:
            kind = _futs[_f]
            try:
                rows[kind]['checks'] = [
                    {'name': n, 'ok': bool(o), 'detail': str(d)[:200]}
                    for (n, o, d) in _f.result()
                ]
            except Exception:
                rows[kind]['checks'] = []

    items = [rows[k] for k, _ in vers.items()]
    result = {'count': len(items), 'items': items,
              'missing': [i['kind'] for i in items if not i['available']]}
    _REPORT_CACHE['env'] = (time.time(), result)
    return result
