#!/usr/bin/env python3
"""
集合竞价 & 大盘数据追踪器
用法:
  python3 auction_tracker.py --capture-auction   # 9:26 AM 运行
  python3 auction_tracker.py --capture-close     # 3:15 PM 运行
  python3 auction_tracker.py --render            # 随时可运行，生成 HTML

数据源: 腾讯行情 (qt.gtimg.cn)
字段[35] = "当前价/成交量/成交额(元)"
  - 9:26 AM 时: 成交额 = 集合竞价成交额（集合竞价结束，连续竞价未开始）
  - 3:15 PM 时: 成交额 = 全天成交额

LaunchAgent 调度:
  9:26 AM Mon-Fri  → --capture-auction
  3:15 PM Mon-Fri  → --capture-close
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import requests
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

# ── 常量 ─────────────────────────────────────────────────────────────────────
CST = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "auction_history.json"
HTML_FILE = BASE_DIR / "docs" / "index.html"

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# ── Tencent Quote 解析 ────────────────────────────────────────────────────────
def fetch_tencent_quotes(codes: list[str]) -> dict[str, list[str]]:
    """
    批量获取腾讯行情数据
    codes: ['sh000001', 'sz399001', 'sz399006']
    返回 {code: fields_list}
    """
    joined = ",".join(codes)
    url = f"https://qt.gtimg.cn/q={joined}"
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 3:
                raise RuntimeError(f"腾讯行情请求失败: {e}") from e
            time.sleep(2 ** attempt)

    result: dict[str, list[str]] = {}
    for line in r.text.strip().splitlines():
        line = line.strip()
        if not line or '="' not in line:
            continue
        key_part, content = line.split('="', 1)
        code_key = key_part.split("_")[-1]    # e.g. v_sh000001 → sh000001
        content = content.rstrip('";')
        result[code_key] = content.split("~")
    return result


def parse_amount_yi(fields: list[str]) -> float | None:
    """从 field[35] 解析成交额（亿元）"""
    try:
        amount_yuan = float(fields[35].split("/")[2])
        return round(amount_yuan / 1e8, 2)
    except (IndexError, ValueError):
        return None


def parse_index_info(fields: list[str]) -> dict:
    """解析指数基本行情"""
    return {
        "price": float(fields[3]) if fields[3] else None,
        "pct_chg": float(fields[32]) if fields[32] else None,
        "amount_yi": parse_amount_yi(fields),
    }


# ── 历史数据存取 ───────────────────────────────────────────────────────────────
def load_history() -> list[dict]:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(records: list[dict]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def upsert(records: list[dict], today_iso: str, updates: dict) -> bool:
    """更新或插入今日记录，返回是否有变化"""
    for rec in records:
        if rec["date"] == today_iso:
            changed = any(rec.get(k) != v for k, v in updates.items())
            rec.update(updates)
            return changed
    records.append({"date": today_iso, **updates})
    return True


# ── 9:25 AM: 集合竞价捕获 ─────────────────────────────────────────────────────
def capture_auction():
    now = datetime.now(CST)
    today = now.date().isoformat()

    # ── 防线①：时间窗口锁 ────────────────────────────────────────────────────
    # 集合竞价 9:25 撮合，9:30 连续竞价开始。超过 9:29 则 field[35] 已含连续竞价，拒绝写入。
    total_min = now.hour * 60 + now.minute
    if total_min < 9 * 60 + 20:   # 早于 9:20 → 市场未开，数据无效
        print(f"  ✗ [{now.strftime('%H:%M')}] 时间过早（9:20 前），拒绝写入")
        return False
    if total_min >= 9 * 60 + 30:  # 9:30 起连续竞价已开始，field[35] 不再是纯集合竞价
        print(f"  ✗ [{now.strftime('%H:%M')}] 已过 9:30，连续竞价已开始，拒绝写入（数据不再精准）")
        return False

    print(f"[{now.strftime('%H:%M')}] 捕获集合竞价数据…")
    quotes = fetch_tencent_quotes(["sh000001", "sz399006"])

    sh_yi  = parse_amount_yi(quotes.get("sh000001", []))
    cyb_yi = parse_amount_yi(quotes.get("sz399006", []))

    if sh_yi is None or cyb_yi is None:
        print(f"  ✗ 数据获取失败: sh={sh_yi}  cyb={cyb_yi}")
        return False

    # ── 防线②：异常值检测 ────────────────────────────────────────────────────
    # 与近 20 日历史均值比较，超过 3 倍视为异常（避免 API 返回全天累计额）
    records = load_history()
    recent = [r for r in records[-20:] if r.get("sh_auction_yi") and r.get("cyb_auction_yi")]
    if len(recent) >= 5:
        avg_sh  = sum(r["sh_auction_yi"]  for r in recent) / len(recent)
        avg_cyb = sum(r["cyb_auction_yi"] for r in recent) / len(recent)
        if sh_yi > avg_sh * 3 or cyb_yi > avg_cyb * 3:
            print(f"  ✗ 异常值拒绝写入！sh={sh_yi}亿（均值{avg_sh:.1f}亿），cyb={cyb_yi}亿（均值{avg_cyb:.1f}亿）")
            print(f"  ℹ️  可能抓到非集合竞价数据，本次跳过，不写入任何内容")
            return False

    print(f"  上证集合竞价: {sh_yi}亿")
    print(f"  创业板集合竞价: {cyb_yi}亿")

    changed = upsert(records, today, {
        "sh_auction_yi": sh_yi,
        "cyb_auction_yi": cyb_yi,
    })
    if changed:
        save_history(records)
        print("  ✓ 已保存")
    render_html(records)
    return True


# ── 3:15 PM: 收盘数据捕获 ─────────────────────────────────────────────────────
def capture_close():
    now = datetime.now(CST)
    today = now.date().isoformat()

    print(f"[{now.strftime('%H:%M')}] 捕获收盘数据…")
    quotes = fetch_tencent_quotes(["sh000001", "sz399001"])

    sh = parse_index_info(quotes.get("sh000001", []))
    sz = parse_index_info(quotes.get("sz399001", []))

    if sh["price"] is None:
        print("  ✗ 上证数据获取失败")
        return False

    total_yi = (sh["amount_yi"] or 0) + (sz["amount_yi"] or 0)
    total_wan = round(total_yi / 10000, 2)  # 亿 → 万亿

    print(f"  上证收盘: {sh['price']}  {sh['pct_chg']:+.2f}%")
    print(f"  沪深成交额: {total_wan}万亿")

    records = load_history()
    changed = upsert(records, today, {
        "sh_close":           sh["price"],
        "sh_pct_chg":         sh["pct_chg"],
        "market_amount_wan":  total_wan,
    })
    if changed:
        save_history(records)
        print("  ✓ 已保存")
    render_html(records)
    return True


# ── HTML 渲染 ─────────────────────────────────────────────────────────────────
def fmt_date_cn(d: date) -> str:
    wd = WEEKDAYS_CN[d.weekday()]
    return f"{d.month:02d}月{d.day:02d}日（{wd}）"


def _mean(lst):
    return sum(lst) / len(lst) if lst else None


def build_analysis(records: list[dict]) -> str:
    """根据历史数据生成分析与策略 HTML 片段。"""
    auc_recs = [r for r in records if r.get("sh_auction_yi") is not None]
    mkt_recs = [r for r in records if r.get("market_amount_wan") is not None]
    close_recs = [r for r in records
                  if r.get("sh_close") is not None and r.get("sh_pct_chg") is not None]

    if len(auc_recs) < 3:
        return ""

    sh_vals  = [r["sh_auction_yi"] for r in auc_recs]
    cyb_vals = [r["cyb_auction_yi"] for r in auc_recs if r.get("cyb_auction_yi") is not None]
    mkt_vals = [r["market_amount_wan"] for r in mkt_recs]

    # ── 近期均值 vs 历史均值 ───────────────────────────────────────────────────
    n = len(sh_vals)
    recent_n  = min(5, n)
    sh_recent = _mean(sh_vals[-recent_n:])
    sh_all    = _mean(sh_vals)
    sh_ratio  = sh_recent / sh_all if sh_all else 1.0

    cyb_recent = _mean(cyb_vals[-recent_n:]) if cyb_vals else None
    cyb_all    = _mean(cyb_vals) if cyb_vals else None
    cyb_ratio  = (cyb_recent / cyb_all) if (cyb_recent and cyb_all) else None

    mkt_recent = _mean(mkt_vals[-recent_n:]) if mkt_vals else None
    mkt_all    = _mean(mkt_vals) if mkt_vals else None
    mkt_ratio  = (mkt_recent / mkt_all) if (mkt_recent and mkt_all) else None

    # ── 连续方向 ──────────────────────────────────────────────────────────────
    def trend_str(vals, recent_n=3):
        if len(vals) < 2:
            return "数据不足"
        last = vals[-recent_n:] if len(vals) >= recent_n else vals
        ups = sum(1 for i in range(1, len(last)) if last[i] > last[i-1])
        dns = sum(1 for i in range(1, len(last)) if last[i] < last[i-1])
        if ups > dns:
            return "持续放量 📈"
        elif dns > ups:
            return "持续缩量 📉"
        return "震荡横盘 ↔"

    sh_trend  = trend_str(sh_vals)
    cyb_trend = trend_str(cyb_vals) if cyb_vals else "—"
    mkt_trend = trend_str(mkt_vals) if mkt_vals else "—"

    # ── 创业板 / 上证 比值（风险偏好）────────────────────────────────────────
    ratio_pts = []
    for r in auc_recs:
        if r.get("sh_auction_yi") and r.get("cyb_auction_yi") and r["sh_auction_yi"] > 0:
            ratio_pts.append(r["cyb_auction_yi"] / r["sh_auction_yi"])
    ratio_recent = _mean(ratio_pts[-5:]) if len(ratio_pts) >= 5 else _mean(ratio_pts)
    ratio_all    = _mean(ratio_pts) if ratio_pts else None
    risk_signal  = ""
    if ratio_recent and ratio_all:
        if ratio_recent > ratio_all * 1.05:
            risk_signal = "创业板/上证比值高于均值，风险偏好上升，成长股相对活跃。"
        elif ratio_recent < ratio_all * 0.95:
            risk_signal = "创业板/上证比值低于均值，资金偏向防御，成长股热情不足。"
        else:
            risk_signal = "创业板/上证比值接近均值，市场风险偏好中性。"

    # ── 集合竞价 vs 当日涨跌关联 ─────────────────────────────────────────────
    paired = []
    auc_map = {r["date"]: r["sh_auction_yi"] for r in auc_recs if r.get("sh_auction_yi")}
    for r in close_recs:
        if r["date"] in auc_map:
            paired.append((auc_map[r["date"]], r["sh_pct_chg"]))

    corr_note = ""
    if len(paired) >= 5:
        high_auc = [p for p in paired if p[0] >= _mean([x[0] for x in paired])]
        low_auc  = [p for p in paired if p[0] <  _mean([x[0] for x in paired])]
        high_avg_pct = _mean([p[1] for p in high_auc]) if high_auc else 0
        low_avg_pct  = _mean([p[1] for p in low_auc])  if low_auc  else 0
        if high_avg_pct > low_avg_pct + 0.1:
            corr_note = (f"历史数据显示：集合竞价成交额偏高的交易日，当日大盘平均涨幅"
                         f"<span class='pos'>+{high_avg_pct:.2f}%</span>，"
                         f"偏低时平均涨幅 <span class='neg'>{low_avg_pct:.2f}%</span>，"
                         f"集合竞价放量对当日行情有一定正向预示。")
        elif low_avg_pct > high_avg_pct + 0.1:
            corr_note = (f"历史数据显示：集合竞价成交额偏高时当日大盘平均"
                         f"<span class='neg'>{high_avg_pct:.2f}%</span>，"
                         f"偏低时平均 <span class='pos'>+{low_avg_pct:.2f}%</span>，"
                         f"近期放量开盘伴随卖压，需注意高开低走风险。")
        else:
            corr_note = "集合竞价成交额与当日涨跌相关性尚不显著，需持续观察。"

    # ── 综合策略 ──────────────────────────────────────────────────────────────
    strategy_items = []

    heat_label = ""
    if sh_ratio >= 1.15:
        heat_label = "🔥 近期集合竞价明显放量（高于历史均值 {:.0f}%），市场情绪积极。".format((sh_ratio - 1) * 100)
        strategy_items.append("集合竞价持续放量，开盘承接力较强，可关注高开强势股的追涨机会，止损设前日低点。")
    elif sh_ratio <= 0.85:
        heat_label = "❄️ 近期集合竞价明显缩量（低于历史均值 {:.0f}%），市场观望情绪浓厚。".format((1 - sh_ratio) * 100)
        strategy_items.append("集合竞价持续缩量，开盘方向不确定性较大，建议轻仓观望，等待放量信号确认方向后再入场。")
    else:
        heat_label = "⚖️ 近期集合竞价处于历史均值附近，市场情绪平稳。"
        strategy_items.append("集合竞价量能平稳，以跟随大盘趋势操作为主，无明显异动时避免追高。")

    if mkt_ratio:
        if mkt_ratio <= 0.90:
            strategy_items.append("全市场成交额萎缩，流动性不足，趋势性行情难以持续，以高抛低吸为主。")
        elif mkt_ratio >= 1.10:
            strategy_items.append("全市场成交额持续放大，资金活跃度提升，趋势行情可延续，持股信心增强。")

    if risk_signal:
        strategy_items.append(risk_signal)

    if corr_note:
        strategy_items.append(corr_note)

    # ── 当日最新竞价数据摘要 ─────────────────────────────────────────────────
    latest_auc = auc_recs[-1]
    latest_sh  = latest_auc.get("sh_auction_yi")
    latest_cyb = latest_auc.get("cyb_auction_yi")
    latest_date = latest_auc["date"]

    summary_parts = []
    if latest_sh is not None:
        vs_avg = (latest_sh / sh_all - 1) * 100 if sh_all else 0
        sign = "+" if vs_avg >= 0 else ""
        summary_parts.append(
            f"上证集合竞价 <strong class='{'pos' if vs_avg >= 0 else 'neg'}'>"
            f"{latest_sh:.2f}亿</strong>（{sign}{vs_avg:.1f}% vs 历史均值）"
        )
    if latest_cyb is not None and cyb_all:
        vs_avg = (latest_cyb / cyb_all - 1) * 100
        sign = "+" if vs_avg >= 0 else ""
        summary_parts.append(
            f"创业板集合竞价 <strong class='{'pos' if vs_avg >= 0 else 'neg'}'>"
            f"{latest_cyb:.2f}亿</strong>（{sign}{vs_avg:.1f}% vs 历史均值）"
        )

    summary_html = "、".join(summary_parts) if summary_parts else ""

    # ── 组装 HTML ─────────────────────────────────────────────────────────────
    items_html = "".join(f"<li>{s}</li>" for s in strategy_items)
    return f"""
