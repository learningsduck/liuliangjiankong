#!/usr/bin/env bash
# 在已配置好 origin 的仓库根目录执行；需 Personal Access Token（classic：勾选 repo）
# 用法：export GITHUB_TOKEN=ghp_xxxxxxxx   && bash scripts/push-to-github.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "请先设置环境变量 GITHUB_TOKEN（不要提交到仓库）" >&2
  exit 1
fi
REMOTE_URL="https://learningsduck:${GITHUB_TOKEN}@github.com/learningsduck/liuliangjiankong.git"
git push -u "${REMOTE_URL}" main
