"""sysres —— 系统资源读取（机制包）。

框架提供的一等公民程序化接口：面板要显示「这台机器现在怎么样」，
统一走这里，不把平台差异散落到各处。

稳定 API 表面：
    snapshot(*, disks=True, net=True, top=0) -> dict
    cpu_percent(interval=0.0) -> float
    memory() -> dict           {total, used, free, percent}
    swap() -> dict
    disks() -> list            [{device, mountpoint, fstype, total, used, free, percent}]
    net_io() -> dict           {bytes_sent, bytes_recv, packets_sent, packets_recv}
    uptime() -> float | None
    load_avg() -> tuple | None
    top_processes(n=10, sort='cpu') -> list
    info() -> dict             {system, release, machine, python, cpu_count, backend}

设计约定：
- **psutil 优先，缺失时 stdlib 兜底**：拿不到的字段一律给 None，绝不编数据；
  `info()['backend']` 会如实标出 'psutil' 还是 'stdlib'，调用方据此决定 UI 该灰掉什么。
- cpu_percent(interval=0) 首次调用会主动打点一次（约 0.1s），不返回假 0；
- top_processes 先打点再读值，并把单核口径归一到整机口径（与任务管理器一致）。
- 不缓存：资源是即时量，缓存即失真。
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import time

try:
    import psutil as _ps
except Exception:                       # 没装 psutil 也能用（降级，字段给 None）
    _ps = None

_BACKEND = "psutil" if _ps is not None else "stdlib"


def backend() -> str:
    return _BACKEND


def info() -> dict:
    """静态环境信息（与进程无关）。"""
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "backend": _BACKEND,
        "pid": os.getpid(),
    }


# ── CPU / 内存 ───────────────────────────────────────────
_CPU_PRIMED = False


def cpu_percent(interval: float = 0.0):
    """CPU 使用率。interval=0 时用「两次采样之差」，见下方说明。

    psutil 的非阻塞调用**需要参照样本**：第一次调用必然返回 0.0。
    面板首页第一屏就显示 CPU，所以这里在首次调用时主动打一次点（约 0.1s），
    避免用户永远看到 0% —— 那会让人以为监控坏了。
    """
    global _CPU_PRIMED
    if _ps is not None:
        if interval and interval > 0:
            return float(_ps.cpu_percent(interval=interval))
        if not _CPU_PRIMED:
            _ps.cpu_percent(None)               # 打点
            time.sleep(0.1)
            _CPU_PRIMED = True
        return float(_ps.cpu_percent(None))
    try:                                        # 无 psutil：用 loadavg 近似（*nix）
        la = os.getloadavg()[0]
        n = os.cpu_count() or 1
        return round(min(100.0, la / n * 100.0), 1)
    except Exception:
        return None


def memory() -> dict:
    if _ps is not None:
        m = _ps.virtual_memory()
        return {"total": m.total, "used": m.used, "free": m.available,
                "percent": float(m.percent)}
    total = _sysconf_pages("SC_PHYS_PAGES")     # Linux 兜底
    avail = _sysconf_pages("SC_AVPHYS_PAGES")
    if total:
        used = total - (avail or 0)
        return {"total": total, "used": used, "free": avail, "percent": round(used / total * 100, 1)}
    return {"total": None, "used": None, "free": None, "percent": None}


def _sysconf_pages(name: str):
    try:
        return os.sysconf(name) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def swap() -> dict:
    if _ps is None:
        return {"total": None, "used": None, "free": None, "percent": None}
    s = _ps.swap_memory()
    return {"total": s.total, "used": s.used, "free": s.free, "percent": float(s.percent)}


# ── 磁盘 / 网络 ──────────────────────────────────────────
def disks() -> list:
    """已挂载的物理磁盘分区（Windows 取盘符，*nix 取挂载点）。"""
    out = []
    if _ps is not None:
        for part in _ps.disk_partitions(all=False):
            try:
                u = _ps.disk_usage(part.mountpoint)
            except Exception:
                continue
            out.append({"device": part.device, "mountpoint": part.mountpoint,
                        "fstype": part.fstype, "total": u.total, "used": u.used,
                        "free": u.free, "percent": float(u.percent)})
        return out
    # stdlib 兜底：只能列「存在的根」
    for root in _fallback_roots():
        try:
            u = shutil.disk_usage(root)
        except Exception:
            continue
        out.append({"device": root, "mountpoint": root, "fstype": "",
                    "total": u.total, "used": u.used, "free": u.free,
                    "percent": round(u.used / u.total * 100, 1) if u.total else 0.0})
    return out


def _fallback_roots() -> list:
    if os.name == "nt":
        import string
        return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    return ["/"]


def net_io() -> dict:
    if _ps is None:
        return {"bytes_sent": None, "bytes_recv": None,
                "packets_sent": None, "packets_recv": None}
    n = _ps.net_io_counters()
    return {"bytes_sent": n.bytes_sent, "bytes_recv": n.bytes_recv,
            "packets_sent": n.packets_sent, "packets_recv": n.packets_recv}


# ── 时间 / 负载 / 进程 ────────────────────────────────────
def uptime():
    if _ps is not None:
        return float(time.time() - _ps.boot_time())
    try:                                        # Linux
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def load_avg():
    try:
        return tuple(os.getloadavg())
    except Exception:
        return None


def top_processes(n: int = 10, sort: str = "cpu", sample: float = 0.15) -> list:
    """资源占用 TOP N；无 psutil 时返回 []（不伪造）。

    两个必须处理的 psutil 细节，否则数字是错的：

    ① **先打点、再读值**：``Process.cpu_percent()`` 第一次调用没有参照样本，
       会以「进程创建至今」为基准算出巨大值（实测能到 1000%+）。
       这里先把所有进程各打一次点，等 ``sample`` 秒后再读，得到真实瞬时占用。
    ② **归一到整机口径**：psutil 的进程 CPU 是「单核百分比」，
       12 核机器上单进程可以 >100%，对用户是噪音。
       这里除以核数并截断到 100%，与任务管理器口径一致。

    另外跳过 pid 0（Windows 的 System Idle Process）—— 它是「空闲」的代名词，
    放进「占用 TOP」里只会永远霸榜，没有信息量。
    """
    if _ps is None:
        return []
    procs = [p for p in _ps.process_iter(["pid", "name", "memory_percent", "username"])
             if p.info.get("pid") not in (0,)]
    for p in procs:                             # ① 打点
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    if sample and sample > 0:
        time.sleep(sample)

    ncpu = os.cpu_count() or 1
    rows = []
    for p in procs:
        try:
            raw = float(p.cpu_percent(None))    # 单核口径
        except Exception:
            raw = 0.0
        i = p.info
        rows.append({
            "pid": i.get("pid"),
            "name": i.get("name") or "",
            "cpu": round(min(100.0, raw / ncpu), 1),      # ② 整机口径
            "mem": round(float(i.get("memory_percent") or 0.0), 1),
            "user": i.get("username") or "",
        })
    key = "mem" if str(sort).lower() in ("mem", "memory") else "cpu"
    rows.sort(key=lambda r: r[key], reverse=True)
    return rows[:max(0, int(n))]


# ── 汇总 ────────────────────────────────────────────────
def snapshot(*, with_disks: bool = True, with_net: bool = True, top: int = 0) -> dict:
    """一次取回面板首页需要的全部指标（字段缺失即 None，不编造）。

    参数名刻意不用 ``disks``/``net``：那会遮蔽同名的模块级函数。
    """
    data = {
        "info": info(),
        "cpu": {"percent": cpu_percent(0.0), "count": os.cpu_count()},
        "memory": memory(),
        "swap": swap(),
        "uptime": uptime(),
        "load": list(load_avg()) if load_avg() else None,
    }
    if with_disks:
        data["disks"] = disks()
    if with_net:
        data["net"] = net_io()
    if top:
        data["top"] = top_processes(top)
    return data
