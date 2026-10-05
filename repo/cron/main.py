"""cron —— cron 表达式解析/匹配/下次触发时间（机制包）。

纯逻辑、零依赖、自包含：zkg 包不 import 框架内核，任何插件通过
``ctx.zkg_tool('cron')`` 拿到本模块后即可使用。

稳定 API 表面：
    parse(expr) -> dict                 # 解析为各字段集合（校验合法性）
    match(expr, dt=None) -> bool        # 某时刻是否命中
    next_run(expr, after=None) -> datetime   # 下一次触发时间
    next_runs(expr, count, after=None) -> list  # 往后 count 次
    describe(expr) -> str               # 人类可读描述

表达式：5 段「分 时 日 月 周」，支持 * , - / 与数字；
星号为 *；周字段 0-6（0=周日）。不支持 @daily 等别名与秒级精度（保持极简）。
"""
from __future__ import annotations

import datetime as _dt
import re

_FIELD_RANGES = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (0=Sunday)
]
_FIELD_NAMES = ["minute", "hour", "day", "month", "week"]


class CronError(ValueError):
    """表达式非法。"""


def _parse_field(raw: str, min_v: int, max_v: int) -> set:
    values = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"空字段片段: {raw!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise CronError(f"步长非法: {step_s!r}")
            if step < 1:
                raise CronError(f"步长必须 >= 1: {step}")
        if part == "*" or part == "":
            start, end = min_v, max_v
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise CronError(f"区间非法: {part!r}")
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise CronError(f"字段值非法: {part!r}")
        if not (min_v <= start <= max_v and min_v <= end <= max_v):
            raise CronError(f"字段越界 [{min_v}-{max_v}]: {part!r}")
        if start > end:
            raise CronError(f"区间起点大于终点: {part!r}")
        values.update(range(start, end + 1, step))
    return values


def parse(expr: str) -> dict:
    """解析 5 段 cron 表达式，返回 {minute:set, hour:set, day:set, month:set, week:set}。"""
    if not isinstance(expr, str) or not expr.strip():
        raise CronError("表达式为空")
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(f"表达式必须为 5 段（分 时 日 月 周）: {expr!r}")
    return {name: _parse_field(p, lo, hi)
            for name, p, (lo, hi) in zip(_FIELD_NAMES, parts, _FIELD_RANGES)}


def match(expr: str, dt: _dt.datetime = None) -> bool:
    """给定时刻（默认当前本地时间）是否命中该表达式。"""
    dt = dt or _dt.datetime.now()
    f = parse(expr)
    if dt.minute not in f["minute"] or dt.hour not in f["hour"] \
            or dt.month not in f["month"]:
        return False
    # 日与周同时受限时为「或」语义（POSIX cron 惯例）
    dom_restricted = f["day"] != set(range(1, 32))
    dow_restricted = f["week"] != set(range(0, 7))
    dom_ok = dt.day in f["day"]
    dow_ok = ((dt.weekday() + 1) % 7) in f["week"]   # Python 周一=0 → cron 0=周日
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_run(expr: str, after: _dt.datetime = None) -> _dt.datetime:
    """after（默认现在）之后的下一次触发时间；一年内未命中抛 CronError。"""
    after = after or _dt.datetime.now()
    t = (after + _dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = after + _dt.timedelta(days=366)
    while t <= limit:
        if match(expr, t):
            return t
        t += _dt.timedelta(minutes=1)
    raise CronError(f"一年内无触发时间: {expr!r}")


def next_runs(expr: str, count: int, after: _dt.datetime = None) -> list:
    """after 之后的 count 次触发时间。"""
    out = []
    cursor = after or _dt.datetime.now()
    for _ in range(max(0, int(count))):
        t = next_run(expr, cursor)
        out.append(t)
        cursor = t
    return out


_DOW_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']


def describe(expr: str) -> str:
    """人类可读的一句话描述。

    坑（踩过）：``parse()`` 返回的是**展开后的集合，步长信息已丢失**，
    所以 ``*/N`` 必须回到原始表达式上用正则识别 ——
    拿集合反推步长既不可靠，还会把「每 5 分钟」说成「每天 00:00」（旧实现的真实 bug）。

    覆盖不到的组合退化为「忠实列举字段值」，宁可啰嗦也不说错。
    """
    raw = str(expr or '').strip()
    f = parse(raw)
    minute, hour = f['minute'], f['hour']
    day, month, week = f['day'], f['month'], f['week']
    parts = raw.split()

    def step(p):
        m = re.match(r'^\*/(\d+)$', p)
        return int(m.group(1)) if m else None

    def listv(v, unit='', limit=5):
        s = sorted(v)
        if len(s) == 1:
            return f'{s[0]}{unit}'
        head = '、'.join(f'{x}{unit}' for x in s[:limit])
        return head + (f' 等 {len(s)} 个' if len(s) > limit else '')

    def dow(v):
        return [_DOW_NAMES[x % 7] for x in sorted(v)]

    mstep = step(parts[0]) if len(parts) > 0 else None
    hstep = step(parts[1]) if len(parts) > 1 else None
    h_all = hour == set(range(24))
    d_all = day == set(range(1, 32))
    mo_all = month == set(range(1, 13))
    w_all = week == set(range(7))

    # ── 时间/频率 ──
    if mstep and h_all:
        # 已经覆盖全天，再说"每天"就是废话
        return f'每 {mstep} 分钟触发'
    elif mstep and hstep:
        freq = f'每 {hstep} 小时的每 {mstep} 分钟'
    elif mstep:
        freq = f'{listv(hour, "时")}的每 {mstep} 分钟'
    elif h_all:
        freq = f'每小时第 {listv(minute)} 分'
    elif len(minute) == 1 and len(hour) == 1:
        freq = f'{sorted(hour)[0]:02d}:{sorted(minute)[0]:02d}'
    else:
        freq = f'{listv(hour, "时")}的{listv(minute, "分")}'

    # ── 日期范围（日与周同时受限时为「或」语义，POSIX 惯例）──
    month_prefix = listv(month, '月') if not mo_all else '每月'
    if not w_all and d_all:
        date = '每周' + '、'.join(dow(week))
    elif not d_all and w_all:
        date = month_prefix + listv(day, '日')
    elif not d_all and not w_all:
        date = month_prefix + listv(day, '日') + '或每周' + '、'.join(dow(week))
    else:
        date = '每天'
    if not mo_all and d_all and w_all:
        date = f'{listv(month, "月")}的' + date

    return f'{date} {freq} 触发'


