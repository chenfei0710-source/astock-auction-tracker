#!/usr/bin/env python3
"""
泰国券商促销活动自动更新脚本
每周二 10:30 由 crontab 触发，抓取三家平台活动后重新生成 HTML 并推送 Netlify
"""

import urllib.request
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
OUTPUT     = SCRIPT_DIR / "th-broker-promotions.html"
DEPLOY_DIR = SCRIPT_DIR / "netlify-deploy"
LOG        = SCRIPT_DIR / "update-promotions.log"
NETLIFY    = "/Users/admin/.assistant/node_modules/.bin/netlify"
SITE_ID_FILE = SCRIPT_DIR / ".netlify-site-id"

BKK = timezone(timedelta(hours=7))

def log(msg):
    ts = datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  WARN fetch failed ({url}): {e}")
        return ""

# ── 周标签 ──────────────────────────────────────────────────────────────────
def week_label(dt):
    first_weekday = dt.replace(day=1).weekday()
    week_num = (dt.day - 1 + first_weekday) // 7 + 1
    th_months = ["","ม.ค.","ก.พ.","มี.ค.","เม.ย.","พ.ค.","มิ.ย.",
                 "ก.ค.","ส.ค.","ก.ย.","ต.ค.","พ.ย.","ธ.ค."]
    zh = f"{dt.month}月 第{week_num}周"
    th = f"สัปดาห์ที่ {week_num} {th_months[dt.month]}"
    return zh, th

# ── 解析各平台 ──────────────────────────────────────────────────────────────
def parse_invx(html):
    cards = []
    pairs = re.findall(
        r'href=["\'](/promotions/detail/([^"\']+))["\'][^>]*>.*?<[^>]+>([^<]{5,80})',
        html, re.S
    )
    seen = set()
    for path, slug, title in pairs:
        title = re.sub(r'\s+', ' ', title).strip()
        if slug in seen or len(title) < 5:
            continue
        seen.add(slug)
        cards.append({"url": f"https://www.innovestx.co.th{path}", "title_th": title})
        if len(cards) >= 9:
            break
    if not cards:
        cards = INVX_DEFAULT
    return cards

def parse_webull(html):
    cards = []
    pairs = re.findall(
        r'href=["\'](/(?:activity|en/activity)[^"\']*)["\'][^>]*>.*?([ก-๙A-Za-z][^<]{8,80})',
        html, re.S
    )
    seen = set()
    for path, title in pairs:
        title = re.sub(r'\s+', ' ', title).strip()
        if path in seen or len(title) < 6:
            continue
        seen.add(path)
        cards.append({"url": f"https://www.webull.co.th{path}", "title_th": title})
        if len(cards) >= 5:
            break
    if not cards:
        cards = WEBULL_DEFAULT
    return cards

