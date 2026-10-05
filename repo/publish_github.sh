#!/usr/bin/env bash
# ============================================================
# ZPanel 官方机制包仓库发布脚本 —— 把 repo/dist 发布到 GitHub
#
# 用途：zkg 的远程源只要一个静态地址：
#     <base>/dist/index.json  +  <base>/pool/*.tar.gz
# GitHub 完美胜任：raw.githubusercontent.com 直接当 CDN。
#
# 一次性准备（本机做一次）：
#   1. 在 GitHub 建一个仓库，如 <you>/zpanel-repo（public）
#   2. git remote add origin git@github.com:<you>/zpanel-repo.git
#
# 日常发布（每次给 repo/ 加了新机制包或改了代码后）：
#   python3 repo/build.py            # 重建 dist/index.json + pool/*.tar.gz
#   bash repo/publish_github.sh      # 提交并推送 dist/
#
# 消费端（被管机/中心机的 config.yaml）：
#   zkg:
#     official_source: https://raw.githubusercontent.com/<you>/zpanel-repo/main
#
# 之后被管机启动时会拉官方索引：repo 里有的机制包/更新，各节点自动可用。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

command -v git >/dev/null || { echo "需要 git"; exit 1; }
[[ -f repo/dist/index.json ]] || { echo "先跑 python3 repo/build.py 生成 dist/"; exit 1; }
git remote get-url origin >/dev/null 2>&1 || {
  echo "还没有 origin remote。先执行："
  echo "  git remote add origin git@github.com:<you>/zpanel-repo.git"
  exit 1
}

BRANCH="${1:-main}"
git add repo/dist/index.json repo/pool/ 2>/dev/null || true
if git diff --cached --quiet; then
  echo "dist 无变化，无需发布"
else
  git commit -m "publish: mechanism packages $(date '+%F %T')"
  git push origin "$BRANCH"
  ORIGIN_URL=$(git remote get-url origin | sed -E 's#git@github.com:#https://github.com/#; s#\.git$##')
  echo "已发布。消费端配置（config.yaml）："
  echo "  zkg:"
  echo "    official_source: ${ORIGIN_URL/raw.githubusercontent.com/https:\/\/raw.githubusercontent.com}/$( [[ "$BRANCH" == main ]] && echo main || echo "$BRANCH" )"
  echo "  （即 https://raw.githubusercontent.com/<you>/<repo>/${BRANCH}）"
fi
