#!/bin/bash
# 集合竞价捕获 + 推送脚本
# 用法: capture_and_push.sh [auction|close|watchdog]
# 由 launchd 在北京时间 9:25(auction)、9:30(watchdog)、15:15(close) 调用

MODE="${1:-auction}"
REPO="/Users/admin/.assistant/astock-auction"
LOG="$REPO/logs/push_${MODE}.log"
PYTHON="/usr/bin/python3"
# 显式 CST 时区，避免 NTP 未同步或系统时区漂移导致时间窗判断错位
export TZ=Asia/Shanghai

cd "$REPO" || exit 1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始 $MODE 捕获" >> "$LOG"

# ── 时间窗口保护：集合竞价主路只在 9:25-9:29 窗口内有效 ─────────────────────
# 注意：date '+%H'/%M 在 macOS 上返回带前导零（如 "09"），bash 算术会当八进制报错。
# 用 $((10#...)) 强制十进制解析，避免前导零问题。
#
# 2026-10-20 修复：原逻辑 9:30 后直接 exit 0，但 Python 内的备路降级
# 循环需要等到 9:30+ 才能从东方财富拿到 09:30 首点。放宽到 9:35
# (575)，给 Python 备路循环留 5 分钟空间。
#
# 2026-10-28 修复：watchdog 模式不受 9:20-9:35 时间窗限制（语义就是 9:30 之后才触发，
# 时间窗判断由 Python 内部 watchdog_auction() 负责）
if [ "$MODE" = "auction" ]; then
  HOUR=$((10#$(date '+%H')))
  MINUTE=$((10#$(date '+%M')))
  TOTAL_MINUTES=$((HOUR * 60 + MINUTE))
  if [ "$TOTAL_MINUTES" -lt 560 ]; then
    echo "[$(date '+%H:%M:%S')] ⚠️ 早于 9:20，跳过集合竞价捕获" >> "$LOG"
    exit 0
  fi
  if [ "$TOTAL_MINUTES" -gt 575 ]; then
    echo "[$(date '+%H:%M:%S')] ⚠️ 已过 9:35，跳过集合竞价捕获" >> "$LOG"
    exit 0
  fi
fi

# ── 拉取最新远程数据（stash 保护本地脏文件，避免 pull 被卡） ─────────────────
# watchdog 模式跳过 stash/pull：watchdog 触发时本地 auction_history.json/docs/index.html
# 可能正被本脚本写入，stash + stash pop 在远端有同文件更新时会留下冲突标记。
# watchdog 是 idempotent 的，直接 add+commit+push 更安全。
if [ "$MODE" != "watchdog" ]; then
  git stash --quiet 2>> "$LOG"
  git pull auction main --rebase --quiet 2>> "$LOG"
  git stash pop --quiet 2>> "$LOG"
fi

# ── 执行数据捕获 ─────────────────────────────────────────────────────────────
if [ "$MODE" = "auction" ]; then
  $PYTHON "$REPO/auction_tracker.py" --capture-auction >> "$LOG" 2>&1
  EXIT_CODE=$?
elif [ "$MODE" = "watchdog" ]; then
  $PYTHON "$REPO/auction_tracker.py" --watchdog >> "$LOG" 2>&1
  EXIT_CODE=$?
else
  $PYTHON "$REPO/auction_tracker.py" --capture-close >> "$LOG" 2>&1
  EXIT_CODE=$?
fi

echo "[$(date '+%H:%M:%S')] 脚本退出码: $EXIT_CODE" >> "$LOG"

# watchdog 模式特殊处理：如果 Python 返回 exit=1（无变更/数据已存在/补抓失败），
# 不走 push 流程。只有 exit=0（成功补抓并写入）才 push。
if [ "$MODE" = "watchdog" ] && [ "$EXIT_CODE" -ne 0 ]; then
  echo "[$(date '+%H:%M:%S')] watchdog 无变更或失败，跳过 push" >> "$LOG"
  exit 0
fi

# ── 只提交竞价相关文件，不受其他脏文件影响 ────────────────────────────────────
if git diff --quiet auction_history.json docs/index.html 2>/dev/null && \
   git diff --cached --quiet auction_history.json docs/index.html 2>/dev/null; then
  echo "[$(date '+%H:%M:%S')] 无变更，跳过 push" >> "$LOG"
  exit 0
fi

git add auction_history.json docs/index.html
git commit -m "auto: ${MODE} $(date '+%Y-%m-%d %H:%M') CST" --quiet

# ── 推送前再 stash + pull，确保不落后远端（watchdog 模式跳过，原因同上）──────
if [ "$MODE" != "watchdog" ]; then
  git stash --quiet 2>> "$LOG"
  git pull auction main --rebase --quiet 2>> "$LOG"
  git stash pop --quiet 2>> "$LOG"
fi

git push auction main --quiet >> "$LOG" 2>&1

if [ $? -eq 0 ]; then
  echo "[$(date '+%H:%M:%S')] ✅ 推送成功" >> "$LOG"
else
  echo "[$(date '+%H:%M:%S')] ❌ 推送失败" >> "$LOG"
fi