# ── 静态活动数据（含中泰双语） ────────────────────────────────────────────
DIME_ITEMS = [
    {
        "badge": "NEW",
        "tag_zh": "Desktop", "tag_th": "เดสก์ท็อป",
        "title_zh": "🖥️ Dime! Lucky Desktop Edition",
        "title_th": "ล็อกอิน Dime! Desktop ครั้งแรก รับหุ้นฟรี",
        "desc_zh": "首次登录桌面版，抽赠美股最高 ฿10,000",
        "desc_th": "รับหุ้นสหรัฐฯ ฟรี สูงสุด ฿10,000",
        "date_zh": "⏳ 倒计时约 15 天", "date_th": "นับถอยหลัง ~15 วัน",
        "url": "https://dime.co.th",
    },
    {
        "tag_zh": "新客户", "tag_th": "ลูกค้าใหม่",
        "title_zh": "🎁 新客户开户礼包「ชุดเจิมพอร์ต」",
        "title_th": "ชาว Dime! หน้าใหม่ รับ 'ชุดเจิมพอร์ต' ฟรี",
        "desc_zh": "新用户开户免手续费买美股，领取专属礼包",
        "desc_th": "เปิดบัญชีใหม่ เทรดหุ้นสหรัฐฯ ฟรีค่าคอม",
        "date_zh": "1 ม.ค. – 31 ธ.ค. 2569", "date_th": "1 ม.ค. – 31 ธ.ค. 2569",
        "url": "https://dime.co.th/articles/starter-pack-2026",
    },
    {
        "tag_zh": "邀友", "tag_th": "ชวนเพื่อน",
        "title_zh": "👥 邀友富贵 — ชวนเพื่อน รวยยกแก๊ง",
        "title_th": "ชวนเพื่อน รับ Dime! Lucky สูงสุด ฿1,000",
        "desc_zh": "每邀 1 位好友开户送美股 ฿50，最高 ฿1,000",
        "desc_th": "ชวนเพื่อนเปิดบัญชี รับ Lucky ฿50/คน สูงสุด 15 คน",
        "date_zh": "1 เม.ย. – 31 ธ.ค. 2569", "date_th": "1 เม.ย. – 31 ธ.ค. 2569",
        "url": "https://dime.co.th/th/articles/invite-and-get-rich",
    },
    {
        "badge": "HOT",
        "tag_zh": "黄金", "tag_th": "ทองคำ",
        "title_zh": "🏅 泰国黄金榜 — บัลลังก์ทองคำ",
        "title_th": "เทรดทองติดอันดับ รับทองคำแท่ง YLG",
        "desc_zh": "9 月买金交易量前 4 名，赢实物黄金最多 1 บาท（约 30g）",
        "desc_th": "Top 4 ยอดซื้อทอง ก.ย. รับทองแท่ง 96.5% รวม 1 บาท 2 สลึง",
        "date_zh": "1 – 30 ก.ย. 2569", "date_th": "1 – 30 ก.ย. 2569",
        "url": "https://dime.co.th/th/articles/all-dime-promo",
    },
    {
        "tag_zh": "基金", "tag_th": "กองทุน",
        "title_zh": "📦 买基金送美股 — ซื้อ 1 ได้ถึง 2",
        "title_th": "ภารกิจสะสมกองทุน ซื้อ 1 ได้ถึง 2",
        "desc_zh": "单月累计买基金满 ฿25,000，送美股 Lucky ฿50",
        "desc_th": "สะสมกองทุนครบ ฿25,000/เดือน รับหุ้นสหรัฐฯ ฟรี ฿50",
        "date_zh": "1 ก.ค. – 30 ก.ย. 2569", "date_th": "1 ก.ค. – 30 ก.ย. 2569",
        "url": "https://dime.co.th/en/articles/fundmission-buy1get2-july-2026",
    },
    {
        "tag_zh": "美股", "tag_th": "หุ้นสหรัฐฯ",
        "title_zh": "⚡ Mid-month 免佣金日",
        "title_th": "Mid-month ซื้อหุ้นฟรีค่าคอม ทุกวันที่ 15",
        "desc_zh": "每月 15 日全天美股/泰股不限单数免手续费",
        "desc_th": "วันที่ 15 ทุกเดือน ซื้อหุ้นฟรีค่าคอมฯ ไม่จำกัดรายการ",
        "date_zh": "ทุกวันที่ 15", "date_th": "ทุกวันที่ 15 ของเดือน",
        "url": "https://dime.co.th/th/articles/all-dime-promo",
    },
]

