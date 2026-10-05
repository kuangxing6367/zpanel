# -*- coding: utf-8 -*-
"""
计划任务（官方扩展）

    backends.py   cron ↔ 系统调度器的翻译与下发（crontab / schtasks）
    main.py       任务清单持久化 + 校验 + 节点命令 + HTTP 接口
    manifest.toml 依赖：cron（表达式）、procs（执行系统命令）

**不自己发明调度器**：只读写系统已有的 crontab / schtasks，
并且只维护带托管标记（zpanel:<id> / zpanel- 前缀）的条目。
"""
