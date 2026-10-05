"""fileops —— 文件系统原语（机制包，单文件）。

上层要做的文件操作，实现**只此一份**：列目录 / 摘要 / 读 / 写 / 建目录 /
改名 / 删 / 复制移动 / 打包 zip / 磁盘用量。

**边界归属**：本包只认「已经是最终绝对路径」的入参，自己**不做**任何路径
合法性判断 —— 那是 sandbox 机制包的事。调用方先 resolve 校验，再交给这里
执行。安全一层、操作一层，谁都不要重复实现对方。

**为什么做成机制包**：内核不该知道「文件怎么读怎么写」；扩展与插件也不该
各写一份。manifest.toml 里声明 ``dependencies = ["fileops"]`` 即可取用::

    import zkg
    fo = zkg.require("fileops")

稳定 API 表面（入参 str/PathLike，出参纯 dict/list，可直接 JSON 化）::

    default_roots() -> list[str]
    is_text_name(name) -> bool
    list_dir(path, show_hidden=True) -> {path, items, count}
    stat_path(path) -> {path, name, dir, size, mtime, mtime_raw, mode}
    read_file(path, max_bytes=MAX_READ) -> {binary, content, encoding, size}
    write_file(path, content, encoding='utf-8', create=True) -> {path, size}
    read_chunk(path, offset=0, length=CHUNK_SIZE) -> {path,size,offset,data_b64,eof}
    write_chunk(path, offset=0, data_b64='') -> {path, size, offset}
    mkdir(path) -> {path}
    rename(path, new_name) -> {from, to}
    delete(paths, protect=()) -> {deleted, failed}
    copy_move(src, dst_dir, move=False) -> {from, to}
    copy_many(paths, dst_dir, move=False) -> {moved, failed, count}
    zip_paths(paths, out_path='') -> {archive, size}
    hash_file(path, algo='sha256', max_bytes=MAX_READ) -> {path, size, algo, hash}
    disk_usage(path) -> {path, total, used, free, percent} | {path, error}

约定：出错的**单点**操作（删除/复制里的一条）不进 failed 就抛异常；
批量操作逐条隔离，单条失败不影响其余。
"""
from __future__ import annotations

import base64
import os
import shutil
import time
import zipfile

MAX_READ = 4 * 1024 * 1024          # 单文件读取上限 4MB（编辑器场景足够）
MAX_TAIL = 4 * 1024 * 1024          # 读文件末尾的上限 4MB（日志场景）

# 认得出来的文本后缀 —— 「可编辑」据此判定（前端据此决定给不给编辑器）
TEXT_EXT = {
    '.txt', '.log', '.conf', '.cfg', '.ini', '.yaml', '.yml', '.json', '.xml',
    '.md', '.html', '.htm', '.css', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx',
    '.py', '.php', '.java', '.go', '.rs', '.sh', '.bat', '.cmd', '.ps1', '.sql',
    '.env', '.htaccess', '.toml', '.properties', '.vue', '.svelte', '.c', '.h',
    '.cpp', '.hpp', '.rb', '.pl', '.lua', '.dockerfile', '.gitignore',
}


def _hashcode():
    """摘要算法来自 hashcode 机制包 —— 本包不自带实现。"""
    import zkg
    mod = zkg.tool('hashcode')
    if mod is None:
        raise RuntimeError("fileops 依赖 hashcode 机制包，但未被加载"
                           "（检查 manifest.toml 的 dependencies）")
    return mod


def _procs():
    """字节解码来自 procs 机制包 —— 「同一台机器上编码混杂」的判别只此一份。"""
    import zkg
    mod = zkg.tool('procs')
    if mod is None:
        raise RuntimeError("fileops 依赖 procs 机制包，但未被加载"
                           "（检查 manifest.toml 的 dependencies）")
    return mod


def decode(raw: bytes, encoding: str = '') -> str:
    """把文件字节解码成文本（统一走 procs.decode_bytes 的判别规则）。"""
    return _procs().decode_bytes(raw, encoding or None)


def _fspath(p):
    return os.fspath(p)


def _as_list(paths) -> list:
    """把「单个路径」与「路径列表」都归一成 list。

    不这么做的话，调用方传一个字符串会被当成可迭代对象**逐字符**处理 ——
    ``delete("D:\\\\tmp")`` 会去删 ``D``、``:``、``\\``、``t``… 这不是错误，
    而是设计漏了口子。单点收口在这儿。
    """
    if paths is None:
        return []
    if isinstance(paths, (str, bytes, os.PathLike)):
        return [paths]
    return list(paths)


