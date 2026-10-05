# ZPanel · 运维面板

跨平台运维面板：管理 PHP / Node.js / Java 等运行时与内容，支持**多机统一纳管**。
内核精简，界面由内核直接提供；系统环境相关能力全部由服务层与软件层注册进来。

```
内核只依赖两样外部能力：SQLite（数据） + Flask（界面出口）
```

---

## 一、分层

| 层 | 目录 | 职责 |
| --- | --- | --- |
| 内核级 | `core/` | 数据库、事件总线、Hook、插件加载、**Web API**、**多机管理** |
| 服务级 | `service/` | zkg 包管理、安全帧传输、系统适配（zernus 侧能力） |
| 机制包 | `repo/` | zkg 机制包：零框架依赖、可复用的一等公民能力 |
| 软件级 | `software/` | 扩展（`extensions/`）与用户插件（`plugins/`），**用 manifest 声明依赖机制包** |

四条铁律：

1. **内核不管系统环境**——它不知道 Windows / Linux，也不知道 PHP / Node / Java。
2. **内核只认各层注册的数据与命令**——谁注册、谁实现、谁负责。
3. **界面由内核出**——内核自建 Flask 应用、自带鉴权、直接托管前端产物。
4. **只有内核最小化，服务层与软件层不做最小化**——能力尽量做成 zkg 机制包，
   软件层用 `dependencies` 声明依赖；同一件事（路径安全、进程终止、版本探测…）
   只允许有一份实现。

---

## 二、zkg 机制包（能力住在包里，软件层只声明依赖）

服务层 `service/zkg/` 负责按依赖图加载机制包，**内核对此一无所知** ——
它只提供一个薄取用句柄 `ctx.zkg_tool(name)`。

```
repo/<id>/manifest.toml + main.py           机制包：零框架依赖的能力
software/extensions/<name>/manifest.toml    软件层：dependencies = ["procs", ...]
software/plugins/<name>/manifest.toml       用户插件：同套机制
```

加载链路：

```
scan(repo/ + software/extensions + software/plugins)
  → resolve(依赖图) → 重建 data/plugins.db
  → 按「包→包」依赖拓扑序加载
  → 无人依赖的包**不加载**（剪枝）
```

机制包之间也能互相依赖，包内直接：

```python
import zkg                      # loader 注册的顶层取用句柄
procs = zkg.tool('procs')       # 未加载返回 None，包内应明确报错而非静默降级
```

现役机制包与依赖它的软件层：

| 包 | 能力 | 被谁依赖 |
| --- | --- | --- |
| `pathsafe` | 路径拼接越界校验（abspath 语义） | sandbox |
| `sandbox` | 路径沙箱：realpath + 根白名单（软链外指也拒） | vhost, files |
| `procs` | 进程管控：独立进程组、整树终止、编码判别、输出截断 | ngxconf, probe, runtime, terminal |
| `probe` | 可执行文件发现 + 版本探测（结果带 TTL 缓存） | runtime, sites |
| `ngxconf` | Nginx server 块渲染 / 托管文件落盘清理 / `-t` `-s reload` | sites |
| `vhost` | 内置虚拟主机（Host 路由 + 静态 + 反代），默认关闭 | sites |
| `sysres` | 系统资源读取（psutil 优先 / stdlib 兜底，拿不到就给 None） | monitor |
| `cron` | cron 表达式解析 / 下次触发 / 人类可读描述 | scheduler |
| `cache` / `lock` / `http` / `hashcode` / `exec` | 缓存 / 命名锁 / HTTP 客户端 / 摘要 / 执行 | probe、ngxconf、sites、files 等 |

软件层现状 —— 每个扩展这几行依赖就是它全部的系统环境诉求：

| 扩展 | dependencies | 说明 |
| --- | --- | --- |
| `files` | sandbox, hashcode | 文件管理（路径边界在包里） |
| `terminal` | procs | 命令模式终端（执行/终止在包里） |
| `runtime` | probe, procs | 运行时探测 + 实例托管 |
| `sites` | ngxconf, vhost, probe, http | 站点托管（管 Nginx 配置，不替代 Nginx） |
| `monitor` | sysres | 主机资源监控 |
| `scheduler` | cron, procs | 计划任务：管系统 crontab / schtasks |

**新增能力的正确姿势**：先问「这能不能做成机制包」；能，就放进 `repo/`，
再让需要它的扩展在 manifest 里声明依赖。不要在扩展里手搓第二份实现 ——
路径安全、进程终止、版本探测这类东西一旦有两份，早晚会有一份是错的。

`/api/system/packages` 与概览页的「zkg 机制包」卡片会如实显示：
加载了哪些、**哪些因无人依赖被剪枝**、哪些依赖缺失。