INVX_DEFAULT = [
    {"tag_zh":"美股碎股","tag_th":"เศษหุ้น","title_zh":"💹 碎股交易最低手续费 $1.99","title_th":"INVX เทรดเศษหุ้น รับ Min Com $1.99","desc_zh":"新客户及 Member Tier 最低手续费仅 $1.99/笔","desc_th":"ค่าคอมฯ ขั้นต่ำ $1.99 สำหรับลูกค้าใหม่และ Member Tier","date_zh":"1 ส.ค. – 30 ก.ย. 2569","date_th":"1 ส.ค. – 30 ก.ย. 2569","url":"https://www.innovestx.co.th/promotions/detail/Fracshare_2026"},
    {"tag_zh":"TFEX","tag_th":"TFEX","title_zh":"🚀 TFEX 开户送 DR + 2,000 Points","title_th":"ติดจรวดพอร์ต TFEX รับโบนัส 2 ต่อ","desc_zh":"首开 TFEX 送 DR SpaceX23（฿100）+ 满 3 合约再送 2,000 Points","desc_th":"เปิดบัญชี TFEX รับ DR SpaceX23 + เทรด 3 สัญญา รับ 2,000 INVX Points","date_zh":"31 ก.ค. – 30 ก.ย. 2569","date_th":"31 ก.ค. – 30 ก.ย. 2569","url":"https://www.innovestx.co.th/promotions/detail/tnc-tfex-to-the-moon"},
    {"tag_zh":"泰股","tag_th":"หุ้นไทย","title_zh":"🏹 泰股 To the Moon — 最高 ฿300","title_th":"สตาร์ทพอร์ตหุ้นไทย To the Moon สูงสุด ฿300","desc_zh":"重新激活泰股账户完成任务，获赠权益最高 ฿300","desc_th":"กลับมาเทรดหุ้นไทย รับสิทธิ์สูงสุด ฿300","date_zh":"31 ก.ค. – 30 ก.ย. 2569","date_th":"31 ก.ค. – 30 ก.ย. 2569","url":"https://www.innovestx.co.th/promotions/detail/tnc-reactivate-th-stock-dr"},
    {"badge":"NEW","tag_zh":"教育","tag_th":"การศึกษา","title_zh":"🌿 EverGreen Investing Class 2026","title_th":"EverGreen Investing Class 2026","desc_zh":"长线投资者专属课程，报名参与学习活动","desc_th":"คลาสลงทุนระยะยาว สำหรับนักลงทุนสายถือยาว","date_zh":"1 ก.ย. – 5 พ.ย. 2569","date_th":"1 ก.ย. – 5 พ.ย. 2569","url":"https://www.innovestx.co.th/promotions/detail/evergreen-investmentclass"},
    {"tag_zh":"加密","tag_th":"คริปโต","title_zh":"₿ 新客交易 BTC 送 ฿200 Points","title_th":"ลูกค้าใหม่! เทรด BTC รับ INVX Point ฿200","desc_zh":"新用户首次交易 BTC 获赠 InnovestX Points ฿200","desc_th":"เทรด BTC ครั้งแรก รับ INVX Points มูลค่า ฿200","date_zh":"11 มิ.ย. – 30 ก.ย. 2569","date_th":"11 มิ.ย. – 30 ก.ย. 2569","url":"https://www.innovestx.co.th/promotions/detail/tnc-btc-invx-point-200"},
    {"tag_zh":"加密","tag_th":"คริปโต","title_zh":"₿ BTC 返佣 50%，最高 ฿1,000","title_th":"เทรด BTC คืนค่าคอม 50% สูงสุด ฿1,000","desc_zh":"持续交易 BTC 手续费返现 50%，上限 ฿1,000","desc_th":"เทรด BTC สม่ำเสมอ รับคืนค่าคอมฯ 50% สูงสุด ฿1,000","date_zh":"11 มิ.ย. – 30 ก.ย. 2569","date_th":"11 มิ.ย. – 30 ก.ย. 2569","url":"https://www.innovestx.co.th/promotions/detail/tnc-btc-50"},
    {"tag_zh":"定投","tag_th":"DCA","title_zh":"📅 DCA 定投送 Bonus Points","title_th":"DCA Bonus Points Campaign","desc_zh":"定期定额投资美股或基金，按月累计积分奖励","desc_th":"ตั้ง DCA หุ้นสหรัฐฯ หรือกองทุน รับ Bonus Points รายเดือน","date_zh":"1 ก.พ. – 31 ธ.ค. 2569","date_th":"1 ก.พ. – 31 ธ.ค. 2569","url":"https://www.innovestx.co.th/promotions/detail/DCA_Bonus_Points"},
    {"tag_zh":"邀友","tag_th":"ชวนเพื่อน","title_zh":"👥 Invite to Invest — 邀友奖励","title_th":"Invite to Invest ชวนเพื่อนลงทุน","desc_zh":"邀请好友开户并完成首笔投资，双方均获积分奖励","desc_th":"ชวนเพื่อนเปิดบัญชีและลงทุนครั้งแรก รับ INVX Points ทั้งคู่","date_zh":"1 ม.ค. – 30 ก.ย. 2569","date_th":"1 ม.ค. – 30 ก.ย. 2569","url":"https://www.innovestx.co.th/promotions/detail/invite-to-invest"},
    {"tag_zh":"基金","tag_th":"กองทุน","title_zh":"🎯 Best Deal 2026 — 优质基金免申购费","title_th":"Best Deal 2026 กองทุนฟรีค่าธรรมเนียมแรกเข้า","desc_zh":"精选优质基金全年免前端申购手续费","desc_th":"กองทุนคุณภาพ ฟรีค่าธรรมเนียมแรกเข้า ตลอดปี 2569","date_zh":"1 ก.พ. – 31 ธ.ค. 2569","date_th":"1 ก.พ. – 31 ธ.ค. 2569","url":"https://www.innovestx.co.th/promotions/detail/Best_Deal_2026"},
]

