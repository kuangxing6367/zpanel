"""procs —— 进程管控（机制包）。

框架提供的一等公民程序化接口：**任何**要起子进程、要停子进程的地方统一走这里，
解决三件每次手搓都会写错的事：

    ① 子进程要能被**整棵树**杀掉（否则 shell/bash 的子进程会变孤儿残留）
    ② 超时不能只 kill 父进程，要走上面的树杀
    ③ 中文输出在 Windows 上是 GBK、外部程序可能是 UTF-8，解码不能硬编码

稳定 API 表面：
    popen(cmd, *, cwd=None, env=None, shell=False, **kw) -> subprocess.Popen
        工厂：自动加「独立进程组/新会话」标志，使 kill_tree 可用
    run(cmd, *, cwd=None, timeout=30.0, env=None, shell=False, max_output=..., **kw) -> ProcResult
    kill_tree(pid, *, force=True) -> bool
    which(name) -> str | None
    decoder(encoding=None) -> tuple[解码用 encoding, errors]
    ProcResult(returncode, stdout, stderr, timed_out, truncated, duration)  .ok

设计约定：
- run() 走字节管道 + decode_bytes() 自动判别编码（GBK 的 dir / UTF-8 的 node 都不乱）；
  popen() 用于需要流式读取的场景，走文本模式 + 本地编码。
- 输出超 max_output **字节**即截断并标记 truncated（防一次 cat 大树撑爆内存）。
- 任何异常都不吞：超时抛 TimeoutExpired 由调用方决定（run 内部已处理并返回 timed_out）。
"""
from __future__ import annotations

import locale
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"

# 默认输出上限：256 KB（终端 / 命令输出足够，防内存炸）
DEFAULT_MAX_OUTPUT = 256 * 1024


# ── 编码自适配 ────────────────────────────────────────────
def locale_encoding() -> str:
    """系统本地编码（Windows 中文机通常是 cp936/GBK）。"""
    try:
        return locale.getencoding()                    # Python 3.11+
    except Exception:
        return locale.getpreferredencoding(False)


def decoder(encoding: str | None = None) -> tuple:
    """返回 (encoding, errors)：给**文本模式流式读取**用（bufsize=1 的 Popen）。

    流式读取无法事后补救编码，只能用本地编码 + errors='replace'
    （cmd 内建命令、jar、部分 CLI 的中文输出都是本地代码页）。
    一次性取回结果请用 ``run()`` —— 它走 decode_bytes()，能自动判别 UTF-8。
    """
    return (encoding or locale_encoding()), "replace"


def decode_bytes(raw: bytes, encoding: str | None = None) -> str:
    """把子进程输出字节解码成 str —— 这是本包最容易被做错的一处。

    为什么要判别而不是直接用本地编码：同一台机器上，
    ``cmd /c dir`` 吐 GBK，而 ``node`` / ``python`` 多数吐 UTF-8。
    硬选一个必然有一边变成乱码。

    判别规则（顺序即优先级）：
      ① 显式指定 encoding → 用它；
      ② 字节是**合法 UTF-8 且含非 ASCII** → 按 UTF-8 解；
         （GBK 中文几乎不可能同时是合法 UTF-8，反之 UTF-8 中文必然是合法 GBK ——
           所以「先判 UTF-8」这一条就能把两边分开）
      ③ 否则按本地编码解，坏字节 replace。
    """
    if not raw:
        return ""
    if encoding:
        return raw.decode(encoding, "replace")
    if any(b > 0x7F for b in raw):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return raw.decode(locale_encoding(), "replace")


# ── 进程工厂（关键：让 kill_tree 有效）────────────────────
def _spawn_flags() -> dict:
    """平台专属的「独立进程组」标志。缺了它，杀父进程杀不掉子树。"""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def popen(cmd, *, cwd=None, env=None, shell: bool = False, **kw):
    """构造一个「可整树杀死」的 Popen。

    额外关键字透传给 subprocess.Popen（stdin/stdout/stderr/text/encoding 等）。
    若调用方自己传了 creationflags/start_new_session，则以调用方为准。
    """
    flags = _spawn_flags()
    for k in ("creationflags", "start_new_session"):
        if k in kw:
            flags.pop(k, None)
    enc, errs = decoder(kw.pop("encoding", None))
    kw.setdefault("text", True)
    kw.setdefault("encoding", enc)
    kw.setdefault("errors", errs)
    return subprocess.Popen(cmd, cwd=cwd, env=env, shell=shell, **flags, **kw)


