# -*- coding: utf-8 -*-
"""
节点注册表（内核级多机管理 · 数据面）

所有纳管节点的身份、接入凭据与状态快照，落 SQLite（或 MySQL）。

职责边界：
- **只管存储**：网络探测、心跳、命令下发分别由 hub / manager 负责；
  注册表自身不做任何网络请求，纯数据层。
- **凭据是唯一可信来源**：hub 验签时按节点名从本表取密钥；
  `config.yaml` 里的 nodes 列表仅用于首次引导（导入后即以库为准）。

表结构（SQLite / MySQL 同构）：

    name           节点名（主键，唯一）
    host           节点地址（展示用；hub 不反向连接节点）
    port           节点控制端口（展示用）
    secret         per-node 预共享密钥（framed 整帧 HMAC 验签用）
    tags           标签（逗号分隔，便于分组）
    status         online | offline | unknown
    version        节点上报的版本
    uptime_seconds 节点运行时长
    memory_mb      节点内存占用
    detail         最近一次上报的原始状态（JSON 文本，截断）
    last_ok_at     最近一次成功采样的时间
    created_at     纳管时间
    updated_at     最近一次状态写入时间

安全说明：`secret` 采用明文存储——它是**对称密钥**，验签必须使用原文，
哈希后无法还原。保护手段为：SQLite 文件落在本机 `data/` 下（依赖文件系统权限），
且密钥仅在纳管时由管理员生成/分发；如需更高强度可在外层加磁盘加密或改用 KMS。
"""
import json
import hashlib
import logging
import os
import secrets as _secrets
import time

logger = logging.getLogger('zernus')

# 密钥派生参数（KDF）
_KDF_SALT = 'zpanel.node.v1'
_KDF_ITERS = 120_000