---

## 三、内核能力

### 1. Web API（`core/api/`）

内核直接构建 Flask 应用并托管前端，不依赖任何扩展：

| 接口 | 说明 |
| --- | --- |
| `POST /api/auth/login` | 登录（pbkdf2 校验，签发令牌） |
| `GET  /api/auth/me` | 当前用户 |
| `POST /api/auth/password` | 改密 |
| `GET  /api/nodes` | 纳管节点列表 |
| `POST /api/nodes` | 纳管节点（返回一次性密钥） |
| `POST /api/nodes/<name>/cmd` | 下发命令 |
| `GET  /api/system/info` | 内核元信息 + 已注册的数据源与命令 |
| `GET  /api/system/health` | 探活（免鉴权，恒 200） |

前端产物由 `config.yaml → api.frontend_dir` 指定（默认 `frontend/dist`），
支持 SPA history 路由回退；目录缺失时后端照常提供 API，只在页面位置给出构建提示。

各层要加自己的接口：

```python
def register(ctx):
    ctx.register_api('/api/zpanel/runtimes', handler, methods=['GET'])
```

### 2. 多机管理（`core/nodes/`）

星型拓扑：中心机（hub）监听控制面，节点**主动外连**，因此节点可在 NAT / 防火墙后，
**自身不需要开放任何入向端口**。

**一条 TCP 承载全部流量**——帧的**首字节即类型**，读到即知走哪条处理路径
（`service/transport/mux.py`）：

| 区段 | 用途 | 特性 |
| --- | --- | --- |
| `0x01–0x0F` 控制面 | 握手 / 心跳 / 命令 / 回执 | 小消息，立即处理 |
| `0x10–0x2F` 流面 | 实例 stdout/stdin、订阅、流控 | 按 `stream_id` 多路复用 + 背压 |
| `0x30–0x3F` 文件面 | 分块传输 | 支持断点续传 |

因此实例日志这类大流量**不占 Web API**，也不需要像 MCSM 那样让浏览器绕过面板直连节点。
流面有背压：缓冲到高水位即通知节点暂停，越上限则丢弃并计数——**绝不阻塞采集侧**。

**载荷全量加密 + 整帧认证**（`service/transport/crypto.py`，纯标准库）：

- 密钥分离：`K_mac = HMAC(master,'mac')` 用于认证，`K_enc = HMAC(master,'enc')` 用于加密；
- 加密：`keystream = SHAKE256(K_enc ‖ nonce ‖ dir ‖ seq)`，XOR 自反（实测 ~96 MB/s）；
- 密钥流不复用由三者共同保证：连接级随机 **nonce**（HELLO 明文携带、受 HMAC 保护）
  + 帧内单调 **seq** + 方向前缀 `c2s`/`s2c`；
- 每帧另有 HMAC 认证与时间戳窗口 / 序号防重放（继承自 ZCBOT 的 agent 协议）。

**密钥零配置**（`both` 模式）：节点名默认取主机名，密钥**自动生成并入库**，
不写入 `config.yaml`；实际通讯用的是 `PBKDF2(密钥, salt=节点名)` 的派生值——
库泄露也不能直接冒充节点，弱口令也有迭代兜底。

**服务端允许没有端口**：

```yaml
nodes:
  hub:
    path: ""      # 配了它就只监听 unix socket，不占 TCP 端口（Unix 平台）
    port: 37010   # <=0 且无 path 时：完全不监听，进程照常运行
```

内核在这一层只有三个职责：**管身份、管连接、管路由**——

```python
# 服务层 / 软件层注册能力（内核不关心实现）
fw.nodes.register_provider('system', fn)            # 数据源：() -> dict，随心跳上报
fw.nodes.register_handler('runtimes.list', fn)      # 命令：(args) -> dict，随命令执行

# 内核只负责搬运
fw.nodes.collect()                                  # 采集本机各层数据（agent 上报用）
fw.nodes.execute('runtimes.list', {})               # 执行命令（agent 收到 CMD 时调用）
fw.nodes.send_cmd('node-1', 'ping')                 # 下发命令到指定节点
```

内置的只有**协议级**语义（`ping` / `node.info` / 数据源 `core`），
其余全部由各层注册——所以新增运维能力**不需要改动内核**。

运行模式由 `config.yaml → nodes.mode` 决定：`hub` / `agent` / `both` / `off`。

---

## 四、快速开始

