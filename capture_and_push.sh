#!/bin/bash
# 集合竞价捕获 + 推送脚本
# 用法: capture_and_push.sh [auction|close]
# 由 launchd 在北京时间 9:25 和 15:15 调用

MODE="${1:-auction}"
REPO="/Users/admin/.assistant/astock-auction"
LOG="$REPO/logs/push_${MODE}.log"
PYTHON="/usr/bin/python3"

cd "$REPO" || exit 1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始 $MODE 捕获" >> "$LOG"

# ── 时间窗口保护：集合竞价只在 9:25-9:29 窗口内有效 ─────────────────────────
if [ "$MODE" = "auction" ]; then
  HOUR=$(date '+%H')
  MINUTE=$(date '+%M')
  TOTAL_MINUTES=$((HOUR * 60 + MINUTE))
  if [ "$TOTAL_MINUTES" -gt 569 ]; then
    echo "[$(date '+%H:%M:%S')] ⚠️ 已过 9:30，跳过集合竞价捕获" >> "$LOG"
    exit 0
  fi
fi

# ── 拉取最新远程数据（stash 保护本地脏文件，避免 pull 被卡） ─────────────────
git stash --quiet 2>> "$LOG"
git pull auction main --rebase --quiet 2>> "$LOG"
git stash pop --quiet 2>> "$LOG"

# ── 执行数据捕获 ─────────────────────────────────────────────────────────────
if [ "$MODE" = "auction" ]; then
  $PYTHON "$REPO/auction_tracker.py" --capture-auction >> "$LOG" 2>&1
  EXIT_CODE=$?
else
  $PYTHON "$REPO/auction_tracker.py" --capture-close >> "$LOG" 2>&1
  EXIT_CODE=$?
fi

echo "[$(date '+%H:%M:%S')] 脚本退出码: $EXIT_CODE" >> "$LOG"

# ── 只提交竞价相关文件，不受其他脏文件影响 ────────────────────────────────────
if git diff --quiet auction_history.json docs/index.html 2>/dev/null && \
   git diff --cached --quiet auction_history.json docs/index.html 2>/dev/null; then
  echo "[$(date '+%H:%M:%S')] 无变更，跳过 push" >> "$LOG"
  exit 0
fi

git add auction_history.json docs/index.html
git commit -m "auto: ${MODE} $(date '+%Y-%m-%d %H:%M') CST" --quiet

# ── 推送前再 stash + pull，确保不落后远端 ─────────────────────────────────────
git stash --quiet 2>> "$LOG"
git pull auction main --rebase --quiet 2>> "$LOG"
git stash pop --quiet 2>> "$LOG"

git push auction main --quiet >> "$LOG" 2>&1

if [ $? -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] ✅ 推送成功" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ❌ 推送失败" >> "$LOG"
fi