def _mtime(ts: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


# ── 平台 ────────────────────────────────────────────────────────
def default_roots() -> list:
    """默认根：Windows 列所有盘符，其它平台为 /。

    平台差异只在这一处判断 —— 内核不需要知道自己是跑在什么系统上。
    """
    if os.name == 'nt':
        roots = [f'{c}:\\' for c in 'CDEFGHIJKLMNOPQRSTUVWXYZ'
                 if os.path.exists(f'{c}:\\')]
        return roots or ['C:\\']
    return ['/']


def is_text_name(name: str) -> bool:
    return os.path.splitext(str(name or ''))[1].lower() in TEXT_EXT


# ── 查询 ────────────────────────────────────────────────────────
def list_dir(path, show_hidden: bool = True) -> dict:
    p = _fspath(path)
    if not os.path.isdir(p):
        raise ValueError(f'不是目录: {path}')
    try:
        names = os.listdir(p)
    except PermissionError as e:
        raise ValueError(f'没有权限读取: {e}')
    items = []
    for name in names:
        if not show_hidden and name.startswith('.'):
            continue
        full = os.path.join(p, name)
        try:
            st = os.stat(full)
            is_dir = os.path.isdir(full)
            items.append({
                'name': name,
                'path': full,
                'dir': is_dir,
                'size': 0 if is_dir else st.st_size,
                'mtime': _mtime(st.st_mtime),
                'mtime_raw': int(st.st_mtime),
                'editable': (not is_dir and is_text_name(name)
                             and st.st_size <= MAX_READ),
                'link': os.path.islink(full),
            })
        except Exception:
            items.append({'name': name, 'path': full, 'dir': False, 'size': 0,
                          'mtime': '', 'mtime_raw': 0, 'editable': False,
                          'link': False, 'error': '无法读取'})
    items.sort(key=lambda x: (not x['dir'], x['name'].lower()))
    return {'path': p, 'items': items, 'count': len(items)}


def stat_path(path) -> dict:
    p = _fspath(path)
    if not os.path.exists(p):
        raise ValueError('路径不存在')
    st = os.stat(p)
    is_dir = os.path.isdir(p)
    return {
        'path': p, 'name': os.path.basename(p) or p, 'dir': is_dir,
        'size': 0 if is_dir else st.st_size,
        'mtime': _mtime(st.st_mtime), 'mtime_raw': int(st.st_mtime),
        'mode': oct(st.st_mode & 0o777),
    }


def disk_usage(path='') -> dict:
    p = _fspath(path) or os.getcwd()
    try:
        u = shutil.disk_usage(p)
        return {'path': p, 'total': u.total, 'used': u.used, 'free': u.free,
                'percent': round(u.used / u.total * 100, 1)}
    except Exception as e:
        return {'path': p, 'error': str(e)}


# ── 读写 ────────────────────────────────────────────────────────
def read_bytes(path, max_bytes: int = MAX_READ) -> dict:
    """读原始字节（需要二进制内容的场景，如证书解析）。返回 ``{path,size,data}``。"""
    p = _fspath(path)
    if not os.path.isfile(p):
        raise ValueError('不是文件')
    size = os.path.getsize(p)
    if size > max_bytes:
        raise ValueError(f'文件过大（{size} 字节 > 上限 {max_bytes}）')
    with open(p, 'rb') as f:
        return {'path': p, 'size': size, 'data': f.read()}


def read_tail(path, max_bytes: int = MAX_TAIL) -> dict:
    """读文件**末尾**至多 max_bytes 并解码为文本 —— 日志场景的通用原语。

    大文件不能整个读进来；这里 seek 到末尾往前读，并如实报告是否截断、
    实际读了多少字节。解码回落规则统一来自 procs.decode_bytes。
    """
    p = _fspath(path)
    if not os.path.isfile(p):
        raise ValueError(f'文件不存在: {path}')
    size = os.path.getsize(p)
    with open(p, 'rb') as f:
        if size > max_bytes:
            f.seek(-max_bytes, 2)
        raw = f.read()
    return {'path': p, 'size': size, 'text': decode(raw),
            'truncated': size > max_bytes, 'bytes_read': len(raw)}


def read_file(path, max_bytes: int = MAX_READ) -> dict:
    """读文件：先试 UTF-8，再回落 GBK；都不行则按二进制返回前 2KB 的 base64。

    只认一种编码必然有一边乱码 —— 这条规矩在这里统一执行，
    上层不用各写一遍。
    """
    p = _fspath(path)
    if not os.path.isfile(p):
        raise ValueError('不是文件')
    size = os.path.getsize(p)
    if size > max_bytes:
        raise ValueError(f'文件过大（{size} 字节 > 上限 {max_bytes}），请下载后查看')
    with open(p, 'rb') as f:
        raw = f.read()
    try:
        return {'path': p, 'binary': False, 'size': size,
                'content': raw.decode('utf-8'), 'encoding': 'utf-8',
                'editable': True}
    except UnicodeDecodeError:
        pass
    try:
        return {'path': p, 'binary': False, 'size': size,
                'content': raw.decode('gbk'), 'encoding': 'gbk',
                'editable': True}
    except UnicodeDecodeError:
        return {'path': p, 'binary': True, 'size': size,
                'content': base64.b64encode(raw[:2048]).decode(),
                'encoding': 'binary'}


def write_file(path, content: str, encoding: str = 'utf-8',
               create: bool = True) -> dict:
    p = _fspath(path)
    if not create and not os.path.exists(p):
        raise ValueError('文件不存在')
    if create:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
    data = str(content).encode(encoding if encoding in ('utf-8', 'gbk') else 'utf-8')
    with open(p, 'wb') as f:
        f.write(data)
    return {'path': p, 'size': len(data)}


# ── 二进制分块传输 ──────────────────────────────────────────────
# 命令通道单帧载荷上限 1MB（core/nodes/hub.py _MAX_PAYLOAD）；
# 512KB 原始字节 base64 后约 683KB，加 JSON 信封仍在限内 —— 块长在服务端收口，
# 调用方传多大都不会撑破帧。更大的流（GB 级）仍应走流面 0x30 文件帧。
CHUNK_SIZE = 512 * 1024


def read_chunk(path, offset: int = 0, length: int = CHUNK_SIZE) -> dict:
    """从 ``offset`` 起读至多 ``length`` 字节，base64 返回 —— 二进制下载的原语。

    返回 ``{path, size, offset, data_b64, eof}``；``eof`` 表示读到文件尾，
    调用方按 offset += len(解码字节) 顺序取块即可。
    """
    p = _fspath(path)
    if not os.path.isfile(p):
        raise ValueError(f'不是文件: {path}')
    size = os.path.getsize(p)
    offset = max(0, int(offset or 0))
    length = max(1, min(int(length or CHUNK_SIZE), CHUNK_SIZE))
    if offset >= size:
        return {'path': p, 'size': size, 'offset': offset,
                'data_b64': '', 'eof': True}
    with open(p, 'rb') as f:
        f.seek(offset)
        raw = f.read(length)
    return {'path': p, 'size': size, 'offset': offset,
            'data_b64': base64.b64encode(raw).decode('ascii'),
            'eof': offset + len(raw) >= size}


def write_chunk(path, offset: int = 0, data_b64: str = '') -> dict:
    """从 ``offset`` 起写入一块 base64 字节 —— 二进制上传的原语。

    只支持**顺序写**：offset 必须等于当前文件大小（第一块 offset=0 时清空重建），
    不连续即拒 —— 断线续传有明确语义，也绝不会写花文件。
    返回 ``{path, size, offset}``（新大小与下一个应写的位置）。
    """
    p = _fspath(path)
    try:
        raw = base64.b64decode(data_b64 or '', validate=True)
    except Exception as e:
        raise ValueError(f'data_b64 不是合法 base64: {e}')
    offset = max(0, int(offset or 0))
    exists = os.path.exists(p)
    if offset == 0:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, 'wb') as f:
            f.write(raw)
    else:
        if not exists:
            raise ValueError('文件不存在，不能从中间开始写')
        cur = os.path.getsize(p)
        if offset != cur:
            raise ValueError(f'写入位置不连续（offset={offset}，当前大小={cur}）')
        with open(p, 'ab') as f:
            f.write(raw)
    return {'path': p, 'size': os.path.getsize(p), 'offset': offset + len(raw)}


