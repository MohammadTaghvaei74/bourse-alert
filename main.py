import os
import time
import datetime
import pytz
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- تنظیمات تلگرام ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@your_channel_id")
PROXY_URL = os.getenv("TELEGRAM_PROXY", "")  # مثال در صورت نیاز: socks5h://127.0.0.1:1080

# --- لیست نمادهای ثابت ---
LEVERAGED_FUNDS = ["موج", "اهرم", "دوایکس", "نارنج اهرم", "جهش", "شتاب", "بیدار"]
LEADERS = [
    "اهرم", "فولاد", "تاپیکو", "فملی", "شستا", "ذوب", "شبریز", "شتران", 
    "وغدیر", "شپنا", "شبندر", "پالایش", "خگستر", "فارس", "خودرو", 
    "وبصادر", "خساپا", "وتجارت", "وبملت", "پارسان", "دارا یکم"
]

# حافظه برای نگهداری نمرات ۱۰ دقیقه قبل (نماد -> نمره قبلی)
previous_scores = {}

def get_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    })
    return s

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    try:
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        resp = requests.post(url, json=payload, proxies=proxies, timeout=15)
        if resp.status_code != 200:
            logging.error(f"Telegram Error ({resp.status_code}): {resp.text}")
    except Exception as e:
        logging.error(f"Error sending telegram message: {e}")

def fetch_market_data():
    """دریافت دیتای زنده تابلو بورس از TSETMC"""
    url = "http://old.tsetmc.com/tsev2/data/MarketWatchPlus.aspx"
    session = get_session()
    try:
        res = session.get(url, timeout=12)
        if res.status_code != 200 or not res.text:
            return None, None
        parts = res.text.split("@")
        stocks_raw = parts[2].split(";") if len(parts) > 2 else []
        depth_raw = parts[3].split(";") if len(parts) > 3 else []
        return stocks_raw, depth_raw
    except Exception as e:
        logging.error(f"Failed to fetch market data: {e}")
        return None, None

def parse_market_data(stocks_raw, depth_raw):
    # پردازش اردر بوک (صف‌های خرید و فروش)
    queues = {}
    for d in depth_raw:
        item = d.split(",")
        if len(item) >= 8:
            ins_id = item[0]
            buy_vol = float(item[4]) if item[4] else 0.0
            buy_price = float(item[5]) if item[5] else 0.0
            sell_price = float(item[6]) if item[6] else 0.0
            sell_vol = float(item[7]) if item[7] else 0.0
            
            if ins_id not in queues:
                queues[ins_id] = {
                    "best_buy_vol": buy_vol, "best_buy_price": buy_price,
                    "best_sell_price": sell_price, "best_sell_vol": sell_vol
                }

    stocks = []
    for s in stocks_raw:
        fields = s.split(",")
        if len(fields) < 23:
            continue
        try:
            ins_id = fields[0]
            symbol = fields[2].replace("ي", "ی").replace("ك", "ک").strip()
            yesterday_price = float(fields[5]) if fields[5] else 0.0
            last_price = float(fields[7]) if fields[7] else 0.0
            trade_count = float(fields[8]) if fields[8] else 0.0
            trade_vol = float(fields[9]) if fields[9] else 0.0
            trade_val = float(fields[10]) if fields[10] else 0.0
            min_allowed = float(fields[19]) if fields[19] else 0.0
            max_allowed = float(fields[20]) if fields[20] else 0.0
            sector_code = fields[18] if len(fields) > 18 else "سایر"

            if yesterday_price <= 0:
                continue

            last_pct = round(((last_price - yesterday_price) / yesterday_price) * 100, 2)
            depth = queues.get(ins_id, {})
            best_buy_p = depth.get("best_buy_price", 0)
            best_buy_v = depth.get("best_buy_vol", 0)
            best_sell_p = depth.get("best_sell_price", 0)
            best_sell_v = depth.get("best_sell_vol", 0)

            # محاسبه نمره بر اساس صف خرید یا صف فروش
            score = last_pct
            queue_factor = 0.0

            # مخرج برای جلوگیری از صفر شدن در معاملات اندک
            effective_vol = max(trade_vol, 50000.0)

            if max_allowed > 0 and best_buy_p >= max_allowed and best_buy_v > 0:
                queue_factor = min(best_buy_v / effective_vol, 15.0)  # سقف برای جلوگیری از اعداد پرت
                score += queue_factor
            elif min_allowed > 0 and best_sell_p <= min_allowed and best_sell_v > 0:
                queue_factor = min(best_sell_v / effective_vol, 15.0)
                score -= queue_factor

            stocks.append({
                "symbol": symbol,
                "score": round(score, 2),
                "last_pct": last_pct,
                "trade_val": trade_val,
                "sector": sector_code,
            })
        except Exception:
            continue

    return stocks

def format_delta(current, prev):
    if prev is None:
        return "(0.00)"
    delta = current - prev
    sign = "+" if delta > 0 else ""
    return f"({sign}{delta:.2f})"

