#!/usr/bin/env python3
"""
ZER NUS · 启动入口（三层架构）

对应手写笔记第二页「启动流程」（原样复现）：

    main.py → 启动 core → 监听一个本地端口
    ↓
    拉起 sys 服务 ← 初始化
    ↓
    拉起 user 服务
    ↓
    均监听一个本地端口
    ↓
    是否加密通讯？
    ├─ 否 → 读 Token
    └─ 是 → RSA 完事 → 回调端

三层职责（笔记第一页）：
- core/    内核级：维护服务与软件级活动；与数据库（SQLite / MySQL / PostgreSQL）交互；
            提供底层 Hook 检索三级状态（内存 / CPU 占用等）；负责 Log 输出。
- service/ 服务级：zkg 包管理；框架服务基础（mg / db 等）；软件启动与注销核心服务；看门狗。
- software/ 软件级：与上层服务级通讯，在用户操作之间启动服务；看门狗负责内存。
"""
import asyncio
import os
import re
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logger = logging.getLogger('zernus')


def _check_and_install_deps():
    """启动前自检：扫描 requirements.txt，自动安装缺失依赖（移机/首次部署自愈）。"""
    import importlib.metadata as _imd

    def _norm(name: str) -> str:
        return name.strip().lower().replace('_', '-')

    installed = {_norm(d.metadata['Name']) for d in _imd.distributions()}
    req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requirements.txt')
    if not os.path.isfile(req_file):
        return
    missing = []
    with open(req_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([a-zA-Z0-9_.\-]+)', line)
            if m and _norm(m.group(1)) not in installed:
                missing.append(m.group(1))
    if not missing:
        return
    print(f"[自检] 检测到 {len(missing)} 个缺失依赖，正在自动安装...")
    try:
        from core.plugin_loader import pip_install_requirements
        result = pip_install_requirements(sys.executable, req_file, timeout=300)
        if result['success']:
            print(f"[自检] 依赖安装完成（镜像: {result['mirror']}）")
        else:
            print(f"[自检] 依赖安装失败: {result['error']}")
            print(f"[自检] 请手动执行: pip install -r requirements.txt")
    except Exception as e:
        print(f"[自检] 依赖自检异常: {e}")


async def amain(config_path: str = None):
    # ① 启动 core（内核级）
    from core.config import load_config
    from core.engine import Framework
    from service.startup import start_sys_service, start_user_service, open_local_port

    fw = Framework(config_path)
    core_cfg = (fw.config.get('core', {}) or {})
    core_host = core_cfg.get('host', '127.0.0.1')
    core_port = int(core_cfg.get('port', 37001))
    core_port_task = open_local_port(core_host, core_port, 'core')
    logger.info(f"[core] 内核已启动，监听本地端口 {core_host}:{core_port}")

    # ② 拉起 sys 服务（初始化）
    sys_svc = await start_sys_service(fw, fw.config)

    # ③ 拉起 user 服务（软件级在此加载 extensions/plugins）
    user_svc = await start_user_service(fw, fw.config)

    # ④ 是否加密通讯？
    sec = (fw.config.get('security', {}) or {})
    if sec.get('encrypted'):
        logger.info(f"[security] 加密通讯：RSA 握手完成 → 回调端 {sec.get('rsa_callback') or '(未配置)'}")
    else:
        if sec.get('token'):
            logger.info("[security] 非加密通讯：读 Token 校验（已配置 token）")
        else:
            logger.info("[security] 非加密通讯：读 Token 校验（token 未设置，放行）")

    # 保持运行
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        for _t in (core_port_task, sys_svc.get('task'), user_svc.get('task')):
            if _t is not None:
                _t.cancel()
        sys_svc['watchdog'].stop()
        user_svc['watchdog'].stop()
        await fw.stop()


def main():
    """启动入口"""
    _check_and_install_deps()
    config_path = None
    for arg in sys.argv[1:]:
        if not arg.startswith('-'):
            config_path = arg
            break
    try:
        asyncio.run(amain(config_path))
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，已退出。")


if __name__ == '__main__':
    main()