<div class="analysis-wrap">
  <div class="analysis-block">
    <div class="an-title">📊 趋势速览</div>
    <div class="an-grid">
      <div class="an-card">
        <div class="an-label">集合竞价上证</div>
        <div class="an-val">{sh_trend}</div>
        <div class="an-sub">近{recent_n}日均值 {sh_recent:.1f}亿 vs 历史 {sh_all:.1f}亿</div>
      </div>
      <div class="an-card">
        <div class="an-label">集合竞价创业板</div>
        <div class="an-val">{cyb_trend}</div>
        <div class="an-sub">近{recent_n}日均值 {f"{cyb_recent:.1f}亿" if cyb_recent else "—"} vs 历史 {f"{cyb_all:.1f}亿" if cyb_all else "—"}</div>
      </div>
      <div class="an-card">
        <div class="an-label">沪深两市成交额</div>
        <div class="an-val">{mkt_trend}</div>
        <div class="an-sub">近{recent_n}日均值 {f"{mkt_recent:.2f}万亿" if mkt_recent else "—"} vs 历史 {f"{mkt_all:.2f}万亿" if mkt_all else "—"}</div>
      </div>
    </div>
  </div>
  <div class="analysis-block">
    <div class="an-title">🧭 策略参考</div>
    <p class="heat-label">{heat_label}</p>
    {"<p class='summary-p'>最新（" + latest_date + "）：" + summary_html + "</p>" if summary_html else ""}
    <ul class="strategy-list">{items_html}</ul>
    <p class="disclaimer">⚠️ 以上分析仅供参考，不构成投资建议。</p>
  </div>
