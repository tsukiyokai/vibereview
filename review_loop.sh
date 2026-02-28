#!/usr/bin/env bash
# 轻量轮询：每 60 秒检查一次 open PR 是否有新 push，有才跑 ai_reviewer.py
# 用法：bash review_loop.sh <TOKEN> [INTERVAL] [REPO]
# 无变化时每轮只 1 次 API 调用，避免 ~200 次/轮的浪费

set -euo pipefail

OWNER="cann"
TOKEN="${1:?用法: $0 <TOKEN> [INTERVAL] [REPO]}"
INTERVAL="${2:-60}"
REPO="${3:-hcomm}"
CACHE_FILE="/tmp/.review_loop_${OWNER}_${REPO}_shas"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEAM_FILE="$SCRIPT_DIR/team.txt"

# 从 team.txt 提取 gitcode 账号（第三列，跳过标题行）
TEAM_ACCOUNTS=$(awk 'NR>1 {print $NF}' "$TEAM_FILE" | sort | paste -sd'|' -)

while true; do
  # 1 次 API 调用：拉 open PR 列表，只追踪 team 成员的 PR
  new_shas=$(curl -s -H "PRIVATE-TOKEN: $TOKEN" \
    "https://gitcode.com/api/v5/repos/${OWNER}/${REPO}/pulls?state=open&per_page=100" \
    | python3 -c "
import sys, json
team = set('$TEAM_ACCOUNTS'.split('|'))
try:
    prs = json.load(sys.stdin)
    for p in prs:
        login = p.get('user', {}).get('login', '')
        if login in team:
            print(f'{p[\"number\"]}:{p[\"head\"][\"sha\"][:12]}')
except:
    pass" | sort)

  if [ ! -f "$CACHE_FILE" ]; then
    # 首次运行，跑一次完整审查
    printf "[%s] 首次运行，执行审查\n" "$(date +%H:%M:%S)"
    if FORCE_COLOR=1 python3 "$SCRIPT_DIR/ai_reviewer.py" \
      --token "$TOKEN" --repo "$REPO" --team "$TEAM_FILE" -n 0 --comment; then
      echo "$new_shas" > "$CACHE_FILE"
    else
      # 写空缓存避免重复走首次分支，下轮 SHA diff 会触发重试
      touch "$CACHE_FILE"
      printf "[%s] 审查异常，下轮重试\n" "$(date +%H:%M:%S)"
    fi
  elif [ "$new_shas" != "$(cat "$CACHE_FILE")" ]; then
    # SHA 变了，有新 push 或新 PR
    _diff_out=$(diff <(cat "$CACHE_FILE") <(echo "$new_shas") | grep '^[<>]' | head -5 || true)
    echo "$_diff_out"
    # 提取变更的 PR 编号（从 < 和 > 行中取，去重）
    _changed=$(echo "$_diff_out" | grep -oE '[0-9]+:' | tr -d ':' | sort -u | paste -sd',' -)
    printf "[%s] 检测到变更，执行审查\n" "$(date +%H:%M:%S)"
    if FORCE_COLOR=1 python3 "$SCRIPT_DIR/ai_reviewer.py" \
      --token "$TOKEN" --repo "$REPO" --team "$TEAM_FILE" -n 0 --comment \
      ${_changed:+--highlight "$_changed"}; then
      echo "$new_shas" > "$CACHE_FILE"
    else
      printf "[%s] 审查异常，保留旧缓存，下轮重试\n" "$(date +%H:%M:%S)"
    fi
  else
    printf "."
  fi

  sleep "$INTERVAL"
done
