# -*- coding: utf-8 -*-
"""文件管理（官方扩展）—— 薄壳。

本文件**不含任何文件操作实现**，只做三件事：

1. **路径边界**：把请求路径交给 ``sandbox`` 机制包解析成真实绝对路径，
   越界即拒（realpath + 根白名单，软链指向外部同样被拦）。
2. **注册命令**：``files.*`` 一组内核命令 —— 本机与远端节点走同一条通道。
   二进制上传/下载走 ``files.write_b64`` / ``files.read_b64``（512KB 分块，
   顺序写/顺序读，远端节点同样可用；GB 级大流仍应走流面 0x30 文件帧）。
3. **两条流式接口**：上传 / 下载（REST）。保留给 hub 本机的 multipart 场景，
   前端主路径已改走命令通道分块。

真正的文件操作在 ``repo/fileops/main.py``（机制包，单文件，一份实现）。
分层是：本扩展 → 沙箱校验 → fileops 执行。谁都不重复实现对方。

依赖声明见 ``manifest.toml``：``dependencies = ["sandbox", "fileops"]``
（fileops 自己再依赖 hashcode 拿摘要算法）。
"""
import os

import logging

logger = logging.getLogger('zernus')

__plugin_meta__ = {
    "name": "文件管理",
    "version": "0.3.0",
    "author": "ZPanel",
    "desc": "浏览 / 编辑 / 上传 / 下载 / 删除；路径边界归 sandbox，文件操作归 fileops",
    "priority": 40,
    "official": True,
}

guard = None      # 沙箱视图（register 时装配）
_fo = None        # fileops 机制包


def resolve(path: str) -> str:
    """把请求路径解析为真实绝对路径并校验落在允许的根目录内。

    判定全部交给 sandbox（realpath + 白名单）；越界统一转 ValueError，
    调用方按 403 处理。
    """
    if not path:
        raise ValueError('路径不能为空')
    try:
        return guard.resolve(os.path.expanduser(str(path)))
    except guard.OutOfRoot as e:
        raise ValueError(f'路径越界（不在允许的根目录内）: {path}') from e


def is_allowed(path: str) -> bool:
    try:
        resolve(path)
        return True
    except ValueError:
        return False


# ── 命令实现：解析边界 → 交给 fileops ────────────────────────────
def _roots_list() -> list:
    return [{'path': r, 'name': os.path.basename(r) or r} for r in guard.roots]


def _list(path: str, hidden: bool = True) -> dict:
    r = _fo.list_dir(resolve(path), hidden)
    parent = os.path.dirname(r['path'])
    r['parent'] = parent if parent != r['path'] and is_allowed(parent) else None
    return r


def _stat(path: str) -> dict:
    return _fo.stat_path(resolve(path))


def _read(path: str) -> dict:
    return _fo.read_file(resolve(path))


def _write(path: str, content: str, encoding: str = 'utf-8') -> dict:
    return _fo.write_file(resolve(path), content, encoding)


def _mkdir(path: str) -> dict:
    return _fo.mkdir(resolve(path))


def _rename(path: str, new_name: str) -> dict:
    return _fo.rename(resolve(path), new_name)


def _resolve_each(paths):
    """逐条过边界：一条越界不该拖垮整批（返回 已解析 / 被拒 两组）。

    单路径字符串与列表都接受 —— 传字符串时**不能**按可迭代逐字符处理。
    """
    if isinstance(paths, str):
        paths = [paths]
    good, bad = [], []
    for raw in (paths or []):
        try:
            good.append(resolve(raw))
        except ValueError as e:
            bad.append({'path': str(raw), 'error': str(e)})
    return good, bad


def _delete(paths: list) -> dict:
    good, bad = _resolve_each(paths)
    r = _fo.delete(good, protect=guard.roots) if good else {'deleted': [], 'failed': []}
    r['failed'] = r['failed'] + bad
    return r


def _copy(paths: list, dst_dir: str, move: bool = False) -> dict:
    good, bad = _resolve_each(paths)
    r = _fo.copy_many(good, resolve(dst_dir), move) if good else \
        {'moved': [], 'failed': [], 'count': 0}
    r['failed'] = r['failed'] + bad
    return r


def _zip(paths: list, out_path: str = '') -> dict:
    return _fo.zip_paths([resolve(p) for p in (paths or [])],
                         resolve(out_path) if out_path else '')


def _hash(path: str, algo: str = 'sha256') -> dict:
    return _fo.hash_file(resolve(path), algo)


def _disk(path: str = '') -> dict:
    p = resolve(path) if path else (guard.roots[0] if guard.roots else os.getcwd())
    return _fo.disk_usage(p)