</div>"""


def build_market_summary(records: list[dict]) -> str:
    """生成今日大盘走势技术面总结 HTML。"""
    close_recs = [r for r in records
                  if r.get("sh_close") is not None and r.get("sh_pct_chg") is not None]
    if len(close_recs) < 2:
        return ""

    closes   = [r["sh_close"]        for r in close_recs]
    pcts     = [r["sh_pct_chg"]      for r in close_recs]
    mkt_vals = [r["market_amount_wan"] for r in close_recs
                if r.get("market_amount_wan") is not None]
    auc_vals = [r["sh_auction_yi"]   for r in close_recs
                if r.get("sh_auction_yi") is not None]

    today     = close_recs[-1]
    prev      = close_recs[-2]
    td_close  = today["sh_close"]
    td_pct    = today["sh_pct_chg"]
    td_date   = today["date"]
    td_mkt    = today.get("market_amount_wan")
    td_auc    = today.get("sh_auction_yi")

    # ── 均线计算 ─────────────────────────────────────────────────────────────
    def ma(n):
        return round(_mean(closes[-n:]), 2) if len(closes) >= n else None

    ma5  = ma(5)
    ma10 = ma(10)
    ma20 = ma(20)

    def ma_tag(price, ma_val, label):
        if ma_val is None:
            return f'<span class="tag-neutral">{label} 数据不足</span>'
        if price > ma_val:
            return f'<span class="tag-up">{label} {ma_val:.2f} ↑站上</span>'
        elif price < ma_val:
            return f'<span class="tag-down">{label} {ma_val:.2f} ↓跌破</span>'
        return f'<span class="tag-neutral">{label} {ma_val:.2f} 贴合</span>'

    ma_tags = " ".join([
        ma_tag(td_close, ma5,  "MA5"),
        ma_tag(td_close, ma10, "MA10"),
        ma_tag(td_close, ma20, "MA20"),
    ])

    # ── 连涨/连跌 ────────────────────────────────────────────────────────────
    streak = 1
    direction = 1 if pcts[-1] > 0 else -1
    for p in reversed(pcts[:-1]):
        if (p > 0) == (direction > 0):
            streak += 1
        else:
            break
    streak_str = ""
    if streak >= 2:
        if direction > 0:
            streak_str = f"连续 <strong>{streak}</strong> 日收涨"
        else:
            streak_str = f"连续 <strong>{streak}</strong> 日收跌"

    # ── 量价关系 ─────────────────────────────────────────────────────────────
    mkt_avg5 = _mean(mkt_vals[-5:]) if len(mkt_vals) >= 5 else _mean(mkt_vals)
    vol_signal = ""
    if td_mkt and mkt_avg5:
        if td_pct > 0 and td_mkt >= mkt_avg5 * 1.05:
            vol_signal = "放量上涨，量价配合，上涨有效性较强。"
        elif td_pct > 0 and td_mkt < mkt_avg5 * 0.95:
            vol_signal = "缩量上涨，量能不足，涨势持续性存疑。"
        elif td_pct < 0 and td_mkt >= mkt_avg5 * 1.05:
            vol_signal = "放量下跌，卖压较重，短期需警惕进一步回调。"
        elif td_pct < 0 and td_mkt < mkt_avg5 * 0.95:
            vol_signal = "缩量下跌，抛压有限，跌势或趋于收敛。"
        else:
            vol_signal = "成交量接近近期均值，市场分歧不大。"

    # ── 集合竞价信号 ─────────────────────────────────────────────────────────
    auc_avg = _mean(auc_vals) if auc_vals else None
    auc_signal = ""
    if td_auc and auc_avg:
        ratio = td_auc / auc_avg
        if ratio >= 1.15:
            auc_signal = f"集合竞价 {td_auc:.2f}亿，明显高于均值（{auc_avg:.1f}亿），开盘做多意愿强烈。"
        elif ratio <= 0.85:
            auc_signal = f"集合竞价 {td_auc:.2f}亿，低于均值（{auc_avg:.1f}亿），开盘热情偏淡。"
        else:
            auc_signal = f"集合竞价 {td_auc:.2f}亿，接近均值（{auc_avg:.1f}亿），开盘情绪平稳。"

    # ── 短期趋势判断 ─────────────────────────────────────────────────────────
    recent5_close = closes[-5:] if len(closes) >= 5 else closes
    trend_up   = sum(1 for i in range(1, len(recent5_close)) if recent5_close[i] > recent5_close[i-1])
    trend_down = sum(1 for i in range(1, len(recent5_close)) if recent5_close[i] < recent5_close[i-1])
    if trend_up > trend_down + 1:
        trend_judge = "短期趋势偏多，价格重心逐步抬升。"
        trend_color = "pos"
    elif trend_down > trend_up + 1:
        trend_judge = "短期趋势偏空，价格重心持续下移。"
        trend_color = "neg"
    else:
        trend_judge = "短期震荡整理，多空力量相对均衡。"
        trend_color = "neutral-text"

    # ── 支撑/压力参考 ────────────────────────────────────────────────────────
    recent_high = max(closes[-10:]) if len(closes) >= 10 else max(closes)
    recent_low  = min(closes[-10:]) if len(closes) >= 10 else min(closes)
    support_str   = f"{recent_low:.2f}"
    resistance_str = f"{recent_high:.2f}"

    # ── 操作建议 ─────────────────────────────────────────────────────────────
    ops = []

    # 仓位基准
    above_ma5  = ma5  and td_close > ma5
    above_ma10 = ma10 and td_close > ma10
    ma_score   = sum([bool(above_ma5), bool(above_ma10),
                      bool(ma20 and td_close > ma20)])   # 0-3

    if ma_score == 3:
        ops.append(("多", "价格站上 MA5/MA10/MA20，多头排列，可维持正常仓位，持股待涨为主。"))
    elif ma_score == 2:
        ops.append(("中", "价格站上多数均线，趋势偏多但尚未完全确认，维持半仓或轻仓，等待回踩均线后加仓。"))
    elif ma_score == 1:
        ops.append(("轻", "仅站上部分均线，结构偏弱，建议轻仓操作，以观望为主。"))
    else:
        ops.append(("空", "价格在均线下方，空头压制，建议空仓或仓位降至最低，等待企稳信号。"))

    # 量价建议
    if td_mkt and mkt_avg5:
        if td_pct > 0 and td_mkt >= mkt_avg5 * 1.05:
            ops.append(("多", "放量上涨，可积极参与，追涨时关注涨停板或强势股。"))
        elif td_pct > 0 and td_mkt < mkt_avg5 * 0.95:
            ops.append(("观", "缩量反弹，可逢低布局低估值品种，但不宜重仓追高。"))
        elif td_pct < 0 and td_mkt >= mkt_avg5 * 1.05:
            ops.append(("空", "放量下跌，卖压较重，建议减仓止损，避免越跌越买。"))
        elif td_pct < 0 and td_mkt < mkt_avg5 * 0.95:
            ops.append(("观", "缩量调整，无需恐慌，持有优质仓位等待方向选择，切忌追杀。"))

    # 支撑压力操作
    dist_to_support    = round((td_close - recent_low)  / td_close * 100, 1)
    dist_to_resistance = round((recent_high - td_close) / td_close * 100, 1)
    if dist_to_support <= 1.0:
        ops.append(("警", f"当前价格已接近近期支撑 {support_str}，若跌破需果断止损离场。"))
    elif dist_to_resistance <= 1.5:
        ops.append(("谨", f"当前价格接近近期压力 {resistance_str}，持仓可考虑部分止盈，不宜此位追多。"))
    else:
        ops.append(("参", f"支撑 {support_str}（距今 {dist_to_support}%），压力 {resistance_str}（距今 {dist_to_resistance}%），当前处于中间区域，以均线为操作参考。"))

    # 明日集合竞价参考
    ops.append(("提", f"明日开盘关注集合竞价额：若高于 {(auc_avg or 70):.0f}亿（历史均值）视为做多积极，可顺势参与；若明显低于均值则谨慎追多。"))

    tag_map = {
        "多": ("op-tag-bull",  "做多"),
        "中": ("op-tag-mid",   "中性偏多"),
        "轻": ("op-tag-mid",   "轻仓"),
        "空": ("op-tag-bear",  "减仓/空仓"),
        "观": ("op-tag-watch", "观望"),
        "警": ("op-tag-bear",  "警示"),
        "谨": ("op-tag-watch", "止盈参考"),
        "参": ("op-tag-mid",   "参考"),
        "提": ("op-tag-watch", "提示"),
    }
    ops_html = ""
    for key, text in ops:
        cls, label = tag_map.get(key, ("op-tag-mid", key))
        ops_html += f'<li><span class="{cls}">{label}</span>{text}</li>'

    # ── 综合观点 ─────────────────────────────────────────────────────────────
    pct_color = "pos" if td_pct > 0 else ("neg" if td_pct < 0 else "neutral-text")
    sign      = "+" if td_pct > 0 else ""
    arrow     = "↑" if td_pct > 0 else ("↓" if td_pct < 0 else "→")

    streak_html = f'<span class="tag-{"up" if direction>0 else "down"}">{streak_str}</span>' if streak_str else ""

    analysis_items = []
    if vol_signal:  analysis_items.append(vol_signal)
    if auc_signal:  analysis_items.append(auc_signal)
    analysis_items.append(trend_judge)
    analysis_items.append(f"近10日参考支撑 <strong>{support_str}</strong>，压力 <strong>{resistance_str}</strong>。")
    items_html = "".join(f"<li>{s}</li>" for s in analysis_items)

    return f"""
