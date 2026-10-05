# -*- coding: utf-8 -*-
"""
计划任务后端适配器 —— 把 cron 表达式落到**系统自己的调度器**

原则（与「面板不是万能中间件」一致）：**管系统的，不自己发明**。
- POSIX：写 `crontab`（带托管标记，只删自己写的行）
- Windows：`schtasks`（Windows 没有 cron，就用它的任务计划程序）

cron → schtasks 只能覆盖常见形态（每分钟 N、每小时、每天、每周、每月），
覆盖不了的表达式**明确标 unsupported 且不下发**，绝不"差不多就写进去"。
"""
from __future__ import annotations

import csv
import logging
import os
import re

import zkg

logger = logging.getLogger('zernus')

# crontab 行尾标记：只认自己写的行，别人的配置一律不碰
CRON_MARK = 'zpanel'


def _procs():
    mod = zkg.tool('procs')
    if mod is None:
        raise RuntimeError('scheduler 依赖 procs 机制包，但未被加载')
    return mod


def _cron():
    mod = zkg.tool('cron')
    if mod is None:
        raise RuntimeError('scheduler 依赖 cron 机制包，但未被加载')
    return mod


# ── 后端探测 ────────────────────────────────────────────
def detect_backend() -> dict:
    """探测本机可用的系统调度器。"""
    procs = _procs()
    if os.name == 'nt':
        exe = procs.which('schtasks')
        return {'backend': 'schtasks' if exe else None, 'exe': exe or '',
                'platform': 'windows'}
    exe = procs.which('crontab')
    return {'backend': 'crontab' if exe else None, 'exe': exe or '',
            'platform': 'posix'}


# ── cron 表达式 → schtasks 计划 ─────────────────────────
_DOW = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT']


def to_schtasks(expr: str) -> dict:
    """把 5 段 cron 翻译成 schtasks 参数；覆盖不了就明确 unsupported。

    注意：cron 机制包的 `parse()` 返回的字段是**展开后的集合**，不保留步长信息，
    所以 `*/N * * * *` 必须回到原始表达式上用正则识别 —— 拿集合反推步长不可靠。
    """
    raw = str(expr or '').strip()
    try:
        f = _cron().parse(raw)          # 借 cron 机制包做合法性校验
    except Exception as e:
        return {'ok': False, 'reason': f'表达式非法: {e}'}

    minute, hour = f['minute'], f['hour']
    day, month, week = f['day'], f['month'], f['week']
    h_all = hour == set(range(0, 24))
    d_all = day == set(range(1, 32))
    mo_all = month == set(range(1, 13))
    w_all = week == set(range(0, 7))

    def one(v):
        return next(iter(v)) if len(v) == 1 else None

    # */N * * * * —— 每 N 分钟
    m = re.match(r'^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$', raw)
    if m and h_all and d_all and mo_all and w_all:
        n = int(m.group(1))
        if 1 <= n <= 1439:
            return {'ok': True, 'args': ['/sc', 'MINUTE', '/mo', str(n)],
                    'kind': f'每 {n} 分钟'}

    if not (d_all and mo_all and w_all):
        return {'ok': False, 'reason': '暂不支持「第几日 + 第几月」同时受限的表达式'}

    mi, ho, dd, wd = one(minute), one(hour), one(day), one(week)
    if mi is None:
        return {'ok': False, 'reason': '分钟字段必须是单一值（不支持多值/区间）'}

    if h_all:
        return {'ok': True, 'args': ['/sc', 'HOURLY', '/st', f'00:{mi:02d}'],
                'kind': f'每小时第 {mi} 分'}
    if ho is not None and wd is not None:
        return {'ok': True,
                'args': ['/sc', 'WEEKLY', '/d', _DOW[wd % 7], '/st', f'{ho:02d}:{mi:02d}'],
                'kind': f'每周{_DOW[wd % 7]} {ho:02d}:{mi:02d}'}
    if ho is not None and dd is not None:
        return {'ok': True,
                'args': ['/sc', 'MONTHLY', '/d', str(dd), '/st', f'{ho:02d}:{mi:02d}'],
                'kind': f'每月 {dd} 日 {ho:02d}:{mi:02d}'}
    if ho is not None:
        return {'ok': True, 'args': ['/sc', 'DAILY', '/st', f'{ho:02d}:{mi:02d}'],
                'kind': f'每天 {ho:02d}:{mi:02d}'}
    return {'ok': False, 'reason': '该周期 Windows 任务计划程序无对应形态（仅支持 每N分钟/每小时/每天/每周/每月）'}


