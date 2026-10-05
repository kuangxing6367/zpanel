"""exec —— 通用命令执行接口（机制包）。

框架提供的一等公民程序化接口：下游「应用」直接 import 调用，
不再各自手搓 subprocess + 读日志反推输出（minecraftconsole 的教训）。

稳定 API 表面：
    run(cmd, *, cwd=None, timeout=30, env=None, shell=False) -> ExecResult
    ExecResult.returncode / .stdout / .stderr / .ok
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(cmd: str, *, cwd=None, timeout: float = 30.0, env=None,
        shell: bool = False) -> ExecResult:
    """在宿主机直接执行命令并取回输出。

    这是框架亲口提供的「终端直接执行命令接口」——下游不再自造执行通道。
    """
    args = cmd if shell else shlex.split(cmd)
    proc = subprocess.run(
        args, cwd=cwd, timeout=timeout, env=env,
        capture_output=True, text=True, shell=shell,
    )
    return ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "")