<div class="summary-section">
  <div class="sum-header">
    <div class="sum-date">{td_date} 大盘总结</div>
    <div class="sum-close">
      上证 <strong>{td_close:.2f}</strong>
      <span class="{pct_color}"> {sign}{td_pct:.2f}% {arrow}</span>
      {streak_html}
    </div>
  </div>
  <div class="sum-body">
    <div class="sum-col">
      <div class="sum-block-title">均线系统</div>
      <div class="ma-tags">{ma_tags}</div>
      <div class="sum-block-title" style="margin-top:14px">技术分析</div>
      <ul class="sum-list">{items_html}</ul>
    </div>
    <div class="sum-col">
      <div class="sum-block-title">量能概况</div>
      <div class="vol-row">
        <div class="vol-card">
          <div class="vol-label">今日成交额</div>
          <div class="vol-val">{f"{td_mkt:.2f}万亿" if td_mkt else "—"}</div>
          <div class="vol-sub">近5日均值 {f"{mkt_avg5:.2f}万亿" if mkt_avg5 else "—"}</div>
        </div>
        <div class="vol-card">
          <div class="vol-label">集合竞价</div>
          <div class="vol-val">{f"{td_auc:.2f}亿" if td_auc else "—"}</div>
          <div class="vol-sub">历史均值 {f"{auc_avg:.1f}亿" if auc_avg else "—"}</div>
        </div>
      </div>
      <div class="sum-block-title" style="margin-top:14px">短期趋势</div>
      <p class="{trend_color}" style="font-size:0.88rem;line-height:1.6">{trend_judge}</p>
    </div>
  </div>
  <div class="sum-ops">
    <div class="sum-block-title">操作建议</div>
    <ul class="ops-list">{ops_html}</ul>
  </div>
  <p class="disclaimer" style="margin-top:10px">⚠️ 以上为技术面参考，不构成投资建议，请结合自身风险偏好决策。</p>
