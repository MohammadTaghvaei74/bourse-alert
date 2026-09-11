import os, sys, time, logging, sqlite3, requests
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DB_PATH = "/root/bourse-alert/scores_history.db"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8682677437:AAFYCBWrpyHUMb6Dixhh9DdMUwUZemYLplc")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004419199993")

LEVERAGED_FUNDS = ["اهرم", "شتاب", "موج", "جهش", "توان", "نارنج", "بیدار", "عیار"]
LEADERS = ["ذوب", "اهرم", "فملی", "فولاد", "تاپیکو", "شستا", "شبریز", "شتران", "وغدیر", "شپنا", "شبندر", "پالایش", "خگستر", "فارس", "خودرو", "وبصادر", "وبملت", "خساپا", "دارا یکم", "پارسان", "وتجارت"]

def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS history (symbol TEXT, score REAL, timestamp DATETIME)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sym_time ON history(symbol, timestamp)")
    except Exception as e:
        logging.error(f"DB init error: {e}")

def save_scores(stocks):
    try:
        now = datetime.now()
        with sqlite3.connect(DB_PATH) as conn:
            data = [(s["symbol"], s["score"], now) for s in stocks]
            conn.executemany("INSERT INTO history VALUES (?, ?, ?)", data)
            week_ago = now - timedelta(days=7)
            conn.execute("DELETE FROM history WHERE timestamp < ?", (week_ago,))
    except Exception as e:
        logging.error(f"DB save error: {e}")

def get_symbol_stats(symbol, current_score):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT score FROM history WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1 OFFSET 1", (symbol,))
            last_row = cursor.fetchone()
            d_last = (current_score - last_row[0]) if last_row else 0.0

            cursor.execute("SELECT DISTINCT date(timestamp) FROM history WHERE symbol = ? ORDER BY date(timestamp) DESC", (symbol,))
            dates = [r[0] for r in cursor.fetchall()]

            d_prev_day = 0.0
            if len(dates) >= 2:
                cursor.execute("SELECT AVG(score) FROM history WHERE symbol = ? AND date(timestamp) = ?", (symbol, dates[1]))
                row = cursor.fetchone()
                if row and row[0] is not None:
                    d_prev_day = current_score - row[0]

            d_5d = 0.0
            past_dates = dates[1:6]
            if past_dates:
                ph = ",".join(["?"] * len(past_dates))
                cursor.execute(f"SELECT AVG(score) FROM history WHERE symbol = ? AND date(timestamp) IN ({ph})", [symbol] + past_dates)
                row = cursor.fetchone()
                if row and row[0] is not None:
                    d_5d = current_score - row[0]

            fmt = lambda val: f"{val:+.1f}" if val != 0 else "0.0"
            return f"(گ قبل: {fmt(d_last)} | دیروز: {fmt(d_prev_day)} | ۵‌روزه: {fmt(d_5d)})"
    except Exception as e:
        return "(گ قبل: 0.0 | دیروز: 0.0 | ۵‌روزه: 0.0)"

def get_leaders_overall_stats(leaders_list):
    if not leaders_list:
        return ""
    avg_score = sum(s["score"] for s in leaders_list) / len(leaders_list)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            symbols = [s["symbol"] for s in leaders_list]
            ph = ",".join(["?"] * len(symbols))
            cursor.execute(f"SELECT AVG(score) FROM (SELECT score, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) as rn FROM history WHERE symbol IN ({ph})) WHERE rn = 2", symbols)
            row = cursor.fetchone()
            d_last = (avg_score - row[0]) if (row and row[0] is not None) else 0.0

            cursor.execute(f"SELECT DISTINCT date(timestamp) FROM history WHERE symbol IN ({ph}) ORDER BY date(timestamp) DESC", symbols)
            dates = [r[0] for r in cursor.fetchall()]

            d_prev_day = 0.0
            if len(dates) >= 2:
                cursor.execute(f"SELECT AVG(score) FROM history WHERE symbol IN ({ph}) AND date(timestamp) = ?", symbols + [dates[1]])
                row = cursor.fetchone()
                if row and row[0] is not None:
                    d_prev_day = avg_score - row[0]

            d_5d = 0.0
            past_dates = dates[1:6]
            if past_dates:
                d_ph = ",".join(["?"] * len(past_dates))
                cursor.execute(f"SELECT AVG(score) FROM history WHERE symbol IN ({ph}) AND date(timestamp) IN ({d_ph})", symbols + past_dates)
                row = cursor.fetchone()
                if row and row[0] is not None:
                    d_5d = avg_score - row[0]

            fmt = lambda val: f"{val:+.1f}" if val != 0 else "0.0"
            return f"📊 <b>میانگین: {avg_score:.1f}</b> (گ قبل: {fmt(d_last)} | دیروز: {fmt(d_prev_day)} | ۵‌روزه: {fmt(d_5d)})\n\n"
    except Exception as e:
        return f"📊 <b>میانگین: {avg_score:.1f}</b>\n\n"

def is_derivative(symbol):
    prefixes = ["ض", "ط", "ص", "هـ", "سکه"]
    return any(symbol.startswith(p) for p in prefixes) and any(char.isdigit() for char in symbol)

def fetch_market_data():
    url = "http://old.tsetmc.com/tsev2/data/MarketWatchPlus.aspx"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    parts = resp.text.split("@")
    return (parts[2].split(";") if len(parts) > 2 else []), (parts[3].split(";") if len(parts) > 3 else [])