WEBULL_DEFAULT = [
    {"badge":"HOT","tag_zh":"新客户","tag_th":"ลูกค้าใหม่","title_zh":"🎉 新户欢迎礼 — รางวัลต้อนรับ","title_th":"รางวัลต้อนรับ เปิดบัญชีรับสูงสุด 10+50 ไม้ฟรี","desc_zh":"开户并入金，最多获赠 10+50 笔免佣金交易次数","desc_th":"เปิดบัญชีและฝากเงิน รับสิทธิ์เทรดฟรีสูงสุด 60 รายการ","date_zh":"31 พ.ค. – 30 ก.ย. 2569","date_th":"31 พ.ค. – 30 ก.ย. 2569","url":"https://www.webull.co.th/activity"},
    {"tag_zh":"邀友","tag_th":"ชวนเพื่อน","title_zh":"👥 邀友最多送 60 笔免佣金交易","title_th":"ชวนเพื่อน รับสูงสุด 60 ไม้เทรดฟรี","desc_zh":"每成功邀请 1 位好友存款，双方获免佣金交易次数，上限 60 笔","desc_th":"ชวนเพื่อนฝากเงิน รับไม้เทรดฟรีทั้งคู่ สูงสุด 60 รายการ","date_zh":"延长进行中","date_th":"ขยายเวลาแล้ว","url":"https://www.webull.co.th/news/14"},
    {"tag_zh":"手续费","tag_th":"ค่าคอมฯ","title_zh":"💸 美股 + 泰股手续费 7 折","title_th":"รับส่วนลด 30% ค่าคอมฯ หุ้นสหรัฐฯ + หุ้นไทย","desc_zh":"限时享受美股及泰股交易佣金折扣 30%","desc_th":"ลดค่าคอมมิชชัน 30% ทั้งหุ้นอเมริกาและหุ้นไทย","date_zh":"进行中","date_th":"กำลังดำเนินการ","url":"https://www.webull.co.th/en/activity/commission-discount"},
    {"tag_zh":"竞赛","tag_th":"การแข่งขัน","title_zh":"📊 模拟交易周赛","title_th":"Paper Trading Contest ประจำสัปดาห์","desc_zh":"每周参与模拟盘竞赛，排名靠前赢取奖励","desc_th":"แข่ง Paper Trading ทุกสัปดาห์ อันดับดี รับรางวัล","date_zh":"每周举行","date_th":"ทุกสัปดาห์","url":"https://www.webull.co.th/activity"},
]

# ── 配色主题（明亮泰国风） ─────────────────────────────────────────────────
THEME = {
    "dime":   {"accent":"#00a846","tag_bg":"rgba(0,168,70,.12)","logo_bg":"#00a846","logo_fg":"#fff","border":"#b2dfcb","section_bg":"#f0faf4","header_bg":"linear-gradient(135deg,#e8f5e9,#c8e6c9)"},
    "invx":   {"accent":"#1a5dc8","tag_bg":"rgba(26,93,200,.10)","logo_bg":"#1a5dc8","logo_fg":"#fff","border":"#b3c8f0","section_bg":"#f0f5ff","header_bg":"linear-gradient(135deg,#e8eeff,#c5d6f7)"},
    "webull": {"accent":"#d4000a","tag_bg":"rgba(212,0,10,.10)","logo_bg":"#d4000a","logo_fg":"#fff","border":"#f5b3b6","section_bg":"#fff5f5","header_bg":"linear-gradient(135deg,#fff0f0,#fdd)"},
}