</div>"""


def render_html(records: list[dict]):
    # ── 表格行 ────────────────────────────────────────────────────────────────
    rows_html = ""
    for i, rec in enumerate(records, 1):
        d = date.fromisoformat(rec["date"])
        date_cn = fmt_date_cn(d)

        sh_a  = rec.get("sh_auction_yi")
        cyb_a = rec.get("cyb_auction_yi")
        mkt   = rec.get("market_amount_wan")
        close = rec.get("sh_close")
        pct   = rec.get("sh_pct_chg")

        sh_str  = f"{sh_a:.2f}亿"  if sh_a  is not None else "—"
        cyb_str = f"{cyb_a:.2f}亿" if cyb_a is not None else "—"
        mkt_str = f"{mkt:.2f}万亿" if mkt   is not None else ""

        if close is not None and pct is not None:
            pct_color = "#e53e3e" if pct > 0 else ("#38a169" if pct < 0 else "#aaa")
            sign = "+" if pct > 0 else ""
            arrow = " ↑" if pct > 0 else (" ↓" if pct < 0 else "")
            market_cell = (
                f'<span style="color:#c53030;font-weight:700">'
                f'上证{close:.2f}</span>&nbsp;'
                f'<span style="color:{pct_color};font-weight:700">'
                f'{sign}{pct:.2f}%{arrow}</span>'
            )
        else:
            market_cell = ""

        rows_html += f"""
        <tr>
          <td>{i}</td>
          <td>{date_cn}</td>
          <td class="red">{sh_str}</td>
          <td class="red">{cyb_str}</td>
          <td>{mkt_str}</td>
          <td>{market_cell}</td>
        </tr>"""

    # ── 折线图数据（JSON）─────────────────────────────────────────────────────
    import json as _json

    chart_dates, sh_auc, cyb_auc, mkt_amt, close_vals, pct_vals = [], [], [], [], [], []
    for rec in records:
        d_str = rec["date"]
        chart_dates.append(d_str[5:])        # MM-DD
        sh_auc.append(rec.get("sh_auction_yi"))
        cyb_auc.append(rec.get("cyb_auction_yi"))
        mkt_amt.append(rec.get("market_amount_wan"))
        close_vals.append(rec.get("sh_close"))
        pct_vals.append(rec.get("sh_pct_chg"))

    dates_js    = _json.dumps(chart_dates,  ensure_ascii=False)
    sh_auc_js   = _json.dumps(sh_auc,       ensure_ascii=False)
    cyb_auc_js  = _json.dumps(cyb_auc,      ensure_ascii=False)
    mkt_amt_js  = _json.dumps(mkt_amt,      ensure_ascii=False)
    pct_js      = _json.dumps(pct_vals,     ensure_ascii=False)

    # ── 分析与策略 ────────────────────────────────────────────────────────────
    analysis_html  = build_analysis(records)
    summary_html   = build_market_summary(records)

    updated_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>集合竞价 &amp; 大盘数据</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
      background: #f4f6f9;
      color: #1a202c;
      min-height: 100vh;
      padding: 32px 16px 60px;
    }}
    h1 {{
      text-align: center;
      font-size: 1.5rem;
      font-weight: 800;
      color: #1a202c;
      margin-bottom: 4px;
      letter-spacing: 3px;
    }}
    .meta {{
      text-align: center;
      font-size: 0.78rem;
      color: #888;
      margin-bottom: 28px;
    }}
    .wrap {{ max-width: 960px; margin: 0 auto; }}

    /* ── 折线图 ── */
    .charts-section {{ margin-bottom: 36px; }}
    .section-title {{
      font-size: 1rem; font-weight: 700; color: #2d3748;
      margin-bottom: 16px;
      border-left: 3px solid #3b82f6;
      padding-left: 10px;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .chart-grid.single {{ grid-template-columns: 1fr; }}
    .chart-box {{
      background: #fff;
      border-radius: 12px;
      padding: 20px 20px 12px;
      box-shadow: 0 1px 8px rgba(0,0,0,.08);
    }}
    .chart-box h3 {{
      font-size: 0.82rem;
      font-weight: 600;
      color: #718096;
      margin-bottom: 12px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }}
    .chart-box canvas {{ max-height: 220px; }}

    /* ── 分析区 ── */
    .analysis-wrap {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 36px;
    }}
    .analysis-block {{
      background: #fff;
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 1px 8px rgba(0,0,0,.08);
    }}
    .an-title {{
      font-size: 0.95rem; font-weight: 700;
      color: #2d3748; margin-bottom: 14px;
    }}
    .an-grid {{
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
    }}
    .an-card {{
      background: #f7fafc; border-radius: 8px;
      padding: 12px 10px; text-align: center;
    }}
    .an-label {{ font-size: 0.7rem; color: #a0aec0; margin-bottom: 6px; }}
    .an-val {{ font-size: 0.92rem; font-weight: 700; color: #2d3748; margin-bottom: 4px; }}
    .an-sub {{ font-size: 0.68rem; color: #a0aec0; line-height: 1.4; }}
    .heat-label {{
      font-size: 0.88rem; color: #2d3748;
      margin-bottom: 10px; line-height: 1.6;
    }}
    .summary-p {{
      font-size: 0.82rem; color: #718096;
      margin-bottom: 12px; line-height: 1.6;
    }}
    .strategy-list {{
      list-style: none; padding: 0;
    }}
    .strategy-list li {{
      font-size: 0.82rem; color: #4a5568;
      padding: 6px 0 6px 16px;
      border-bottom: 1px solid #edf2f7;
      line-height: 1.6;
      position: relative;
    }}
    .strategy-list li::before {{
      content: "›";
      position: absolute; left: 0;
      color: #3b82f6; font-weight: 700;
    }}
    .strategy-list li:last-child {{ border-bottom: none; }}
    .disclaimer {{
      font-size: 0.72rem; color: #a0aec0;
      margin-top: 12px;
    }}
    .pos {{ color: #e53e3e; font-weight: 700; }}
    .neg {{ color: #38a169; font-weight: 700; }}

    /* ── 数据表 ── */
    .table-section {{ margin-bottom: 0; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 1px 8px rgba(0,0,0,.08);
    }}
    thead tr {{ background: #eef2ff; }}
    thead th {{
      padding: 14px 10px;
      font-size: 0.85rem; font-weight: 700;
      color: #2d3748; text-align: center;
      white-space: nowrap;
      border-bottom: 2px solid #e2e8f0;
    }}
    thead th.red {{ color: #c53030; }}
    tbody tr {{ border-bottom: 1px solid #edf2f7; transition: background .15s; }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{ background: #f7fafc; }}
    tbody td {{
      padding: 12px 10px; font-size: 0.88rem;
      text-align: center; white-space: nowrap;
      color: #2d3748;
    }}
    td.red {{ color: #c53030; font-weight: 600; }}

    /* ── 今日总结 ── */
    .summary-section {{
      background: #fff;
      border-radius: 12px;
      padding: 20px 24px;
      box-shadow: 0 1px 8px rgba(0,0,0,.08);
      margin-bottom: 36px;
    }}
    .sum-header {{
      display: flex; align-items: center; gap: 16px;
      margin-bottom: 16px; flex-wrap: wrap;
    }}
    .sum-date {{ font-size: 0.82rem; color: #a0aec0; }}
    .sum-close {{ font-size: 1.15rem; font-weight: 700; color: #1a202c; }}
    .sum-body {{
      display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
    }}
    .sum-col {{ display: flex; flex-direction: column; }}
    .sum-block-title {{
      font-size: 0.75rem; font-weight: 700; color: #a0aec0;
      text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px;
    }}
    .ma-tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tag-up {{
      background: #fff5f5; color: #c53030; border: 1px solid #feb2b2;
      border-radius: 6px; padding: 3px 8px; font-size: 0.78rem; font-weight: 600;
    }}
    .tag-down {{
      background: #f0fff4; color: #276749; border: 1px solid #9ae6b4;
      border-radius: 6px; padding: 3px 8px; font-size: 0.78rem; font-weight: 600;
    }}
    .tag-neutral {{
      background: #f7fafc; color: #718096; border: 1px solid #e2e8f0;
      border-radius: 6px; padding: 3px 8px; font-size: 0.78rem; font-weight: 600;
    }}
    .sum-list {{
      list-style: none; padding: 0; margin: 0;
    }}
    .sum-list li {{
      font-size: 0.82rem; color: #4a5568; padding: 5px 0 5px 14px;
      border-bottom: 1px solid #edf2f7; line-height: 1.6; position: relative;
    }}
    .sum-list li::before {{
      content: "›"; position: absolute; left: 0; color: #3b82f6; font-weight: 700;
    }}
    .sum-list li:last-child {{ border-bottom: none; }}
    .vol-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .vol-card {{
      background: #f7fafc; border-radius: 8px; padding: 12px; text-align: center;
    }}
    .vol-label {{ font-size: 0.7rem; color: #a0aec0; margin-bottom: 4px; }}
    .vol-val {{ font-size: 1rem; font-weight: 700; color: #2d3748; margin-bottom: 2px; }}
    .vol-sub {{ font-size: 0.68rem; color: #a0aec0; }}
    .neutral-text {{ color: #718096; }}
    .sum-ops {{
      margin-top: 18px; border-top: 1px solid #edf2f7; padding-top: 16px;
    }}
    .ops-list {{
      list-style: none; padding: 0; margin: 0;
      display: grid; grid-template-columns: 1fr 1fr; gap: 0;
    }}
    .ops-list li {{
      font-size: 0.82rem; color: #4a5568;
      padding: 7px 8px 7px 0; border-bottom: 1px solid #edf2f7;
      line-height: 1.6; display: flex; align-items: baseline; gap: 7px;
    }}
    .ops-list li:nth-last-child(-n+2) {{ border-bottom: none; }}
    .op-tag-bull  {{ background:#fff5f5; color:#c53030; border:1px solid #feb2b2; border-radius:5px; padding:1px 7px; font-size:0.72rem; font-weight:700; white-space:nowrap; flex-shrink:0; }}
    .op-tag-bear  {{ background:#f0fff4; color:#276749; border:1px solid #9ae6b4; border-radius:5px; padding:1px 7px; font-size:0.72rem; font-weight:700; white-space:nowrap; flex-shrink:0; }}
    .op-tag-mid   {{ background:#ebf8ff; color:#2b6cb0; border:1px solid #90cdf4; border-radius:5px; padding:1px 7px; font-size:0.72rem; font-weight:700; white-space:nowrap; flex-shrink:0; }}
    .op-tag-watch {{ background:#fffff0; color:#975a16; border:1px solid #f6e05e; border-radius:5px; padding:1px 7px; font-size:0.72rem; font-weight:700; white-space:nowrap; flex-shrink:0; }}

    @media (max-width: 640px) {{
      .chart-grid {{ grid-template-columns: 1fr; }}
      .analysis-wrap {{ grid-template-columns: 1fr; }}
      .an-grid {{ grid-template-columns: 1fr; }}
      .sum-body {{ grid-template-columns: 1fr; }}
      .vol-row {{ grid-template-columns: 1fr 1fr; }}
      .ops-list {{ grid-template-columns: 1fr; }}
      .ops-list li:nth-last-child(-n+2) {{ border-bottom: 1px solid #edf2f7; }}
      .ops-list li:last-child {{ border-bottom: none; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>集合竞价 &amp; 大盘数据</h1>
    <p class="meta">数据源: 腾讯行情 &nbsp;|&nbsp; 每交易日 09:26 &amp; 15:15 自动更新 &nbsp;|&nbsp; 最后更新: {updated_at}</p>

    <!-- 数据明细表 -->
    <div class="section-title" style="margin-bottom:16px">数据明细</div>
    <div class="table-section" style="overflow-x:auto;margin-bottom:36px">
      <table>
        <thead>
          <tr>
            <th>序号</th>
            <th>日期</th>
            <th class="red">集合竞价上证</th>
            <th class="red">集合竞价创业板</th>
            <th>沪深两市成交额</th>
            <th>大盘涨跌情况</th>
          </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
      </table>
    </div>

    <!-- 折线图 -->
    <div class="charts-section">
      <div class="section-title">折线图走势</div>
      <div class="chart-grid">
        <div class="chart-box">
          <h3>集合竞价成交额（亿元）</h3>
          <canvas id="chartAuction"></canvas>
        </div>
        <div class="chart-box">
          <h3>沪深两市成交额（万亿元）</h3>
          <canvas id="chartMkt"></canvas>
        </div>
      </div>
      <div class="chart-grid single">
        <div class="chart-box">
          <h3>大盘涨跌幅（%）</h3>
          <canvas id="chartPct"></canvas>
        </div>
      </div>
    </div>

    <!-- 今日大盘总结 -->
    <div class="section-title" style="margin-bottom:16px">今日大盘走势总结</div>
    {summary_html}

    <!-- 分析与策略 -->
    <div class="section-title" style="margin-bottom:16px">历史趋势分析</div>
    {analysis_html}
  </div>

  <script>
  const LABELS   = {dates_js};
  const SH_AUC   = {sh_auc_js};
  const CYB_AUC  = {cyb_auc_js};
  const MKT_AMT  = {mkt_amt_js};
  const PCT_VALS = {pct_js};

  // 自定义数据标注插件（原生 Canvas 绘制，无需外部依赖）
  const inlineLabels = {{
    id: 'inlineLabels',
    afterDatasetsDraw(chart) {{
      const {{ ctx }} = chart;
      chart.data.datasets.forEach((ds, di) => {{
        const meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        const fmt = ds._labelFmt || (v => v == null ? '' : String(v));
        const color = ds.borderColor || (Array.isArray(ds.backgroundColor) ? null : ds.backgroundColor);
        meta.data.forEach((el, j) => {{
          const val = ds.data[j];
          if (val == null) return;
          const text = fmt(val);
          if (!text) return;
          ctx.save();
          ctx.font = 'bold 9px -apple-system, PingFang SC, sans-serif';
          ctx.textAlign = 'center';
          // 柱状图：正值标在上，负值标在下
          if (chart.config.type === 'bar') {{
            const barColor = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[j] : ds.backgroundColor;
            ctx.fillStyle = barColor || '#666';
            if (val >= 0) {{
              ctx.textBaseline = 'bottom';
              ctx.fillText(text, el.x, el.y - 3);
            }} else {{
              ctx.textBaseline = 'top';
              ctx.fillText(text, el.x, el.y + 3);
            }}
          }} else {{
            ctx.fillStyle = color || '#666';
            ctx.textBaseline = 'bottom';
            ctx.fillText(text, el.x, el.y - 5);
          }}
          ctx.restore();
        }});
      }});
    }}
  }};
  Chart.register(inlineLabels);

  const CHART_DEFAULTS = {{
    responsive: true,
    maintainAspectRatio: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ labels: {{ color: '#4a5568', font: {{ size: 12 }} }} }},
      tooltip: {{
        backgroundColor: '#fff',
        titleColor: '#2d3748',
        bodyColor: '#4a5568',
        borderColor: '#e2e8f0',
        borderWidth: 1,
      }}
    }},
    scales: {{
      x: {{
        ticks: {{ color: '#a0aec0', maxRotation: 45, font: {{ size: 10 }} }},
        grid: {{ color: '#edf2f7' }},
      }},
      y: {{
        ticks: {{ color: '#a0aec0', font: {{ size: 11 }} }},
        grid: {{ color: '#edf2f7' }},
      }}
    }}
  }};

  function lineDataset(label, data, color, dashed=false) {{
    return {{
      label,
      data,
      borderColor: color,
      backgroundColor: color + '22',
      borderWidth: 2,
      borderDash: dashed ? [5,4] : [],
      pointRadius: 3,
      pointHoverRadius: 5,
      tension: 0.3,
      spanGaps: true,
    }};
  }}

  function labeledLine(label, data, color, fmtFn) {{
    const ds = lineDataset(label, data, color);
    ds._labelFmt = fmtFn;
    return ds;
  }}

  // 集合竞价折线图
  new Chart(document.getElementById('chartAuction'), {{
    type: 'line',
    data: {{
      labels: LABELS,
      datasets: [
        labeledLine('集合竞价上证（亿）',   SH_AUC,  '#e53e3e', v => v == null ? '' : v.toFixed(1)),
        labeledLine('集合竞价创业板（亿）', CYB_AUC, '#d97706', v => v == null ? '' : v.toFixed(1)),
      ]
    }},
    options: CHART_DEFAULTS,
  }});

  // 成交额折线图
  new Chart(document.getElementById('chartMkt'), {{
    type: 'line',
    data: {{
      labels: LABELS,
      datasets: [ labeledLine('沪深成交额（万亿）', MKT_AMT, '#3b82f6', v => v == null ? '' : v.toFixed(2)) ]
    }},
    options: CHART_DEFAULTS,
  }});

  // 大盘涨跌柱状图（正负着色）
  const pctColors = PCT_VALS.map(v => v === null ? '#ccc' : (v >= 0 ? '#fc8181' : '#68d391'));
  const pctDs = {{
    label: '上证涨跌幅（%）',
    data: PCT_VALS,
    backgroundColor: pctColors,
    borderRadius: 3,
    _labelFmt: v => v == null ? '' : (v > 0 ? '+' : '') + v.toFixed(2) + '%',
  }};
  new Chart(document.getElementById('chartPct'), {{
    type: 'bar',
    data: {{ labels: LABELS, datasets: [pctDs] }},
    options: {{
      ...CHART_DEFAULTS,
      scales: {{
        ...CHART_DEFAULTS.scales,
        y: {{
          ...CHART_DEFAULTS.scales.y,
          ticks: {{ ...CHART_DEFAULTS.scales.y.ticks, callback: v => v + '%' }}
        }}
      }}
    }},
  }});
  </script>
</body>
</html>"""

    HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ HTML → {HTML_FILE}")


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-auction", action="store_true")
    parser.add_argument("--capture-close",   action="store_true")
    parser.add_argument("--render",          action="store_true")
    args = parser.parse_args()

    try:
        if args.capture_auction:
            ok = capture_auction()
        elif args.capture_close:
            ok = capture_close()
        elif args.render:
            records = load_history()
            render_html(records)
            print("渲染完成.")
            ok = True
        else:
            parser.print_help()
            sys.exit(1)
        sys.exit(0 if ok else 1)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
