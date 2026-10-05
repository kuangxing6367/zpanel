"""repo/build.py —— 生成本地仓库索引与包体。

产出（每个源都暴露一份/多份索引，Resolver 自动合并多份）：
    repo/dist/index.json          主索引（可含 "splits" 指向分片）
    repo/dist/indices/index-*.json  索引分片（--split 时生成）
    repo/pool/<id>-<ver>.tar.gz  每个包的压缩包

索引 schema（官方包与社区包同一套）：
    {"packages": [{id,name,type,version,description,dependencies,provides,entry,
                   url(下载地址), sha256}]}

多份索引：
- 包过多时可 ``python build.py --split N`` 拆成 N 份分片（dist/indices/index-0..N-1.json），
  主 index.json 仅声明 "splits"；框架启动时自动合并所有分片。
- 单个源既支持单文件 index.json，也支持「主文件 + 分片」形式，框架统一处理。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tarfile

try:
    import tomllib
except ImportError:  # 兼容 3.10
    import tomli as tomllib  # type: ignore

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _signing_key() -> bytes:
    """取签名密钥：优先环境变量 ZKG_SIGNING_KEY，其次 --key-file 文件内容。"""
    import sys
    key = os.environ.get("ZKG_SIGNING_KEY", "")
    if key:
        return key.encode("utf-8")
    if "--key-file" in sys.argv:
        path = sys.argv[sys.argv.index("--key-file") + 1]
        with open(path, "rb") as f:
            return f.read().strip()
    return b""


def _hmac_sign(key: bytes, path: str) -> str:
    """对包体整文件做 HMAC-SHA256，返回 hex 签名。"""
    h = hmac.new(key, digestmod=hashlib.sha256)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pkg_files(pkg_dir: str) -> list:
    out = []
    for name in sorted(os.listdir(pkg_dir)):
        if name in ("dist", "pool", "__pycache__"):
            continue
        full = os.path.join(pkg_dir, name)
        if os.path.isfile(full):
            out.append(name)
    return out


def _build_package_list() -> list:
    packages = []
    for entry in sorted(os.listdir(REPO_ROOT)):
        pkg_dir = os.path.join(REPO_ROOT, entry)
        mani = os.path.join(pkg_dir, "manifest.toml")
        if not os.path.isfile(mani):
            continue
        with open(mani, "rb") as f:
            data = tomllib.load(f)
        pkg = data.get("package", data)
        ver = pkg["version"]
        arcname = f"{pkg['id']}-{ver}"
        pool_dir = os.path.join(REPO_ROOT, "pool")
        os.makedirs(pool_dir, exist_ok=True)
        tar_path = os.path.join(pool_dir, f"{arcname}.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tf:
            for fn in _pkg_files(pkg_dir):
                tf.add(os.path.join(pkg_dir, fn),
                       arcname=os.path.join(arcname, fn))
        packages.append({
            "id": pkg["id"],
            "name": pkg.get("name", pkg["id"]),
            "type": pkg.get("type", "tool"),
            "version": ver,
            "description": pkg.get("description", ""),
            "dependencies": list(pkg.get("dependencies", [])),
            "provides": list(pkg.get("provides", [pkg["id"]])),
            "entry": pkg.get("entry"),
            "url": f"pool/{arcname}.tar.gz",   # 下载地址（相对源根）
            "sha256": _sha256(tar_path),
        })
        # 可选 HMAC 签名：ZKG_SIGNING_KEY / --key-file 提供密钥时自动附加
        key = _signing_key()
        if key:
            packages[-1]["signature"] = _hmac_sign(key, tar_path)
    return packages


def _write_json(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def build(split: int = 0) -> dict:
    """生成索引。split>=1 时拆成 N 份分片，主索引仅保留 splits 声明。"""
    packages = _build_package_list()
    dist_dir = os.path.join(REPO_ROOT, "dist")
    os.makedirs(dist_dir, exist_ok=True)

    # 清理旧的分片目录，避免残留
    indices_dir = os.path.join(dist_dir, "indices")
    if os.path.isdir(indices_dir):
        for fn in os.listdir(indices_dir):
            if fn.endswith(".json"):
                os.remove(os.path.join(indices_dir, fn))

    if split and split >= 1:
        os.makedirs(indices_dir, exist_ok=True)
        # 轮询分配到 N 份，避免单文件过大
        chunks = [packages[i::split] for i in range(split)]
        splits = []
        for i, chunk in enumerate(chunks):
            fn = f"indices/index-{i}.json"
            _write_json(os.path.join(dist_dir, fn), {"packages": chunk})
            splits.append(fn)
        index = {
            "generated_by": "zeronus-repo-build",
            "splits": splits,
            "packages": [],
        }
        _write_json(os.path.join(dist_dir, "index.json"), index)
    else:
        index = {"generated_by": "zeronus-repo-build", "packages": packages}
        _write_json(os.path.join(dist_dir, "index.json"), index)

    return index


if __name__ == "__main__":
    import sys
    split_n = 0
    if "--split" in sys.argv:
        try:
            split_n = int(sys.argv[sys.argv.index("--split") + 1])
        except (IndexError, ValueError):
            print("用法: python build.py --split N")
            sys.exit(1)
    idx = build(split=split_n)
    total = len(idx.get("packages", [])) or sum(
        len(c.get("packages", [])) for c in ()  # 分片模式下主文件 packages 为空
    )
    # 统计实际包数
    n = len(idx.get("packages", []))
    if not n and idx.get("splits"):
        import glob as _g
        for sp in idx["splits"]:
            with open(os.path.join(REPO_ROOT, "dist", sp), encoding="utf-8") as f:
                n += len(json.load(f).get("packages", []))
    print(f"生成索引: {n} 个包" + (f"（拆成 {len(idx['splits'])} 份分片）" if idx.get("splits") else ""))
    for p in idx.get("packages", []):
        print(f"  - {p['id']}@{p['version']}  {p['url']}  sha256={p['sha256'][:12]}...")