# ── 下发 / 回收 ─────────────────────────────────────────
def _task_name(tid: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_-]', '-', str(tid or ''))
    return f'zpanel-{safe}'[:60]


def apply_schtasks(task: dict) -> dict:
    procs = _procs()
    if not task.get('enabled', True):
        return remove_schtasks(task)
    tr = to_schtasks(task.get('expr', ''))
    if not tr['ok']:
        return {'ok': False, 'status': 'unsupported', 'note': tr['reason']}

    argv = ['schtasks', '/create', '/f',
            '/tn', _task_name(task['id']),
            '/tr', str(task.get('command') or '').strip()]
    argv += tr['args']
    # 命令里含空格/引号时 Windows 要求 /tr 整体加引号
    if ' ' in str(task.get('command') or ''):
        argv[argv.index('/tr') + 1] = f'"{task["command"].strip()}"'
    res = procs.run(argv, timeout=25, shell=False)
    return {'ok': res.ok, 'status': 'applied' if res.ok else 'error',
            'output': (res.stdout or res.stderr or '').strip()[:400],
            'plan': ' '.join(tr['args']), 'kind': tr.get('kind', '')}


def remove_schtasks(task: dict) -> dict:
    procs = _procs()
    res = procs.run(['schtasks', '/delete', '/f', '/tn', _task_name(task['id'])],
                    timeout=25, shell=False)
    return {'ok': res.ok, 'status': 'removed',
            'output': (res.stdout or res.stderr or '').strip()[:300]}


def query_schtasks() -> list:
    """列出系统里 zpanel- 前缀的任务（用于发现"库里有、系统里没了"的漂移）。

    注意：schtasks CSV 里根目录下的任务名带前导反斜杠（``"\\zpanel-xxx"``），
    不剥掉就会导致「明明创建成功却报缺失」的误判。
    """
    def _clean(n: str) -> str:
        return n.strip().strip('"').lstrip('\\/').strip()

    procs = _procs()
    res = procs.run(['schtasks', '/query', '/fo', 'CSV', '/nh'], timeout=25, shell=False)
    if not res.ok or not res.stdout.strip():
        # 英文/中文 locale 下 /nh 行为不一致，LIST 兜底（列名也是两种语言）
        res = procs.run(['schtasks', '/query', '/fo', 'LIST'], timeout=25, shell=False)
        names = re.findall(r'(?im)^(?:TaskName|任务名):\s*(\S+)', res.stdout or '')
        return [n for n in (_clean(x) for x in names)
                if n.lower().startswith('zpanel-')]
    out = []
    for row in csv.reader((res.stdout or '').splitlines()):
        if not row:
            continue
        name = _clean(row[0])
        if name.lower().startswith('zpanel-'):
            out.append(name)
    return out


def apply_crontab(task: dict, all_tasks: list) -> dict:
    """重写 crontab：保留他人条目，只维护带托管标记的行。"""
    procs = _procs()
    keep = []
    cur = procs.run(['crontab', '-l'], timeout=20, shell=False)
    if cur.stdout:
        for line in cur.stdout.splitlines():
            if f'#{CRON_MARK}:' in line:
                continue           # 我们自己写的行，稍后按当前清单重建
            keep.append(line)

    for t in all_tasks:
        if not t.get('enabled', True):
            continue
        keep.append(f"{t['expr']} {t['command']} #{CRON_MARK}:{t['id']}")

    payload = '\n'.join([l for l in keep if l.strip()]) + '\n' if keep else ''
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix='zp-cron-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
        res = procs.run(['crontab', tmp], timeout=20, shell=False)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {'ok': res.ok, 'status': 'applied' if res.ok else 'error',
            'output': (res.stdout or res.stderr or '').strip()[:300],
            'lines': len([l for l in keep if f'#{CRON_MARK}:' in l])}


def remove_crontab(task: dict, all_tasks: list) -> dict:
    return apply_crontab(task, [t for t in all_tasks if t['id'] != task['id']])
