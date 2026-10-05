#!/usr/bin/env bash
# ============================================================
# ZPanel 一键安装脚本（对标 1Panel quick_start / aaPanel install.sh）
#
# 用法：
#   bash install.sh --tar ./zpanel-code.tar.gz              # 本地代码包
#   bash install.sh --tar https://example.com/zpanel.tar.gz # 远程代码包
#   bash install.sh --repo https://github.com/<you>/zpanel.git
#   可选：--dir /opt/zpanel  --port 8000  --force（已装目录覆盖，自动备份 data/）
#
# 干了什么：装 4 个真实依赖（pyyaml/flask/waitress/psutil）→ 落代码到
# /opt/zpanel → 生成干净的 config.yaml（database 键只有一份！）→
# 注册 systemd 服务并启动 → 防火墙放行 → 告诉你账号和改密入口。
# ============================================================
set -euo pipefail

DIR="/opt/zpanel"
PORT="8000"
UNIT=""          # 空 = 按目录名推导（zpanel / zpanel-test），多实例并存不撞单元
REPO_URL=""
TAR_URL=""
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)  REPO_URL="$2"; shift 2 ;;
    --tar)   TAR_URL="$2"; shift 2 ;;
    --dir)   DIR="$2"; shift 2 ;;
    --port)  PORT="$2"; shift 2 ;;
    --unit)  UNIT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

[[ $(id -u) -eq 0 ]] || { echo "需要 root 运行"; exit 1; }
command -v systemctl >/dev/null || { echo "需要 systemd"; exit 1; }
command -v python3 >/dev/null || { echo "未找到 python3"; exit 1; }
PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')
[[ -n "$UNIT" ]] || UNIT="zpanel$( [[ "$DIR" == /opt/zpanel ]] && echo '' || echo "-$(basename "$DIR" | sed 's/^zpanel-//')" )"
[[ "$PYV" -ge 310 ]] || { echo "python3 需要 >= 3.10（当前 $(python3 -V)）"; exit 1; }

echo "==> ① 安装运行依赖（真实依赖只有 4 个：pyyaml flask waitress psutil）"
if command -v pip3 >/dev/null; then
  pip3 install -q pyyaml flask waitress psutil || true
fi
# pip3 不可用的发行版兜底（Debian/Ubuntu 系）
if ! python3 -c 'import yaml, flask, waitress, psutil' 2>/dev/null; then
  if command -v apt-get >/dev/null; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip >/dev/null
    pip3 install -q pyyaml flask waitress psutil
  else
    echo "依赖安装失败：请手动安装 pyyaml flask waitress psutil"; exit 1
  fi
fi
python3 -c 'import yaml, flask, waitress, psutil' || { echo "依赖仍不可用"; exit 1; }

echo "==> ② 获取代码到 $DIR"
if [[ -d "$DIR" ]]; then
  if [[ $FORCE -eq 1 ]]; then
    BAK="${DIR}.bak-$(date +%m%d%H%M)"
    echo "    已存在 → 备份原目录到 $BAK（含 data/）"
    cp -a "$DIR" "$BAK"
    rm -rf "$DIR"
  else
    echo "目录已存在：$DIR（--force 覆盖并自动备份 data/）"; exit 1
  fi
fi
mkdir -p "$DIR"
if [[ -n "$REPO_URL" ]]; then
  command -v git >/dev/null || { echo "需要 git（或改用 --tar）"; exit 1; }
  git clone --depth 1 "$REPO_URL" "$DIR"
elif [[ -n "$TAR_URL" ]]; then
  if [[ -f "$TAR_URL" ]]; then
    tar xzf "$TAR_URL" -C "$DIR"
  else
    curl -fsSL "$TAR_URL" | tar xz -C "$DIR"
  fi
  # 兼容「带顶层目录」与「直接平铺」两种打包形态
  if [[ -f "$DIR/main.py" ]]; then :;
  elif [[ -d "$DIR/zpanel" && -f "$DIR/zpanel/main.py" ]]; then
    shopt -s dotglob; mv "$DIR"/zpanel/* "$DIR"/; rmdir "$DIR/zpanel"
  else
    echo "代码包里没找到 main.py"; exit 1
  fi
else
  echo "必须提供 --repo 或 --tar 之一（官方发布地址见 README）"; exit 1
fi

echo "==> ③ 生成 config.yaml（干净模板：database 键只有一份，监听 0.0.0.0:$PORT）"
cat > "$DIR/config.yaml" <<EOF
# ZPanel 配置（由 install.sh 生成于 $(date '+%F %T')）
database:
  type: sqlite
  path: data/zernus.db
task_queue:
  workers: 4
  max_history: 200
core:
  host: 127.0.0.1
  port: $((PORT + 100))
log:
  level: "INFO"
  file: data/logs/zernus.log
  log_raw_message: false
  log_sent_message: false
service:
  sys:    { host: 127.0.0.1, port: $((PORT + 101)) }
  user:   { host: 127.0.0.1, user_port: $((PORT + 102)) }
  watchdog: { max_memory_mb: 256, interval: 30 }
zkg:
  local_dir: repo
  official_source: ""          # 官方源发布后填：https://raw.githubusercontent.com/<you>/<repo>/main
plugin:
  dir: software/plugins
  dat_dir: data/plugins_dat
  auto_install_deps_on_startup: true
  max_memory_mb: 64
api:
  enabled: true
  host: 0.0.0.0
  port: ${PORT}
  frontend_dir: frontend/dist
  session_timeout: 43200
  cors_origins: []
ws:
  host: 127.0.0.1
  port: $((PORT + 1))
  origins: []
extensions: {}
ssl:
  enabled: false
  cert: ""
  key: ""
github_proxy: ""
security:
  encrypted: false
  token: ""
  rsa_callback: ""
nodes:
  mode: both
  hub:    { host: 0.0.0.0, port: $((PORT + 110)), path: "" }
  agent:  { hub_host: 127.0.0.1, hub_port: $((PORT + 110)), hub_path: "", name: "", secret: "", interval: 30 }
  registry: { bootstrap: [] }
runtime:  { enabled: true, auto_start: true }
sites:
  enabled: true
  nginx_conf: ""
  nginx_reload: true
  builtin_enabled: false
  builtin_host: 127.0.0.1
  builtin_port: 8080
files:
  enabled: true
  roots: []
terminal:
  enabled: true
  default_cwd: ""
  timeout: 60
monitor:
  history_interval: 60
  history_days: 30
services:
  enabled: true
project:
  name: "ZPanel"
EOF

echo "==> ④ 注册 systemd 服务（$UNIT）"
cat > "/etc/systemd/system/${UNIT}.service" <<EOF
[Unit]
Description=ZPanel Ops Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${DIR}
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$UNIT"

echo "==> ⑤ 防火墙放行 $PORT/tcp（ufw 存在且启用时）"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null && echo "    ufw 已放行 ${PORT}/tcp"
fi

IP=$(curl -fsS -m 3 http://ip.3322.net 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "============================================================"
echo "  ZPanel 安装完成"
echo "  面板地址 : http://${IP:-127.0.0.1}:${PORT}"
echo "  默认账号 : admin / admin123  ← 登录后立即改密！"
echo "  目录     : $DIR   日志: journalctl -u $UNIT -f"
echo "  服务     : systemctl {status|restart} $UNIT"
echo "  安全提醒 : 建议置于 TLS 反代之后；公网暴露前先配安全入口/2FA"
echo "============================================================"
