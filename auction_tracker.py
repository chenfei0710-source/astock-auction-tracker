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


# ── 9:26 AM: 集合竞价捕获 ─────────────────────────────────────────────────────
def capture_auction():
    now = datetime.now(CST)
    today = now.date().isoformat()

    print(f"[{now.strftime('%H:%M')}] 捕获集合竞价数据…")
    quotes = fetch_tencent_quotes(["sh000001", "sz399006"])

    sh_yi  = parse_amount_yi(quotes.get("sh000001", []))
    cyb_yi = parse_amount_yi(quotes.get("sz399006", []))

    if sh_yi is None or cyb_yi is None:
        print(f"  ✗ 数据获取失败: sh={sh_yi}  cyb={cyb_yi}")
        return False

    print(f"  上证集合竞价: {sh_yi}亿")
    print(f"  创业板集合竞价: {cyb_yi}亿")

    records = load_history()
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


def render_html(records: list[dict]):
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
            pct_color = "#ff4444" if pct > 0 else ("#22c55e" if pct < 0 else "#aaa")
            sign = "+" if pct > 0 else ""
            market_cell = (
                f'<span style="color:#ff8888;font-weight:700">'
                f'上证{close:.2f}</span>&nbsp;'
                f'<span style="color:{pct_color};font-weight:700">'
                f'{sign}{pct:.2f}%</span>'
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

    updated_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>集合竞价 &amp; 大盘数据</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
      background: #0d1117;
      color: #e6edf3;
      min-height: 100vh;
      padding: 32px 16px 60px;
    }}
    h1 {{
      text-align: center;
      font-size: 1.5rem;
      font-weight: 800;
      color: #fff;
      margin-bottom: 4px;
      letter-spacing: 3px;
    }}
    .meta {{
      text-align: center;
      font-size: 0.78rem;
      color: #666;
      margin-bottom: 28px;
    }}
    .wrap {{ max-width: 900px; margin: 0 auto; overflow-x: auto; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #161b22;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 0 40px rgba(0,0,0,.5);
    }}
    thead tr {{ background: #0f2137; }}
    thead th {{
      padding: 14px 10px;
      font-size: 0.85rem;
      font-weight: 700;
      color: #cdd9e5;
      text-align: center;
      white-space: nowrap;
      border-bottom: 2px solid #21262d;
    }}
    thead th.red {{ color: #ff8888; }}
    tbody tr {{ border-bottom: 1px solid #21262d; transition: background .15s; }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{ background: #1c2128; }}
    tbody td {{
      padding: 12px 10px;
      font-size: 0.88rem;
      text-align: center;
      white-space: nowrap;
    }}
    td.red {{ color: #ff8888; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>集合竞价 &amp; 大盘数据</h1>
  <p class="meta">数据源: 腾讯行情 &nbsp;|&nbsp; 更新: {updated_at}</p>
  <div class="wrap">
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
