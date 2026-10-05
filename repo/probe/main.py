"""probe —— 可执行文件发现与版本探测（机制包）。

框架提供的一等公民程序化接口：回答「这台机器上装了 X 吗？什么版本？在哪？」
统一走这里，不再各自写 shutil.which + 正则抠版本 + 缓存过期判断。

稳定 API 表面：
    which(*names, extra_paths=()) -> str | None
    find(candidates, *, extra_paths=(), required=False) -> str | None
    version(exe, *, args=None, timeout=8.0) -> str        取版本号（失败返回 ""）
    parse(text) -> str                                    从任意输出抠 x.y.z
    probe(name, *, exes, args=None, extra_paths=(), ttl=300.0) -> dict
        返回 {'available': bool, 'exe': str, 'version': str, 'cached': bool}
    versions(name, *, exes, args=None, dirs=(), depth=2, extra_paths=(), ttl=300.0) -> list
        多版本清点：同一运行时装了多个版本时**全部列出**（不是只给第一个）
        返回 [{'exe': str, 'version': str, 'source': 'path'|'dir'}, ...]（按版本倒序）
    version_ok(version, spec) -> bool                     比版本（">=8.2,<9"）
    clear_cache() -> None

设计约定：
- 版本探测会真起子进程（有开销），结果经 cache 机制包做 TTL 缓存（默认 5 分钟）；
- java / nginx 的版本走 **stderr**，因此合并两路输出后再解析，不看返回码；
- 解析规则：取输出中第一个形如 ``1.2`` / ``1.2.3`` / ``1.2.3.4`` 的数字串。
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

import zkg                                  # 由 zkg loader 注入：跨机制包取用

_CACHE = None
_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")
_SPEC_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)$")


def _procs():
    """取依赖的机制包。manifest 已声明 dependencies，取不到说明加载顺序出了问题。"""
    mod = zkg.tool("procs")
    if mod is None:
        raise RuntimeError("probe 依赖 procs 机制包，但未被加载（检查 manifest.dependencies）")
    return mod


def _cache():
    """版本缓存（懒建单例）。机制包 cache 提供的是 Cache 类，不是一个模块级字典。"""
    global _CACHE
    mod = zkg.tool("cache")
    if mod is None:
        raise RuntimeError("probe 依赖 cache 机制包，但未被加载（检查 manifest.dependencies）")
    if _CACHE is None:
        _CACHE = mod.Cache(maxsize=256, ttl=300.0)
    return _CACHE


# ── 查找 ────────────────────────────────────────────────
def which(*names, extra_paths=()) -> str | None:
    """按顺序在 PATH 与 extra_paths 中查找任一候选名，返回首个命中。"""
    procs = _procs()
    for name in names:
        if not name:
            continue
        if os.path.isabs(name) or os.sep in name:
            if os.path.isfile(name):
                return os.path.abspath(name)
            continue
        for d in list(extra_paths or ()) + [""]:
            cand = os.path.join(d, name) if d else name
            found = procs.which(cand)
            if found:
                return found
    return None


def find(candidates, *, extra_paths=(), required: bool = False) -> str | None:
    """candidates 为候选名列表；required=True 且找不到时抛 FileNotFoundError。"""
    found = which(*candidates, extra_paths=extra_paths)
    if found is None and required:
        raise FileNotFoundError(f"找不到可执行文件，候选: {list(candidates)}")
    return found


# ── 版本 ────────────────────────────────────────────────
def parse(text: str) -> str:
    """从输出里抠第一个版本号；抠不到返回 ""。"""
    if not text:
        return ""
    m = _VERSION_RE.search(text)
    return m.group(0) if m else ""


def version(exe: str, *, args=None, timeout: float = 8.0) -> str:
    """执行 exe 的版本命令并解析版本号（java/nginx 走 stderr，故两路合并）。

    结果按「可执行文件 + 参数」缓存，**只缓存成功解析到的版本**——
    避免把「还没装」误判为长期无版本（装完即见）。重复探测同一 exe
    不再重复起子进程，冷启动后也大幅降载。
    """
    cache = _cache()
    akey = ",".join(args if args is not None else ("--version",))
    ckey = f"ver:{os.path.normcase(str(exe or ''))}:{akey}"
    hit = cache.get(ckey)
    if hit is not None:
        return hit
    procs = _procs()
    argv = [exe] + list(args if args is not None else ("--version",))
    res = procs.run(argv, timeout=timeout, max_output=64 * 1024)
    ver = parse(f"{res.stdout}\n{res.stderr}")
    if ver:                                  # 只缓存命中，错误结果不雪藏
        cache.set(ckey, ver, ttl=120.0)
    return ver


def probe(name: str, *, exes, args=None, extra_paths=(), ttl: float = 300.0) -> dict:
    """一站式探测：找可执行 + 取版本，结果 TTL 缓存。"""
    cache = _cache()
    key = f"probe:version:{name}"
    hit = cache.get(key)
    if hit is not None:
        out = dict(hit)
        out["cached"] = True
        return out

    exe = find(exes, extra_paths=extra_paths)
    ver = version(exe, args=args) if exe else ""
    out = {"available": bool(exe), "exe": exe or "", "version": ver, "cached": False}
    if exe:                      # 只缓存命中结果：未安装的应尽快复检（装完即可见）
        cache.set(key, dict(out), ttl=ttl)
    return out


_WIN_EXEC_EXT = ('.exe', '.cmd', '.bat', '.com')


def _is_exec_file(fn: str, full: str) -> bool:
    """判断是否真可执行 —— 光比文件名会把 python3.dll / Python.h 一起捞进来
    （真踩过：本机 Python 目录下这三个都叫 python/python3）。"""
    ext = os.path.splitext(fn)[1].lower()
    if os.name == 'nt':
        return ext in _WIN_EXEC_EXT
    return (not ext) and os.access(full, os.X_OK)


def _vkey(version: str):
    """版本号排序键（短的补零）；解析不出数字的排最后。"""
    parts = [int(x) for x in str(version or '').split('.') if x.isdigit()]
    return (0, tuple(parts)) if parts else (1, ())


def versions(name: str, *, exes, args=None, dirs=(), depth: int = 2,
             extra_paths=(), ttl: float = 300.0, limit: int = 40) -> list:
    """清点本机上某运行时的**所有**版本。

    :param exes: 候选可执行文件名（如 ('node', 'nodejs')）
    :param dirs: 额外安装根目录（如 nvm / 宝塔的 /www/server/nodejs）——向下扫描 depth 层
    :param extra_paths: 额外 PATH（沿用 find 的语义）

    为什么单独有这个函数：`probe()` 只回首个命中，装了两个 PHP / 三个 Python 时
    用户看不到另一个 —— 而「管理」的第一步就是知道机器上到底有哪几个版本、分别在哪。
    扫描只在声明的候选目录里做（不遍历整个磁盘），且只认文件名精确匹配。

    结果按版本倒序（新的在前），同名同版本去重；命中过的路径进 TTL 缓存。
    """
    cache = _cache()
    key = f"probe:versions:{name}"
    hit = cache.get(key)
    if hit is not None:
        return [dict(x) for x in hit]

    found = {}                       # normcase(exe) -> {exe, source}

    hit_exe = find(exes, extra_paths=extra_paths)
    if hit_exe:
        found[os.path.normcase(hit_exe)] = {'exe': os.path.abspath(hit_exe),
                                            'source': 'path'}

    want = {str(x).lower() for x in (exes or ()) if x}
    for root in (dirs or ()):
        root = os.path.expandvars(os.path.expanduser(str(root or '')))
        if not root or not os.path.isdir(root):
            continue
        base_depth = root.rstrip('\\/').count(os.sep)
        for cur, subdirs, files in os.walk(root):
            if cur.rstrip('\\/').count(os.sep) - base_depth >= int(depth):
                subdirs[:] = []                     # 到深度就不再往下
            for fn in files:
                stem = os.path.splitext(fn)[0].lower()
                if stem in want:                    # 精确文件名匹配（node.exe → node）
                    full = os.path.join(cur, fn)
                    if not _is_exec_file(fn, full):  # 排除 .dll/.h/.lib 这类同名文件
                        continue
                    found.setdefault(os.path.normcase(full),
                                     {'exe': full, 'source': 'dir'})

    # 同一安装目录里往往有多个入口名（python.exe / python3.exe / python3.cmd）——
    # 按「目录 + 版本」去重，只保留一个（优先真 .exe，其次名字最短的）。
    _prefer = {'.exe': 0, '.com': 1, '.bat': 2, '.cmd': 3, '': 4}

    # 并行取每个候选 exe 的版本：子进程等待是 I/O 密集，线程池显著快于串行
    # （本机上并存的 PHP / JDK / Node 多版本逐个串行能累加到好几秒）。
    exes = [it['exe'] for it in found.values() if it['exe']]
    ver_map: dict = {}
    if exes:
        with ThreadPoolExecutor(max_workers=min(8, len(exes))) as _tp:
            for _e, _v in _tp.map(lambda e: (e, version(e, args=args)), exes):
                ver_map[_e] = _v

    picked = {}
    for item in found.values():
        ver = ver_map.get(item['exe'], '') if item['exe'] else ''
        if not ver and item['source'] != 'path':
            continue            # 抠不出版本的多半不是真入口（PATH 命中的另说）
        d = os.path.dirname(item['exe'])
        rank = (_prefer.get(os.path.splitext(item['exe'])[1].lower(), 5),
                len(os.path.basename(item['exe'])),
                0 if item['source'] == 'path' else 1)
        key = (os.path.normcase(d), ver)
        if key not in picked or rank < picked[key][0]:
            picked[key] = (rank, {'exe': item['exe'], 'version': ver,
                                  'source': item['source']})

    out = [v[1] for v in picked.values()]
    # 同一版本可能有多个入口路径（JDK 的 javapath / javatmp 都是同版本的别名）——
    # 按版本再收一道：留"最干净"的那个当代表，其余记为 aliases，避免面板里同版本刷屏。
    best = {}
    for item in out:
        ver = item['version'] or item['exe']
        cur = best.get(ver)
        rank = (len(item['exe']),
                1 if any(x in item['exe'].lower() for x in ('javatmp', 'javapath_target')) else 0,
                0 if item['source'] == 'path' else 1)
        if cur is None or rank < cur[0]:
            if cur is not None:
                item = dict(item)
                item['aliases'] = [cur[1]['exe']] + cur[1].get('aliases', [])
            best[ver] = (rank, item)
        else:
            cur[1].setdefault('aliases', []).append(item['exe'])
    out = [v[1] for v in best.values()]
    out.sort(key=lambda x: _vkey(x['version']), reverse=True)
    if out:
        cache.set(key, [dict(x) for x in out], ttl=ttl)
    return out


def version_ok(version: str, spec: str) -> bool:
    """版本比较：spec 形如 ">=8.2,<9"；version 为空视为不满足。"""
    if not version:
        return False
    cur = tuple(int(x) for x in version.split(".") if x.isdigit())
    for raw in str(spec).split(","):
        m = _SPEC_RE.match(raw.strip())
        if not m:
            return False
        op, want = m.group(1), tuple(int(x) for x in m.group(2).split("."))
        n = max(len(cur), len(want))
        a = cur + (0,) * (n - len(cur))
        b = want + (0,) * (n - len(want))
        if op == ">=" and not a >= b:
            return False
        if op == "<=" and not a <= b:
            return False
        if op == ">" and not a > b:
            return False
        if op == "<" and not a < b:
            return False
        if op == "==" and not a == b:
            return False
        if op == "!=" and not a != b:
            return False
    return True


def clear_cache() -> None:
    """清掉版本缓存（装完新运行时想立刻看到时调用）。"""
    _cache().clear()