# ── 变更 ────────────────────────────────────────────────────────
def mkdir(path) -> dict:
    p = _fspath(path)
    if os.path.exists(p):
        raise ValueError('已存在同名文件或目录')
    os.makedirs(p, exist_ok=False)
    return {'path': p}


def rename(path, new_name: str) -> dict:
    p = _fspath(path)
    if not os.path.exists(p):
        raise ValueError('路径不存在')
    new_name = os.path.basename(str(new_name or '').strip())
    if not new_name or new_name in ('.', '..'):
        raise ValueError('新名称非法')
    dst = os.path.join(os.path.dirname(p), new_name)
    if os.path.exists(dst):
        raise ValueError('目标已存在')
    os.rename(p, dst)
    return {'from': p, 'to': dst}


def delete(paths, protect=()) -> dict:
    """删除多个路径。``protect`` 里的路径（通常是盘根）拒绝删除。

    逐条隔离：一条失败不影响其余，结果里分开列。
    """
    guard = {os.path.normcase(_fspath(x)) for x in _as_list(protect)}
    deleted, failed = [], []
    for raw in _as_list(paths):
        p = _fspath(raw)
        try:
            if os.path.normcase(p) in guard:
                failed.append({'path': p, 'error': '不允许删除根目录'})
                continue
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
            else:
                failed.append({'path': p, 'error': '不存在'})
                continue
            deleted.append(p)
        except Exception as e:
            failed.append({'path': str(raw), 'error': str(e)})
    return {'deleted': deleted, 'failed': failed}


