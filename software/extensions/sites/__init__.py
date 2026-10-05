# -*- coding: utf-8 -*-
"""
站点托管（官方扩展）

    model.py      站点模型 + 映射为机制包描述（to_ngx_spec / to_vhost_spec）
    subsystem.py  站点注册表 + 后端下发编排
    main.py       入口：注册数据源与命令进内核，并挂 HTTP 接口

渲染与 HTTP 服务**不在本扩展里**，而是依赖两个机制包：
    ngxconf   Nginx server 块渲染 / 托管文件落盘清理 / -t 与 -s reload
    vhost     内置虚拟主机（可选兜底后端，默认关闭，绝不占 80）
依赖关系见同目录 manifest.toml。
"""