def escape(s):
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def make_card(broker, item):
    t = THEME[broker]
    badge = item.get("badge","")
    badge_html = ""
    if badge == "NEW":
        badge_html = '<span class="badge badge-new">NEW</span>'
    elif badge == "HOT":
        badge_html = '<span class="badge badge-hot">HOT</span>'

    tag_zh = escape(item.get("tag_zh",""))
    tag_th = escape(item.get("tag_th",""))

    title_zh = escape(item.get("title_zh", item.get("title_th","")))
    title_th = escape(item.get("title_th",""))
    desc_zh  = escape(item.get("desc_zh",""))
    desc_th  = escape(item.get("desc_th",""))
    date_zh  = escape(item.get("date_zh",""))
    date_th  = escape(item.get("date_th",""))
    url      = escape(item.get("url","#"))

    tag_block = ""
    if tag_zh:
        tag_block = f'<div class="tag" style="background:{t["tag_bg"]};color:{t["accent"]}">{tag_zh} · {tag_th}</div>'

    date_block = ""
    if date_zh:
        date_block = f'''<div class="card-date">
          <span>{date_zh}</span>
          <span class="th">{date_th}</span>
        </div>'''

    desc_block = ""
    if desc_zh:
        desc_block = f'''<div class="card-desc">
          <div>{desc_zh}</div>
          <div class="th">{desc_th}</div>
        </div>'''

    return f'''<a class="card" href="{url}" target="_blank"
        style="--accent:{t["accent"]};--card-hover:{t["tag_bg"]}">
      <div class="card-top">
        {badge_html}
        {tag_block}
      </div>
      <div class="card-title">
        <div>{title_zh}</div>
        <div class="th">{title_th}</div>
      </div>
      {desc_block}
      {date_block}
      <div class="card-arrow" style="color:{t["accent"]}">查看详情 / ดูรายละเอียด →</div>
    </a>'''

def make_section(broker, label_zh, label_th, site, site_url, all_url, items):
    t = THEME[broker]
    cards = "\n".join(make_card(broker, it) for it in items)
    css_vars = (f"--s-accent:{t['accent']};--s-border:{t['border']};"
                f"--s-hbg:{t['section_bg']};")
    return f'''<section class="broker-section" style="{css_vars}">
  <div class="section-header">
    <div class="logo" style="background:{t["logo_bg"]};color:{t["logo_fg"]}">{label_zh[:4]}</div>
    <div class="section-title">
      <div><span class="name-zh">{label_zh}</span><span class="name-th">{label_th}</span></div>
      <a href="{site_url}" target="_blank" class="site-link">{site} ↗</a>
    </div>
    <a href="{all_url}" target="_blank" class="all-btn">全部活动 ดูทั้งหมด →</a>
  </div>
  <div class="card-grid">{cards}</div>
</section>'''