# ── 整树终止 ──────────────────────────────────────────────
def kill_tree(pid, *, force: bool = True) -> bool:
    """杀掉以 pid 为根的整棵进程树。返回是否成功发出终止信号。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if IS_WINDOWS:
        args = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        try:
            r = subprocess.run(args, capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    import signal
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:                                        # 优先整组
        os.killpg(os.getpgid(pid), sig)
        return True
    except Exception:
        pass
    try:                                        # 退化为单进程
        os.kill(pid, sig)
        return True
    except Exception:
        return False


@dataclass
class ProcResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def output(self) -> str:
        """合并输出（stdout 优先，空则用 stderr）。"""
        return self.stdout if self.stdout.strip() else self.stderr


def _clip(data: str, limit: int):
    """按字节上限裁剪（保留**尾部**：日志/报错的尾部信息量最大）。"""
    if limit is None or limit <= 0 or len(data) <= limit:
        return data, False
    raw = data.encode("utf-8", "replace")
    if len(raw) <= limit:
        return data, False
    tail = raw[-limit:]
    return tail.decode("utf-8", "replace"), True


def run(cmd, *, cwd=None, timeout: float = 30.0, env=None, shell: bool = False,
        max_output: int = DEFAULT_MAX_OUTPUT, encoding: str | None = None,
        stdin=None):
    """执行命令并等待结束，返回 ProcResult（超时不抛异常，标记 timed_out）。

    走**字节管道**而非文本模式：拿到原始字节后再交给 decode_bytes() 判别编码，
    这样同一台机器上 GBK 的 ``dir`` 与 UTF-8 的 ``node`` 都不会变乱码。

    stdin：传入可读对象（如打开的文件）即作为子进程标准输入 ——
    导入 SQL 这类「喂文件」的场景用它，**代替 shell 的 < 重定向**
    （拼接 shell 字符串是注入的老巢）。
    """
    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, shell=shell,
                                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **_spawn_flags())
    except OSError as e:
        # 可执行文件不存在 / 无权限：这属于「命令没跑起来」，不是调用方的错误，
        # 返回失败结果而不是抛异常 —— 否则每个调用点都得自己包一层 try。
        return ProcResult(
            returncode=-1, stdout="",
            stderr=f"[procs] 无法启动进程: {type(e).__name__}: {e}",
            duration=time.perf_counter() - t0)
    try:
        out_b, err_b = proc.communicate(timeout=timeout if timeout and timeout > 0 else None)
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid, force=True)          # 关键：整树，不只是父进程
        try:
            out_b, err_b = proc.communicate(timeout=5)
        except Exception:
            out_b, err_b = b"", b""
        timed_out = True
    duration = time.perf_counter() - t0

    out = decode_bytes(out_b or b"", encoding)
    err = decode_bytes(err_b or b"", encoding)
    out, cut1 = _clip(out, max_output)
    err, cut2 = _clip(err, max_output)
    code = -9 if timed_out else (proc.returncode if proc.returncode is not None else -1)
    return ProcResult(returncode=code, stdout=out, stderr=err, timed_out=timed_out,
                      truncated=cut1 or cut2, duration=duration)


def which(name: str) -> str | None:
    """在 PATH 中查找可执行文件（Windows 自动补 .exe/.cmd/.bat）。"""
    if not name:
        return None
    found = shutil.which(name)
    if found:
        return found
    if IS_WINDOWS and not name.lower().endswith((".exe", ".cmd", ".bat")):
        for ext in (".exe", ".cmd", ".bat"):
            found = shutil.which(name + ext)
            if found:
                return found
    return None


def python_exe() -> str:
    """当前解释器绝对路径（起 python 子进程时不要用裸 'python'）。"""
    return sys.executable or "python"