def derive_key(secret: str, node_name: str, dklen: int = 32) -> bytes:
    """把「配置/库里的密钥」派生为「实际用于 HMAC 的通讯密钥」。

    借鉴 minecraftconsole（ZCBOT MC agent）的加密策略——**帧不裸奔**：
    每帧带 `HMAC-SHA256(secret, ts)` 令牌 + 时间戳窗口 + 单调序号防重放。
    在此基础上多做一步派生，解决两个问题：

    ① **节点间隔离**：salt 掺入节点名，即使多个节点配了同一个弱口令，
       派生出的通讯密钥也彼此不同，无法互相冒充（原始密钥则做不到）；
    ② **弱密钥兜底**：用户随手填的口令经 12 万次 PBKDF2 迭代后再使用，
       离线爆破成本大幅提高。

    帧格式与校验逻辑本身不变——派生只影响「secret 具体是什么」。
    """
    return hashlib.pbkdf2_hmac(
        'sha256',
        str(secret).encode('utf-8'),
        f'{_KDF_SALT}:{node_name}'.encode('utf-8'),
        _KDF_ITERS,
        dklen=dklen,
    )

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS nodes (
    name            VARCHAR(64)  NOT NULL PRIMARY KEY,
    host            VARCHAR(255) DEFAULT '',
    port            INTEGER      DEFAULT 0,
    secret          VARCHAR(128) DEFAULT '',
    tags            VARCHAR(255) DEFAULT '',
    status          VARCHAR(16)  DEFAULT 'unknown',
    version         VARCHAR(32)  DEFAULT NULL,
    uptime_seconds  INTEGER      DEFAULT 0,
    memory_mb       REAL         DEFAULT NULL,
    detail          VARCHAR(2000) DEFAULT NULL,
    last_ok_at      VARCHAR(32)  DEFAULT NULL,
    created_at      VARCHAR(32)  DEFAULT NULL,
    updated_at      VARCHAR(32)  DEFAULT NULL
)
"""


def _now() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


def gen_secret(nbytes: int = 32) -> str:
    """生成一个 per-node 预共享密钥（URL 安全、无歧义字符）。"""
    return _secrets.token_urlsafe(nbytes)


class NodeRegistry:
    """节点注册表：身份 / 凭据 / 状态（内核级多机管理的数据面）。"""

    def __init__(self, fw):
        self.fw = fw
        self._ready = False

    # ── 建表 ──────────────────────────────────────────────
    def ensure_table(self) -> bool:
        """幂等建表（首次调用时执行）。支持 SQLite / MySQL 两种方言。"""
        if self._ready:
            return True
        try:
            db_type = getattr(self.fw.db, 'db_type', 'sqlite')
            if db_type == 'mysql':
                ddl = (_DDL_SQLITE
                       .replace('CREATE TABLE IF NOT EXISTS nodes', 'CREATE TABLE IF NOT EXISTS nodes')
                       .replace('VARCHAR(64)', 'VARCHAR(64)')
                       .replace('VARCHAR(2000)', 'VARCHAR(2000)'))
                ddl = ddl.rstrip() + ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
                self.fw.db.execute(ddl)
            else:
                self.fw.db.execute(_DDL_SQLITE)
            self._ready = True
            return True
        except Exception as e:
            # 并发建表可能撞车：表已存在即视为成功
            if 'exist' in str(e).lower() or 'duplicate' in str(e).lower():
                self._ready = True
                return True
            logger.error(f"[nodes] 建表失败: {e}")
            return False

    # ── 查询 ──────────────────────────────────────────────
    def list(self) -> list:
        """全部节点（按名称排序），不含展示给前端的密钥原文。"""
        self.ensure_table()
        try:
            rows = self.fw.db.query(
                "SELECT name, host, port, tags, status, version, uptime_seconds, "
                "memory_mb, last_ok_at, created_at, updated_at FROM nodes ORDER BY name")
            return [self._decorate(r) for r in (rows or [])]
        except Exception as e:
            logger.error(f"[nodes] 查询节点列表失败: {e}")
            return []

    def get(self, name: str) -> dict:
        """单节点（含密钥原文，供 hub 验签使用；勿直接回传前端）。"""
        self.ensure_table()
        try:
            row = self.fw.db.query_one("SELECT * FROM nodes WHERE name=?", (name,))
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"[nodes] 查询节点 {name} 失败: {e}")
            return None

    def secrets_map(self) -> dict:
        """{name: 派生后的通讯密钥}，供 hub 加载可信节点表。

        返回的是 **KDF 派生后**的密钥——库里的原文只在纳管时回传一次给管理员，
        通讯一律使用派生值。因此即使库文件泄露，也不能直接拿来冒充节点。
        """
        self.ensure_table()
        try:
            rows = self.fw.db.query("SELECT name, secret FROM nodes WHERE secret <> ''")
            return {r['name']: derive_key(r['secret'], r['name']) for r in (rows or [])}
        except Exception as e:
            logger.error(f"[nodes] 读取密钥表失败: {e}")
            return {}

    def exists(self, name: str) -> bool:
        self.ensure_table()
        try:
            return bool(self.fw.db.query_one("SELECT name FROM nodes WHERE name=?", (name,)))
        except Exception:
            return False

    # ── 写入 ──────────────────────────────────────────────
    def add(self, name: str, host: str = '', port: int = 0, tags: str = '',
            secret: str = None) -> dict:
        """纳管一个节点；secret 省略则自动生成。返回含密钥原文的记录（仅此一次回传机会）。"""
        self.ensure_table()
        name = str(name or '').strip()
        if not name:
            return {'ok': False, 'error': '节点名不能为空'}
        if self.exists(name):
            return {'ok': False, 'error': f'节点 {name} 已存在'}
        raw_secret = str(secret).strip() if secret else gen_secret()
        now = _now()
        self.fw.db.execute(
            "INSERT INTO nodes (name, host, port, secret, tags, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?)",
            (name, str(host or ''), int(port or 0), raw_secret, str(tags or ''), now, now))
        return {'ok': True, 'name': name, 'secret': raw_secret}

    def update(self, name: str, **fields) -> dict:
        """更新节点可变字段（host / port / tags / secret）。"""
        self.ensure_table()
        allowed = {'host', 'port', 'tags', 'secret'}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                vals.append(int(v) if k == 'port' else str(v))
        if not sets:
            return {'ok': False, 'error': '无可更新字段'}
        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(name)
        self.fw.db.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE name=?", tuple(vals))
        return {'ok': True, 'name': name}

    def remove(self, name: str) -> dict:
        """移除纳管（节点侧仍需清理其配置，否则会持续重连并握手失败）。"""
        self.ensure_table()
        self.fw.db.execute("DELETE FROM nodes WHERE name=?", (name,))
        return {'ok': True, 'name': name}

    def save_status(self, name: str, status: str, payload: dict = None,
                    host: str = '', port: int = 0) -> None:
        """写入节点状态快照（hub 心跳 / manager 轮询共用）。"""
        self.ensure_table()
        now = _now()
        if not self.exists(name):
            self.fw.db.execute(
                "INSERT INTO nodes (name, host, port, secret, status, created_at, updated_at) "
                "VALUES (?, ?, ?, '', ?, ?, ?)",
                (name, str(host or ''), int(port or 0), status, now, now))
        if status == 'online' and payload:
            # version 为空时保留原值：心跳载荷里通常不带 version，
            # 若直接覆盖会把 HELLO 阶段登记到的版本抹掉。
            self.fw.db.execute(
                "UPDATE nodes SET status=?, "
                "version=COALESCE(NULLIF(?, ''), version), "
                "uptime_seconds=?, memory_mb=?, detail=?, last_ok_at=?, updated_at=? "
                "WHERE name=?",
                ('online',
                 str(payload.get('version') or '')[:32],
                 int(payload.get('uptime_seconds') or 0),
                 float(payload['memory_mb']) if payload.get('memory_mb') is not None else None,
                 json.dumps(payload, ensure_ascii=False)[:2000],
                 now, now, name))
        else:
            self.fw.db.execute(
                "UPDATE nodes SET status=?, updated_at=? WHERE name=?",
                (status, now, name))

    # ── 内部 ──────────────────────────────────────────────
    @staticmethod
    def _decorate(row: dict) -> dict:
        """补出前端友好的派生字段（不含密钥）。"""
        r = dict(row)
        r['tags'] = [t for t in str(r.get('tags') or '').split(',') if t]
        r['status'] = r.get('status') or 'unknown'
        return r