# ── 构建完整 HTML ───────────────────────────────────────────────────────────
def build_html(dime_items, invx_items, webull_items):
    now_dt  = datetime.now(BKK)
    now_str = now_dt.strftime("%Y/%m/%d %H:%M")
    wk_zh, wk_th = week_label(now_dt)

    sec_dime   = make_section("dime",   "Dime!",        "ไดม์",        "dime.co.th",         "https://dime.co.th",                "https://dime.co.th/th/articles/all-dime-promo",     dime_items)
    sec_invx   = make_section("invx",   "InnovestX",    "อินโนเวสท์",  "innovestx.co.th",    "https://www.innovestx.co.th",       "https://www.innovestx.co.th/promotions",            invx_items)
    sec_webull = make_section("webull", "Webull TH",    "วีบูลล์",    "webull.co.th",       "https://www.webull.co.th",          "https://www.webull.co.th/activity",                 webull_items)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>🇹🇭 泰国券商促销汇总</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans Thai', sans-serif;
      background: #fdf6e3;
      background-image:
        radial-gradient(ellipse at 0% 0%, rgba(212,0,10,.06) 0%, transparent 50%),
        radial-gradient(ellipse at 100% 100%, rgba(249,168,37,.10) 0%, transparent 50%);
      color: #1a1a2e;
      min-height: 100vh;
      padding: 28px 16px 60px;
    }}

    /* ── page header ── */
    .page-header {{
      max-width: 960px;
      margin: 0 auto 32px;
      background: linear-gradient(135deg,#b71c1c 0%,#e53935 40%,#f9a825 100%);
      border-radius: 20px;
      padding: 24px 28px;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      box-shadow: 0 4px 24px rgba(183,28,28,.25);
      position: relative;
      overflow: hidden;
    }}
    .page-header::before {{
      content: "🏛️";
      position: absolute;
      right: 20px; top: 50%;
      transform: translateY(-50%);
      font-size: 5rem;
      opacity: .12;
      pointer-events: none;
    }}
    .page-header h1 {{
      font-size: 1.7rem;
      font-weight: 900;
      color: #fff;
      line-height: 1.2;
      text-shadow: 0 1px 4px rgba(0,0,0,.25);
    }}
    .page-header h1 .th {{
      display: block;
      font-size: 1.05rem;
      font-weight: 600;
      color: rgba(255,255,255,.82);
      margin-top: 3px;
    }}
    .header-right {{
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
      position: relative;
      z-index: 1;
    }}
    .week-badge {{
      background: rgba(255,255,255,.95);
      color: #b71c1c;
      border-radius: 20px;
      padding: 6px 18px;
      font-size: .84rem;
      font-weight: 800;
      box-shadow: 0 2px 10px rgba(0,0,0,.18);
      white-space: nowrap;
    }}
    .week-badge .wk-th {{
      font-size: .72rem;
      font-weight: 600;
      color: #c62828;
      opacity: .85;
    }}
    .updated {{
      font-size: .70rem;
      color: rgba(255,255,255,.65);
    }}

    /* ── section ── */
    .broker-section {{
      max-width: 960px;
      margin: 0 auto 28px;
      background: #fff;
      border-radius: 18px;
      overflow: hidden;
      box-shadow: 0 2px 16px rgba(0,0,0,.08);
      border: 1.5px solid var(--s-border);
    }}
    .section-header {{
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 16px 20px;
      border-bottom: 1.5px solid var(--s-border);
      background: var(--s-hbg);
    }}
    .logo {{
      width: 46px;
      height: 46px;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 900;
      font-size: .76rem;
      flex-shrink: 0;
      box-shadow: 0 2px 8px rgba(0,0,0,.18);
      letter-spacing: -.02em;
    }}
    .section-title {{ flex: 1; min-width: 0; }}
    .section-title .name-zh {{ font-size: 1.08rem; font-weight: 800; color: #1a1a2e; }}
    .section-title .name-th {{ font-size: .78rem; font-weight: 500; color: #888; margin-left: 6px; }}
    .site-link {{ font-size: .72rem; color: #aaa; text-decoration: none; margin-top: 1px; display: block; }}
    .site-link:hover {{ color: var(--s-accent); }}
    .all-btn {{
      padding: 8px 18px;
      border-radius: 20px;
      font-size: .76rem;
      font-weight: 800;
      text-decoration: none;
      white-space: nowrap;
      color: #fff;
      background: var(--s-accent);
      box-shadow: 0 2px 8px rgba(0,0,0,.15);
      flex-shrink: 0;
      transition: opacity .15s;
    }}
    .all-btn:hover {{ opacity: .85; }}

    /* ── grid ── */
    .card-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(268px, 1fr));
      gap: 1px;
      background: #f0f0f0;
    }}

    /* ── card ── */
    .card {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 18px;
      background: #fff;
      text-decoration: none;
      color: inherit;
      transition: background .15s, box-shadow .15s;
    }}
    .card:hover {{
      background: var(--card-hover);
      outline: 2px solid var(--accent);
      outline-offset: -2px;
    }}
    .card-top {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .badge {{
      font-size: .67rem;
      font-weight: 900;
      padding: 2px 8px;
      border-radius: 4px;
      letter-spacing: .08em;
    }}
    .badge-new {{ background: #e53935; color: #fff; }}
    .badge-hot {{ background: #f57c00; color: #fff; }}
    .tag {{
      display: inline-block;
      padding: 3px 11px;
      border-radius: 20px;
      font-size: .72rem;
      font-weight: 700;
    }}
    .card-title {{
      font-size: .93rem;
      font-weight: 800;
      color: #1a1a2e;
      line-height: 1.45;
    }}
    .card-title .th {{
      font-size: .82rem;
      font-weight: 500;
      color: #666;
      margin-top: 3px;
    }}
    .card-desc {{
      font-size: .78rem;
      color: #555;
      line-height: 1.55;
    }}
    .card-desc .th {{ color: #888; }}
    .card-date {{
      font-size: .70rem;
      color: #aaa;
      margin-top: auto;
      padding-top: 8px;
      border-top: 1px solid #f0f0f0;
    }}
    .card-date .th {{ color: #c0c0c0; }}
    .card-arrow {{
      font-size: .73rem;
      font-weight: 700;
      text-align: right;
      color: var(--accent);
    }}

    /* ── footer ── */
    .footer {{
      text-align: center;
      font-size: .72rem;
      color: #bbb;
      margin-top: 8px;
      max-width: 960px;
      margin-left: auto;
      margin-right: auto;
    }}

    @media (max-width: 600px) {{
      .page-header {{ border-radius: 14px; padding: 18px 16px; }}
      .page-header h1 {{ font-size: 1.35rem; }}
      .all-btn {{ display: none; }}
    }}
  </style>
</head>
<body>

<div class="page-header">
  <h1>
    🇹🇭 泰国券商促销汇总
    <span class="th">สรุปโปรโมชันโบรกเกอร์ไทย</span>
  </h1>
  <div class="header-right">
    <div class="week-badge">
      📅 {wk_zh} &nbsp;<span class="wk-th">{wk_th}</span>
    </div>
    <div class="updated">自动更新 · อัปเดตอัตโนมัติ · {now_str} BKK</div>
  </div>
</div>

{sec_dime}
{sec_invx}
{sec_webull}

<div class="footer">
  数据来源各平台官网 · ข้อมูลจากเว็บไซต์ทางการ · 活动条款以官网为准 · การลงทุนมีความเสี่ยง
</div>

</body>
</html>'''

# ── 主流程 ─────────────────────────────────────────────────────────────────
def main(no_deploy=False):
    log("=== 开始更新促销活动 ===")

    log("抓取 InnovestX...")
    invx_html  = fetch("https://www.innovestx.co.th/promotions")
    invx_items = parse_invx(invx_html)
    log(f"  InnovestX: {len(invx_items)} 条")

    log("抓取 Webull Thailand...")
    wb_html      = fetch("https://www.webull.co.th/activity")
    webull_items = parse_webull(wb_html)
    log(f"  Webull: {len(webull_items)} 条")

    log("Dime! 使用静态数据（页面 403）")
    dime_items = DIME_ITEMS

    html = build_html(dime_items, invx_items, webull_items)
    OUTPUT.write_text(html, encoding="utf-8")
    log(f"  HTML 写入 {OUTPUT}")

    # 部署到 Netlify
    if no_deploy:
        log("  --no-deploy 跳过 Netlify 部署")
        log("=== 完成 ===")
        print("促销页面已生成（未部署）")
        return

    DEPLOY_DIR.mkdir(exist_ok=True)
    deploy_html = DEPLOY_DIR / "index.html"
    deploy_html.write_text(html, encoding="utf-8")

    site_id = ""
    if SITE_ID_FILE.exists():
        site_id = SITE_ID_FILE.read_text().strip()

    cmd = [NETLIFY, "deploy", "--prod", "--dir", str(DEPLOY_DIR)]
    if site_id:
        cmd += ["--site", site_id]

    log(f"  Netlify 部署: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPT_DIR))
    if result.returncode == 0:
        log("  Netlify 部署成功")
        log(result.stdout[-500:] if result.stdout else "")
        # 提取 site id 缓存
        m = re.search(r'site id:\s*([a-f0-9\-]+)', result.stdout, re.I)
        if m and not site_id:
            SITE_ID_FILE.write_text(m.group(1))
    else:
        log(f"  Netlify 部署失败: {result.stderr[-300:]}")

    log("=== 完成 ===")
    print("促销页面已更新并部署")

if __name__ == "__main__":
    import sys
    no_deploy = "--no-deploy" in sys.argv
    main(no_deploy=no_deploy)