def run_pipeline():
    global previous_scores
    stocks_raw, depth_raw = fetch_market_data()
    if not stocks_raw:
        logging.warning("No data parsed from TSETMC.")
        return

    data = parse_market_data(stocks_raw, depth_raw)
    stock_dict = {s["symbol"]: s for s in data}

    # 1. گزارش صندوق‌های اهرمی
    leveraged_list = [stock_dict[sym] for sym in LEVERAGED_FUNDS if sym in stock_dict]
    leveraged_list.sort(key=lambda x: x["score"], reverse=True)
    msg_lev = "<b>#اهرمی - رتبه‌بندی صندوق‌های اهرمی</b>\n\n"
    for idx, s in enumerate(leveraged_list, 1):
        delta_str = format_delta(s['score'], previous_scores.get(s['symbol']))
        msg_lev += f"{idx}. {s['symbol']} | امتیاز: <b>{s['score']}</b> {delta_str}\n"

    # 2. گزارش لیدرها
    leaders_list = [stock_dict[sym] for sym in LEADERS if sym in stock_dict]
    leaders_list.sort(key=lambda x: x["score"], reverse=True)
    msg_ldr = "<b>#لیدر - رتبه‌بندی سهام لیدر</b>\n\n"
    for idx, s in enumerate(leaders_list, 1):
        delta_str = format_delta(s['score'], previous_scores.get(s['symbol']))
        msg_ldr += f"{idx}. {s['symbol']} | امتیاز: <b>{s['score']}</b> {delta_str}\n"

    # 3. محاسبه وضعیت صنایع و رتبه صنایع
    sectors = {}
    for s in data:
        sec = s["sector"]
        if sec not in sectors:
            sectors[sec] = []
        sectors[sec].append(s["score"])

    sector_stats = []
    for sec, scores in sectors.items():
        if len(scores) >= 3:  # حداقل ۳ نماد برای معنادار بودن صنعت
            avg_score = sum(scores) / len(scores)
            sector_stats.append({
                "sector": sec,
                "avg_score": round(avg_score, 2),
                "count": len(scores)
            })

    sector_stats.sort(key=lambda x: x["avg_score"], reverse=True)
    sector_rank_map = {item["sector"]: rank for rank, item in enumerate(sector_stats, 1)}

    msg_sec = "<b>#وضعیت_صنایع - ۵ صنعت برتر بازار</b>\n\n"
    for idx, sec in enumerate(sector_stats[:5], 1):
        msg_sec += f"{idx}. صنعت {sec['sector']}: میانگین نمره = <b>{sec['avg_score']}</b> (تعداد سهام: {sec['count']})\n"

    # 4. تفکیک سهام غیرلیدر واجد شرایط
    non_leaders = [
        s for s in data 
        if s["symbol"] not in LEADERS 
        and s["symbol"] not in LEVERAGED_FUNDS
        and s["trade_val"] >= 150_000_000_000  # فیلتر حداقل ۱۵ میلیارد تومان ارزش معامله
    ]
    non_leaders.sort(key=lambda x: x["score"], reverse=True)

    # گزارش سوپر (۳۰ نماد برتر)
    msg_super = "<b>#سوپر - ۳۰ سهم برتر بازار</b>\n\n"
    for idx, s in enumerate(non_leaders[:30], 1):
        sec_rank = sector_rank_map.get(s['sector'], "-")
        delta_str = format_delta(s['score'], previous_scores.get(s['symbol']))
        msg_super += f"{idx}. {s['symbol']} | امتیاز: <b>{s['score']}</b> {delta_str} (رتبه صنعت: {sec_rank})\n"

    # گزارش نیمه سوپر (رتبه ۳۱ تا ۶۰)
    msg_semi = "<b>#نیمه_سوپر - ۳۰ سهم دوم بازار</b>\n\n"
    for idx, s in enumerate(non_leaders[30:60], 31):
        sec_rank = sector_rank_map.get(s['sector'], "-")
        delta_str = format_delta(s['score'], previous_scores.get(s['symbol']))
        msg_semi += f"{idx}. {s['symbol']} | امتیاز: <b>{s['score']}</b> {delta_str} (رتبه صنعت: {sec_rank})\n"

    # ارسال به تلگرام
    for msg in [msg_lev, msg_ldr, msg_sec, msg_super, msg_semi]:
        send_telegram_message(msg)
        time.sleep(1.5)  # وقفه برای جلوگیری از Rate-Limit تلگرام

    # به‌روزرسانی حافظه نمرات
    for s in data:
        previous_scores[s["symbol"]] = s["score"]

def is_market_open():
    tehran_tz = pytz.timezone("Asia/Tehran")
    now = datetime.datetime.now(tehran_tz)
    # شنبه (5) تا چهارشنبه (2) در تقویم میلادی پایتون: دوشنبه 0، ..., شنبه 5، یکشنبه 6
    if now.weekday() in [3, 4]:  # پنج‌شنبه (3) و جمعه (4)
        return False
    market_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_end = now.replace(hour=12, minute=30, second=0, microsecond=0)
    return market_start <= now <= market_end

def main():
    logging.info("Bourse Alert Bot started successfully.")
    while True:
        try:
            if is_market_open():
                logging.info("Market is OPEN. Running analysis...")
                run_pipeline()
                time.sleep(600)  # وقفه ۱۰ دقیقه‌ای
            else:
                logging.info("Market is CLOSED. Sleeping for 60 seconds...")
                time.sleep(60)
        except Exception as e:
            logging.error(f"Unexpected error in main loop: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
