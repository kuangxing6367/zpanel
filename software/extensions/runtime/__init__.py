# -*- coding: utf-8 -*-
"""
运行时管理（官方扩展）

    instance.py   实例模型：config（可持久化）+ 运行态（状态机 / 进程 / 输出缓冲）
    subsystem.py  实例子系统：持久化、编排、输出总线
    adapters.py   运行时适配：探测 PHP / Node / Java，生成启动命令
    main.py       入口：注册数据源与命令进内核，并挂 HTTP 接口
"""
