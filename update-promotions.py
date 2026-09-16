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

# ── 关键词映射：泰语 → 中文标签 ────────────────────────────────────────────
TH_TAG_MAP = [
    (r'ลูกค้าใหม่|เปิดบัญชี|หน้าใหม่',        "新客户",  "ลูกค้าใหม่"),
    (r'ชวนเพื่อน|invite',                      "邀友",    "ชวนเพื่อน"),
    (r'กองทุน',                                "基金",    "กองทุน"),
    (r'ทองคำ|ทอง',                             "黄金",    "ทองคำ"),
    (r'หุ้นสหรัฐ|US|หุ้นอเมริก',              "美股",    "หุ้นสหรัฐฯ"),
    (r'หุ้นไทย',                               "泰股",    "หุ้นไทย"),
    (r'ออปชัน|option',                         "期权",    "ออปชัน"),
    (r'แลกเงิน|FX|เงินตรา|ไดม์เนิน',          "换汇",    "แลกเงิน"),
    (r'ประกัน|ไดม์ใจ',                         "保险",    "ประกัน"),
    (r'DCA|สะสม',                              "定投",    "DCA"),
    (r'Mid.month|midmonth',                    "免佣金日", "Mid-month"),
    (r'[Pp]ayday|เงินเดือน',                   "发薪日",  "Payday"),
    (r'คูปอง|coupon|birthday|วันเกิด',         "优惠券",  "คูปอง"),
    (r'Club|club',                             "会员",    "Club"),
]

def th_tag(text):
    for pattern, zh, th in TH_TAG_MAP:
        if re.search(pattern, text, re.I):
            return zh, th
    return "活动", "กิจกรรม"