### 一键安装（Linux，推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/kuangxing6367/zpanel/main/install.sh -o install.sh
bash install.sh --repo https://github.com/kuangxing6367/zpanel.git
# 可选：--dir /opt/zpanel --port 8000 --unit zpanel --force
```
脚本自动装 4 个真实依赖 → 落码 → 生成干净配置 → systemd 托管 → 防火墙放行。
机制包官方源（被管机自动拉取）：

```yaml
zkg:
  official_source: https://raw.githubusercontent.com/kuangxing6367/zpanel/main/repo
```

### 手动方式

```bash
# 1) 后端
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source venv/bin/activate && pip install -r requirements.txt  # Linux

python main.py
```

启动后：

| 地址 | 说明 |
| --- | --- |
| `http://127.0.0.1:8000` | 运维面板（内核 API + 前端） |
| `127.0.0.1:37001` | 内核探活通道 |
| `127.0.0.1:38001` / `38002` | sys / user 服务探活通道 |
| `0.0.0.0:37010` | 节点控制面（`nodes.mode` 含 hub 时） |

默认账号 `admin / admin123`（首次启动自举，**请立即改密**）。

```bash
# 2) 前端
cd frontend
npm install
npm run build        # 产物到 frontend/dist，由内核直接托管
npm run dev          # 开发模式，/api 自动代理到 127.0.0.1:8000
```

---

## 五、接入一台被管机器

在中心机纳管（面板「节点 → 纳管节点」拿到一次性密钥），然后在该机器上配置：

```yaml
nodes:
  mode: agent
  agent:
    hub_host: <中心机地址>
    hub_port: 37010
    name: node-1
    secret: <纳管时返回的密钥>
```

密钥只在纳管时返回一次；轮换后旧密钥立即失效。

---

## 六、目录

```
core/            内核：数据库 / 事件 / Hook / 插件加载 / Web API / 多机管理
  api/           内核 Web API（鉴权 · 路由 · 前端托管）
  nodes/         内核级多机管理（注册表 · 控制面 · 代理 · 数据与命令路由）
service/         服务层：zkg 包管理 / 安全帧传输
  zkg/           zkg：包清单 / 依赖解析 / 依赖库 / 按拓扑序加载 / 包间取用句柄
repo/            机制包（每个包 = manifest.toml + main.py）
  build.py       生成 dist/index.json 与 pool/*.tar.gz（可发到远程源）
software/        软件级：extensions（官方扩展） / plugins（用户插件）
frontend/        自研前端（Vite + React + TS）
sql/             建表 SQL
data/            运行时数据（zernus.db / logs）
```

---

## 七、配置要点

| 配置 | 说明 |
| --- | --- |
| `api.host` / `api.port` | 内核 Web API 监听（默认仅本机 8000） |
| `api.frontend_dir` | 前端产物目录 |
| `api.session_timeout` | 登录会话有效期（秒），0 = 不过期 |
| `nodes.mode` | `hub` / `agent` / `both` / `off` |
| `nodes.hub.port` | 控制面端口（默认 37010） |
| `nodes.registry.bootstrap` | 首次引导纳管清单（之后以数据库为准） |
| `database.type` | `sqlite`（默认）/ `mysql` |

---

## 八、安全边界

- 默认仅监听 `127.0.0.1`；公网部署请改 `0.0.0.0` 并置于 TLS 反代之后。
- 节点控制面是**裸 TCP**，不经 Web 证书；公网暴露时请限制来源 IP。
- 节点侧的 shell 类命令需在各层注册时显式开启，内核不下发、不解释业务语义。
- 审计日志写入 `audit_logs` 表（操作者 / 动作 / 目标 / IP / 结果）。

---

## 九、路线图

- [x] 内核 Web API + 前端托管
- [x] 内核级多机管理（纳管 / 心跳 / 命令 / 密钥轮换）
- [x] 通讯层：首字分区多路复用 + 载荷加密 + 可无端口监听
- [x] zkg 机制包体系（依赖图加载 / 剪枝 / 包间取用）
- [x] 运行时管理：探测 + 实例托管（启停 / 输出流 / 崩溃自愈）
- [x] 站点托管：渲染并下发 Nginx 配置（不替代 Nginx）
- [x] 文件管理 / 在线终端 / 主机监控
- [x] **实例视角导航**：本机只是名叫 `localhost` 的一个节点 ——
      点节点进它的详情（概览/实例/站点/文件/终端），点实例进实例详情；
      顶栏随时切换节点，所有页面取数都走 `/api/nodes/<name>/cmd` 这一条通道
- [x] 站点 / 文件 / 终端的前端页面
- [x] 计划任务（管系统 crontab / schtasks，依赖 `cron` + `procs`）
- [ ] 告警
- [ ] 防火墙与安全
- [ ] 文件上传（multipart 走不了节点命令通道，需走流面 0x30 文件帧）

---

## 协议

MIT / Apache-2.0 双协议，任选其一。