def copy_move(src, dst_dir, move: bool = False) -> dict:
    """复制/移动单个路径到目标目录。同名不覆盖 —— 加时间戳后缀。"""
    s, d = _fspath(src), _fspath(dst_dir)
    if not os.path.exists(s):
        raise ValueError('源不存在')
    if not os.path.isdir(d):
        raise ValueError('目标必须是目录')
    target = os.path.join(d, os.path.basename(s))
    if os.path.exists(target):
        target = f'{target}.{int(time.time())}'
    if move:
        shutil.move(s, target)
    elif os.path.isdir(s):
        shutil.copytree(s, target)
    else:
        shutil.copy2(s, target)
    return {'from': s, 'to': target}


def copy_many(paths, dst_dir, move: bool = False) -> dict:
    items = _as_list(paths)
    if not items:
        raise ValueError('未选择任何路径')
    moved, failed = [], []
    for raw in items:
        try:
            moved.append(copy_move(raw, dst_dir, move))
        except Exception as e:
            failed.append({'path': str(raw), 'error': str(e)})
    return {'moved': moved, 'failed': failed, 'count': len(moved)}


# ── 打包 / 摘要 ─────────────────────────────────────────────────
def zip_paths(paths, out_path: str = '') -> dict:
    """打包为 zip。``out_path`` 为空时落在第一个路径的父目录下。

    返回 ``{archive, size, files}``；``files`` 是归档内的条目数，
    调用方（备份等）直接取用，不必自己再数一遍。
    """
    resolved = [_fspath(p) for p in _as_list(paths)]
    if not resolved:
        raise ValueError('未选择任何路径')
    if not out_path:
        out_path = os.path.join(os.path.dirname(resolved[0]),
                                f'archive_{int(time.time())}.zip')
    target = _fspath(out_path)
    parent_dir = os.path.dirname(target)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    n = 0
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in resolved:
            if os.path.isdir(p):
                parent = os.path.dirname(p)
                for root, _dirs, files in os.walk(p):
                    for fn in files:
                        full = os.path.join(root, fn)
                        z.write(full, os.path.relpath(full, parent))
                        n += 1
            elif os.path.exists(p):
                z.write(p, os.path.basename(p))
                n += 1
    return {'archive': target, 'size': os.path.getsize(target), 'files': n}


def hash_file(path, algo: str = 'sha256', max_bytes: int = MAX_READ) -> dict:
    """算文件摘要。算法来自 hashcode 机制包（本包不自带实现）。

    限 ``max_bytes``；更大文件请直接用命令行工具（sha256sum / certutil）。
    """
    p = _fspath(path)
    if not os.path.isfile(p):
        raise ValueError(f'不是文件: {path}')
    size = os.path.getsize(p)
    if size > max_bytes:
        raise ValueError(f'文件过大（{size} 字节 > {max_bytes}），请用命令行工具计算')
    fn = getattr(_hashcode(), str(algo), None)
    if fn is None:
        raise ValueError(f'不支持的算法: {algo}（可用 sha256 / sha1 / md5）')
    with open(p, 'rb') as f:
        return {'path': p, 'size': size, 'algo': algo, 'hash': fn(f.read())}
