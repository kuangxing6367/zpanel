# -*- coding: utf-8 -*-
"""
数据库管理（官方扩展）

    main.py        入口：数据源 + 13 条节点命令 + HTTP 接口
    store.py       连接持久化（口令加密入库）、按类型挑 SQL、服务启停委派
    manifest.toml  依赖：dbclient（连接）+ secretbox（加密）+ svcmgr（启停）

**管已有的数据库**：不内置、不实现协议、不抢 3306/5432/6379 端口。
"""