def register(ctx):
    """装配沙箱 + 取用 fileops，然后注册命令与两条流式接口。"""
    global guard, _fo
    fw = ctx._framework
    cfg = fw.config.get('files') or {}
    if cfg.get('enabled', True) is False:
        ctx.log("文件管理已禁用 (files.enabled: false)")
        return

    sandbox = ctx.zkg_tool('sandbox')
    _fo = ctx.zkg_tool('fileops')
    if sandbox is None or _fo is None:
        missing = [n for n, m in (('sandbox', sandbox), ('fileops', _fo)) if m is None]
        raise RuntimeError(
            f"文件管理缺少机制包 {missing} —— 检查 software/extensions/files/"
            f"manifest.toml 的 dependencies，以及 repo/ 是否被 zkg 扫描到")

    roots = cfg.get('roots') or _fo.default_roots()
    guard = sandbox.Sandbox(roots)

    # ── 命令表（本机 / 远端同一条通道）──────────────────────────
    def _ok(d):
        return {'ok': True, 'data': d}

    def _guard(fn, args):
        try:
            return _ok(fn(args or {}))
        except Exception as e:
            return {'ok': False, 'data': f'{type(e).__name__}: {e}'}

    fw.nodes.register_provider(
        'files',
        lambda: {'roots': _roots_list(),
                 'disk': _disk(guard.roots[0] if guard.roots else '')},
        desc='文件系统概览', level='software')

    for cmd, fn, desc in [
        ('files.roots',  lambda a: _ok({'roots': _roots_list()}),        '根目录清单'),
        ('files.list',   lambda a: _guard(lambda x: _list(x.get('path', ''),
                                                          x.get('hidden', True) is not False), a), '列目录'),
        ('files.stat',   lambda a: _guard(lambda x: _stat(x.get('path', '')), a),      '路径摘要'),
        ('files.read',   lambda a: _guard(lambda x: _read(x.get('path', '')), a),      '读文件'),
        ('files.write',  lambda a: _guard(lambda x: _write(x.get('path', ''),
                                                           x.get('content', ''),
                                                           x.get('encoding', 'utf-8')), a), '写文件'),
        ('files.read_b64',  lambda a: _guard(lambda x: _fo.read_chunk(
            resolve(x.get('path', '')), x.get('offset') or 0, x.get('length') or 0), a),
         '分块读文件（base64，二进制下载）'),
        ('files.write_b64', lambda a: _guard(lambda x: _fo.write_chunk(
            resolve(x.get('path', '')), x.get('offset') or 0, x.get('data_b64') or ''), a),
         '分块写文件（base64，二进制上传）'),
        ('files.mkdir',  lambda a: _guard(lambda x: _mkdir(x.get('path', '')), a),     '新建目录'),
        ('files.rename', lambda a: _guard(lambda x: _rename(x.get('path', ''),
                                                            x.get('new_name', '')), a), '重命名'),
        ('files.delete', lambda a: _guard(lambda x: _delete(x.get('paths') or []), a), '删除'),
        ('files.copy',   lambda a: _guard(lambda x: _copy(x.get('paths') or [],
                                                          x.get('dst_dir', ''),
                                                          bool(x.get('move'))), a),
         '批量复制/移动（move=true 为剪切）'),
        ('files.zip',    lambda a: _guard(lambda x: _zip(x.get('paths') or [],
                                                         x.get('out_path', '')), a), '打包 zip'),
        ('files.hash',   lambda a: _guard(lambda x: _hash(x.get('path', ''),
                                                          x.get('algo', 'sha256')), a), '文件摘要'),
        ('files.disk',   lambda a: _guard(lambda x: _disk(x.get('path', '')), a),      '磁盘用量'),
    ]:
        fw.nodes.register_handler(cmd, fn, desc=desc, level='software')

    # 兼容旧引用点（没有任何内部代码使用，仅保留注册事实）
    fw.services.register('files', guard)

    # ── 流式接口（唯二走 HTTP 的理由：二进制上传 / 下载）──────────
    from flask import jsonify, request as _req, send_file

    def _api_upload():
        """上传：multipart 或 {path, content_base64} 两种方式。"""
        dst_dir = _req.args.get('dir', '')
        files = _req.files.getlist('file')
        saved = []
        try:
            base = resolve(dst_dir)
            if not os.path.isdir(base):
                return jsonify({'ok': False, 'error': '目标目录不存在'}), 400
            if files:
                for f in files:
                    name = os.path.basename(f.filename or 'upload.bin')
                    target = resolve(os.path.join(base, name))
                    f.save(target)
                    saved.append({'name': name, 'path': target,
                                  'size': os.path.getsize(target)})
            else:
                b = _req.get_json(silent=True) or {}
                name = os.path.basename(b.get('name') or 'upload.bin')
                target = resolve(os.path.join(base, name))
                import base64
                raw = base64.b64decode(b.get('content_base64') or '')
                with open(target, 'wb') as fh:
                    fh.write(raw)
                saved.append({'name': name, 'path': target, 'size': len(raw)})
            return jsonify({'ok': True, 'saved': saved})
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 403
        except Exception as e:
            logger.exception("[files] 上传失败")
            return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'}), 500

    def _api_download():
        p = _req.args.get('path', '')
        try:
            real = resolve(p)
            if not os.path.isfile(real):
                return jsonify({'ok': False, 'error': '不是文件'}), 400
            return send_file(real, as_attachment=True,
                             download_name=os.path.basename(real))
        except ValueError as e:
            return jsonify({'ok': False, 'error': str(e)}), 403

    ctx.register_api('/api/files/upload', _api_upload, methods=['POST'],
                     description='上传文件（multipart 或 base64）')
    ctx.register_api('/api/files/download', _api_download, methods=['GET'],
                     description='下载文件（二进制流）')

    ctx.log(f"文件管理已就绪：{len(guard.roots)} 个根目录，操作实现取自 fileops")