# ── 解析各平台 ──────────────────────────────────────────────────────────────
def parse_dime():
    """用手机 UA 抓取 dime.co.th 活动汇总页，解析 Next.js JSON 内嵌数据"""
    url = "https://dime.co.th/th/articles/all-dime-promo"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                      "Mobile/15E148 Safari/604.1",
        "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
        "Referer": "https://dime.co.th/",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  WARN Dime! fetch failed: {e}")
        return DIME_ITEMS

    # 提取 Next.js 内嵌 JSON
    m = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        log("  WARN Dime! JSON not found, using static data")
        return DIME_ITEMS

    try:
        import json
        d = json.loads(m.group(1))
        content = d["props"]["pageProps"]["articleData"]["content"]
    except Exception as e:
        log(f"  WARN Dime! JSON parse failed: {e}")
        return DIME_ITEMS

    # 提取所有活动链接（คลิก! 锚点）
    links = re.findall(r'href="(https://dime\.co\.th/[^"]+)"', content)
    links = list(dict.fromkeys(links))  # 去重保序

    # 提取活动标题（表格第一列 <td> 里的文字）
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', content, re.S)
    cards = []
    link_idx = 0
    seen_titles = set()

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
        if len(cells) < 1:
            continue
        title_raw = re.sub(r'<[^>]+>', ' ', cells[0])
        title_raw = re.sub(r'&nbsp;', ' ', title_raw)
        title_th  = re.sub(r'\s+', ' ', title_raw).strip()
        if len(title_th) < 5 or title_th in seen_titles:
            continue
        seen_titles.add(title_th)

        # 取对应链接
        url_card = links[link_idx] if link_idx < len(links) else "https://dime.co.th/th/articles/all-dime-promo"
        link_idx += 1

        # 取日期（第二列包含日期关键词的文字）
        date_th = ""
        if len(cells) >= 2:
            desc_raw = re.sub(r'<[^>]+>', ' ', cells[1])
            dm = re.search(r'(\d+\s*[ก-๙\.]+.*?(?:69|70|71|2569|2570|2571))', desc_raw)
            if dm:
                date_th = re.sub(r'\s+', ' ', dm.group(1)).strip()

        tag_zh, tag_th = th_tag(title_th + " " + url_card)
        cards.append({
            "tag_zh": tag_zh, "tag_th": tag_th,
            "title_th": title_th,
            "date_th": date_th,
            "url": url_card,
        })
        if len(cards) >= 10:
            break

    if not cards:
        log("  WARN Dime! parsed 0 cards, using static data")
        return DIME_ITEMS

    log(f"  Dime!: {len(cards)} 条（实时抓取）")
    return cards

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
        "tag_zh": "新客户", "tag_th": "ลูกค้าใหม่",
        "title_zh": "🎁 新客户开户礼包「ชุดเจิมพอร์ต」",
        "title_th": "ชาว Dime! หน้าใหม่ รับ 'ชุดเจิมพอร์ต' ฟรี",
        "desc_zh": "开户即享免佣金交易 10 次",
        "desc_th": "เปิดบัญชีลงทุน รับสิทธิ์เทรดฟรี ไม่มีค่าคอมมิชชัน 10 ไม้",
        "date_zh": "1 ม.ค. – 31 ธ.ค. 69", "date_th": "1 ม.ค. – 31 ธ.ค. 69",
        "url": "https://dime.co.th/th/articles/starter-pack-2026",
    },
    {
        "tag_zh": "邀友", "tag_th": "ชวนเพื่อน",
        "title_zh": "👥 邀友富贵 — ชวนเพื่อน รวยยกแก๊ง",
        "title_th": "ชวนเพื่อน รับ Dime! Lucky สูงสุด ฿1,000",
        "desc_zh": "每邀 1 位好友开户送美股 Lucky ฿50，最高 ฿1,000",
        "desc_th": "รับหุ้นสหรัฐฯ มูลค่า ฿50 ต่อเพื่อน 1 คน สูงสุด ฿1,000",
        "date_zh": "1 เม.ย. – 31 ธ.ค. 69", "date_th": "1 เม.ย. – 31 ธ.ค. 69",
        "url": "https://dime.co.th/th/articles/invite-and-get-rich",
    },
    {
        "tag_zh": "换汇", "tag_th": "แลกเงิน",
        "title_zh": "💱 换汇满 1 万返现 ฿50",
        "title_th": "ตั้งแลก 1 หมื่น รับเงินคืน 50 บาท",
        "desc_zh": "通过 Preferred Rate 换汇满 ฿10,000 返现 ฿50",
        "desc_th": "ตั้งแลกเงินผ่านฟีเจอร์ Preferred Rate ตั้งแต่ ฿10,000 รับเงินคืน ฿50",
        "date_zh": "1 – 30 ก.ย. 69", "date_th": "1 – 30 ก.ย. 69",
        "url": "https://dime.co.th/th/articles/fx-preferred-rate-50",
    },
    {
        "tag_zh": "基金", "tag_th": "กองทุนรวม",
        "title_zh": "📦 买基金送美股 — ซื้อ 1 ได้ถึง 2",
        "title_th": "ภารกิจสะสมกองทุน พร้อมรับหุ้นสหรัฐฯ ฟรี",
        "desc_zh": "单月累计买基金达目标金额，赠美股 Lucky",
        "desc_th": "สะสมกองทุนรวมวันนี้ รับฟรีหุ้นสหรัฐฯ",
        "date_zh": "1 ก.ค. – 30 ก.ย. 69", "date_th": "1 ก.ค. – 30 ก.ย. 69",
        "url": "https://dime.co.th/en/articles/fundmission-buy1get2-july-2026",
    },
    {
        "badge": "HOT",
        "tag_zh": "黄金", "tag_th": "ทองคำ",
        "title_zh": "🏅 买金排行赢实物黄金 — บัลลังก์ทองคำ",
        "title_th": "เทรดทองติดอันดับ รับทองคำแท่ง YLG",
        "desc_zh": "9 月买金量前 4 名，最高赢黄金 1 บาท（约 15.2g）",
        "desc_th": "Top 4 ยอดซื้อทอง ก.ย. รับทองแท่ง 96.5% รวม 1 บาท 2 สลึง",
        "date_zh": "1 – 30 ก.ย. 69", "date_th": "1 – 30 ก.ย. 69",
        "url": "https://dime.co.th/th/articles/game-of-the-golden-throne-ylg-sep-2026",
    },
    {
        "badge": "NEW",
        "tag_zh": "周年庆", "tag_th": "วันเกิด Dime!",
        "title_zh": "🎂 Dime! 生日优惠券 — สูงสุด 60%",
        "title_th": "Dime! Birthday Coupon แจกใหญ่จัดเต็ม สูงสุด 60%",
        "desc_zh": "领取最多 60 张佣金折扣券，最高 60% 折扣",
        "desc_th": "กดรับคูปองส่วนลดค่าคอมฯ สูงสุด 60% ได้สูงสุด 60 รายการ",
        "date_zh": "เก็บ 21 ก.ย. / ใช้ถึง 26 ก.ย. 69", "date_th": "เปิดเก็บ 21 ก.ย. 12:00 น. / ใช้ถึง 26 ก.ย. 06:00 น.",
        "url": "https://dime.co.th/th/articles/dime-coupon-sep-2026",
    },
    {
        "tag_zh": "免佣金", "tag_th": "ฟรีค่าคอม",
        "title_zh": "⚡ Mid-month 免佣金日（每月 15 日）",
        "title_th": "Mid-month ซื้อหุ้นฟรีค่าคอม ไม่จำกัดรายการ",
        "desc_zh": "每月 15 日买泰股/美股不限次数免手续费",
        "desc_th": "วันที่ 15 ทุกเดือน ซื้อหุ้นไทยและสหรัฐฯ ฟรีค่าคอมฯ ไม่จำกัด",
        "date_zh": "ทุกวันที่ 15 ของเดือน", "date_th": "ทุกวันที่ 15 ของเดือน",
        "url": "https://dime.co.th/th/articles/mid-month-sep-2026",
    },
    {
        "tag_zh": "发薪日", "tag_th": "Payday",
        "title_zh": "💸 Payday 发薪日免佣金",
        "title_th": "ฉลอง Payday ช้อปหุ้นฟิน ๆ แบบฟรีค่าคอมฯ",
        "desc_zh": "发薪日当天买泰股/美股全免佣金，不限单数",
        "desc_th": "วันเงินเดือนออก ซื้อหุ้นได้ไม่อั้น ไม่จำกัดรายการ ฟรีค่าคอม",
        "date_zh": "ทุกวันเงินเดือนออก", "date_th": "ทุกวันเงินเดือนออก",
        "url": "https://dime.co.th/th/articles/payday-sep-2026",
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

# ── 竞品分析数据 ────────────────────────────────────────────────────────────
ANALYSIS = {
    "dime": {
        "highlights": [
            ("每月两次免佣金日", "Mid-month（15日）+ Payday 双免佣，泰国券商中频率最高"),
            ("实物黄金排行奖", "买金量前4名赢 YLG 黄金实物，稀缺感强，吸引高净值用户"),
            ("生日优惠券力度大", "最多60张60折券，短期内大幅降低交易成本"),
        ],
        "lowlights": [
            ("Lucky股票有使用限制", "赠送的美股有锁定期和交易限制，不能立即变现"),
            ("换汇活动受众窄", "仅限有外币换汇需求用户，普通股票投资者感知弱"),
        ],
        "insight": "Dime! 促销策略以「高频低门槛」为主，Mid-month + Payday 双免佣制造持续回访动机，适合活跃交易用户。黄金排行赛则是差异化亮点，直接对标高净值客群。",
        "insight_th": "กลยุทธ์ Dime! เน้น 'บ่อยครั้ง ต้นทุนต่ำ' สร้างแรงจูงใจกลับมาใช้งานต่อเนื่อง",
    },
    "invx": {
        "highlights": [
            ("活动种类最多最全", "覆盖美股/泰股/TFEX/基金/加密/DCA，满足多类型投资者"),
            ("全年免基金申购费", "Best Deal 2026 全年有效，长期价值最高，无需追热点"),
            ("BTC返佣50%", "加密资产返佣力度在泰国券商中领先，吸引crypto用户"),
        ],
        "lowlights": [
            ("多数活动9月底到期", "大量活动截止 30 ก.ย.，届时吸引力大幅下降，续约不确定"),
            ("奖励以积分为主", "INVX Points 兑换有门槛，不如现金/免佣直接，感知价值打折"),
        ],
        "insight": "InnovestX 是三家中活动覆盖面最广的，尤其对多元资产投资者友好。但大量活动集中9月底到期，10月促销能否续力是关键观察点。",
        "insight_th": "InnovestX มีแคมเปญหลากหลายที่สุด แต่หลายรายการหมดอายุ 30 ก.ย. ต้องติดตามว่าจะต่อหรือไม่",
    },
    "webull": {
        "highlights": [
            ("新户最多60笔免佣，门槛低", "10+50笔分层赠送，规则简单易懂，转化率高"),
            ("佣金直接打7折", "不需要积分兑换，折扣直接反映在每笔交易，用户感知最强"),
        ],
        "lowlights": [
            ("活动种类最少", "仅4条，覆盖面窄，对老客户缺乏持续激励"),
            ("新户欢迎礼9月底到期", "主力促销即将到期，若不续期，拉新竞争力将明显减弱"),
            ("缺乏长期留存机制", "没有积分体系或等级制度，用户黏性主要靠佣金折扣维持"),
        ],
        "insight": "Webull TH 策略简单粗暴但有效——直接折扣和免佣对新户转化效率高。但缺乏中长期留存工具，一旦欢迎礼到期，与其他平台的竞争优势会减弱。",
        "insight_th": "Webull TH เน้นดึงลูกค้าใหม่ด้วยส่วนลดตรงๆ แต่ขาดกลไกรักษาลูกค้าระยะยาว",
    },
}

def make_analysis_section():
    brokers = [
        ("dime",   "Dime!",     "ไดม์",       "#00a846", "#f0faf4", "#b2dfcb"),
        ("invx",   "InnovestX", "อินโนเวสท์", "#1a5dc8", "#f0f5ff", "#b3c8f0"),
        ("webull", "Webull TH", "วีบูลล์",    "#d4000a", "#fff5f5", "#f5b3b6"),
    ]
    cards = ""
    for key, name_zh, name_th, accent, bg, border in brokers:
        a = ANALYSIS[key]
        hi_items = "".join(
            f'<li><strong>{h}</strong> — {d}</li>' for h, d in a["highlights"]
        )
        lo_items = "".join(
            f'<li><strong>{h}</strong> — {d}</li>' for h, d in a["lowlights"]
        )
        cards += f'''
<div class="ana-card" style="--a-accent:{accent};--a-bg:{bg};--a-border:{border}">
  <div class="ana-header">
    <span class="ana-logo" style="background:{accent}">{name_zh[:4]}</span>
    <div>
      <div class="ana-name">{name_zh}</div>
      <div class="ana-name-th">{name_th}</div>
    </div>
  </div>
  <div class="ana-cols">
    <div class="ana-col hi">
      <div class="ana-col-label">✅ Highlights</div>
      <ul>{hi_items}</ul>
    </div>
    <div class="ana-col lo">
      <div class="ana-col-label">⚠️ Lowlights</div>
      <ul>{lo_items}</ul>
    </div>
  </div>
  <div class="ana-insight">
    💡 {a["insight"]}
    <div class="ana-insight-th">{a["insight_th"]}</div>
  </div>
</div>'''

    return f'''<section class="analysis-section">
  <div class="analysis-title">
    📊 竞品促销分析
    <span class="analysis-title-th">วิเคราะห์โปรโมชันคู่แข่ง moomoo</span>
  </div>
  <div class="ana-grid">{cards}</div>
</section>'''

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

    /* ── analysis section ── */
    .analysis-section {{
      max-width: 960px;
      margin: 0 auto 28px;
    }}
    .analysis-title {{
      font-size: 1.2rem;
      font-weight: 900;
      color: #1a1a2e;
      margin-bottom: 16px;
      padding-bottom: 10px;
      border-bottom: 2px solid #e8e0d0;
    }}
    .analysis-title-th {{
      font-size: .82rem;
      font-weight: 500;
      color: #999;
      margin-left: 8px;
    }}
    .ana-grid {{
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .ana-card {{
      background: var(--a-bg);
      border: 1.5px solid var(--a-border);
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 2px 10px rgba(0,0,0,.06);
    }}
    .ana-header {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .ana-logo {{
      width: 40px; height: 40px;
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-weight: 900; font-size: .74rem; color: #fff;
      flex-shrink: 0;
    }}
    .ana-name {{ font-size: 1rem; font-weight: 800; color: #1a1a2e; }}
    .ana-name-th {{ font-size: .75rem; color: #999; }}
    .ana-cols {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .ana-col {{ background: #fff; border-radius: 10px; padding: 12px 14px; }}
    .ana-col.hi {{ border-left: 3px solid #43a047; }}
    .ana-col.lo {{ border-left: 3px solid #e53935; }}
    .ana-col-label {{
      font-size: .72rem;
      font-weight: 800;
      margin-bottom: 8px;
      color: #444;
      letter-spacing: .04em;
    }}
    .ana-col ul {{
      list-style: none;
      padding: 0; margin: 0;
      display: flex; flex-direction: column; gap: 6px;
    }}
    .ana-col li {{
      font-size: .76rem;
      color: #444;
      line-height: 1.5;
    }}
    .ana-col li strong {{ color: #1a1a2e; }}
    .ana-insight {{
      background: rgba(255,255,255,.7);
      border-radius: 8px;
      padding: 10px 14px;
      font-size: .78rem;
      color: #444;
      line-height: 1.6;
      border-left: 3px solid var(--a-accent);
    }}
    .ana-insight-th {{
      font-size: .71rem;
      color: #888;
      margin-top: 4px;
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
      .ana-cols {{ grid-template-columns: 1fr; }}
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

{make_analysis_section()}

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

    log("抓取 Dime!...")
    dime_items = parse_dime()

    html = build_html(dime_items, invx_items, webull_items)
    OUTPUT.write_text(html, encoding="utf-8")
    log(f"  HTML 写入 {OUTPUT}")

    # 始终生成部署目录
    DEPLOY_DIR.mkdir(exist_ok=True)
    deploy_html = DEPLOY_DIR / "index.html"
    deploy_html.write_text(html, encoding="utf-8")

    if no_deploy:
        log("  --no-deploy 跳过 Netlify 部署")
        log("=== 完成 ===")
        print("促销页面已生成（未部署）")
        return

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