def parse_market_data(stocks_raw, depth_raw):
    quotes = {}
    for d in depth_raw:
        item = d.split(",")
        if len(item) >= 8:
            ins_id = item[0]
            try:
                b_price, s_price = float(item[4] or 0), float(item[5] or 0)
                b_vol, s_vol = float(item[6] or 0), float(item[7] or 0)
            except Exception:
                continue
            if ins_id not in quotes:
                quotes[ins_id] = []
            quotes[ins_id].append({"b_price": b_price, "s_price": s_price, "b_vol": b_vol, "s_vol": s_vol})

    stocks = []
    for s in stocks_raw:
        fields = s.split(",")
        if len(fields) < 23:
            continue
        try:
            ins_id = fields[0]
            symbol = fields[2].replace("ي", "ی").replace("ك", "ک").strip()
            yesterday_price = float(fields[13] or 0)
            close_price = float(fields[6] or 0)
            last_price = float(fields[7] or 0)
            trade_vol = float(fields[9] or 0)
            trade_val = float(fields[10] or 0)
            p19, p20 = float(fields[19] or 0), float(fields[20] or 0)
            max_allowed, min_allowed = max(p19, p20), min(p19, p20)
            sector_code = fields[18] if len(fields) > 18 else "سایر"

            if is_derivative(symbol) or yesterday_price <= 0:
                continue

            close_pct = round(((close_price - yesterday_price) / yesterday_price) * 100, 2)
            last_pct = round(((last_price - yesterday_price) / yesterday_price) * 100, 2)

            order_rows = quotes.get(ins_id, [])
            best_buy_p = max([r["b_price"] for r in order_rows], default=0.0)
            best_sell_p = min([r["s_price"] for r in order_rows if r["s_price"] > 0], default=0.0)

            total_buy_queue_vol = sum(r["b_vol"] for r in order_rows if max_allowed > 0 and r["b_price"] >= (max_allowed - 1))
            total_sell_queue_vol = sum(r["s_vol"] for r in order_rows if min_allowed > 0 and r["s_price"] <= (min_allowed + 1) and r["s_price"] > 0)
            effective_vol = max(trade_vol, 50000.0)

            is_buy_queue = (total_buy_queue_vol > 0) and (best_buy_p >= (max_allowed - 1))
            is_sell_queue = (total_sell_queue_vol > 0) and (best_sell_p <= (min_allowed + 1) and best_sell_p > 0)

            if is_buy_queue:
                score = last_pct + round(total_buy_queue_vol / effective_vol, 2)
            elif is_sell_queue:
                score = last_pct - round(total_sell_queue_vol / effective_vol, 2)
            else:
                score = last_pct

            stocks.append({"symbol": symbol, "score": round(score, 2), "last_pct": last_pct, "close_pct": close_pct, "trade_val": trade_val, "trade_vol": trade_vol, "sector": sector_code, "buy_queue_vol": total_buy_queue_vol, "sell_queue_vol": total_sell_queue_vol})
        except Exception:
            continue
    return stocks

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://tg-proxy.m-taghvaei74.workers.dev/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def run_pipeline():
    try:
        s_raw, d_raw = fetch_market_data()
        data = parse_market_data(s_raw, d_raw)
        if not data:
            return
        stock_dict = {s["symbol"]: s for s in data}

        leveraged_list = [stock_dict[sym] for sym in LEVERAGED_FUNDS if sym in stock_dict]
        leveraged_list.sort(key=lambda x: x["score"], reverse=True)
        msg_lev = "<b>#اهرمی - رتبه‌بندی صندوق‌های اهرمی</b>\n\n"
        for idx, s in enumerate(leveraged_list, 1):
            msg_lev += f"{idx}. {s['symbol']} | <b>{s['score']:.1f}</b> {get_symbol_stats(s['symbol'], s['score'])}\n"

        leaders_list = [stock_dict[sym] for sym in LEADERS if sym in stock_dict]
        leaders_list.sort(key=lambda x: x["score"], reverse=True)
        msg_ldr = "<b>#لیدر - رتبه‌بندی سهام لیدر</b>\n\n"
        msg_ldr += get_leaders_overall_stats(leaders_list)
        for idx, s in enumerate(leaders_list[:10], 1):
            msg_ldr += f"{idx}. {s['symbol']} | <b>{s['score']:.1f}</b> {get_symbol_stats(s['symbol'], s['score'])}\n"

        save_scores(data)
        send_telegram(msg_lev)
        send_telegram(msg_ldr)
        logging.info("Reports sent successfully.")
    except Exception as e:
        logging.error(f"Pipeline error: {e}")

def is_market_open():
    now = datetime.now()
    if now.weekday() in [3, 4]:
        return False
    start = now.replace(hour=8, minute=45, second=0, microsecond=0)
    end = now.replace(hour=12, minute=35, second=0, microsecond=0)
    return start <= now <= end

def main():
    init_db()
    logging.info("Bourse Bot started.")
    while True:
        try:
            if is_market_open():
                run_pipeline()
                time.sleep(300)
            else:
                logging.info("Market CLOSED. Sleeping 60s...")
                time.sleep(60)
        except Exception as e:
            logging.error(f"Loop error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
