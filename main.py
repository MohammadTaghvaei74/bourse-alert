import html
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

DB_PATH = os.getenv("DB_PATH", "/root/bourse-alert/scores_history.db")
TSETMC_URL = "https://old.tsetmc.com/tsev2/data/MarketWatchPlus.aspx"
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "https://tg-proxy.m-taghvaei74.workers.dev")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004419199993")
TEHRAN = ZoneInfo("Asia/Tehran")

LEVERAGED_FUNDS = ["اهرم", "شتاب", "موج", "جهش", "توان", "نارنج", "بیدار"]
LEADERS = ["ذوب", "اهرم", "فملی", "فولاد", "تاپیکو", "شستا", "شبریز", "شتران", "وغدیر", "شپنا", "شبندر", "پالایش", "خگستر", "فارس", "خودرو", "وبصادر", "وبملت", "خساپا", "دارا یکم", "پارسان", "وتجارت"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def calculate_score(last_pct, buy_queue_volume, sell_queue_volume, trade_volume):
    effective_volume = max(float(trade_volume), 50_000.0)
    return round(float(last_pct) + float(buy_queue_volume) / effective_volume - float(sell_queue_volume) / effective_volume, 2)


def queue_value_billion_toman(price, volume):
    # TSE prices and values are in rials; divide by 10 for tomans.
    return int(float(price) * float(volume) / 10 / 1_000_000_000)


def ltr_signed(value, decimals=1):
    return f"\u200e{value:+.{decimals}f}\u200e"


def format_report_line(index, stock):
    score = ltr_signed(stock["score"])
    value = int(stock.get("queue_value", 0))
    if value and stock.get("queue_side") == "buy":
        return f"{index}. {stock['symbol']} | {score} | \u200e+{value} B\u200e"
    if value and stock.get("queue_side") == "sell":
        return f"{index}. {stock['symbol']} | {score} | \u200e-{value} B\u200e"
    return f"{index}. {stock['symbol']} | {score}"


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS history (symbol TEXT NOT NULL, score REAL NOT NULL, timestamp TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_symbol_time ON history(symbol, timestamp)")


def save_scores(stocks, now):
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("INSERT INTO history(symbol, score, timestamp) VALUES (?, ?, ?)", [(s["symbol"], s["score"], now.isoformat()) for s in stocks])
        conn.execute("DELETE FROM history WHERE timestamp < ?", ((now - timedelta(days=7)).isoformat(),))


def _avg_for(symbols, start=None, end=None):
    if not symbols:
        return None
    marks = ",".join("?" for _ in symbols)
    clauses = [f"symbol IN ({marks})"]
    params = list(symbols)
    if start:
        clauses.append("timestamp >= ?"); params.append(start.isoformat())
    if end:
        clauses.append("timestamp < ?"); params.append(end.isoformat())
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(f"SELECT AVG(score) FROM history WHERE {' AND '.join(clauses)}", params).fetchone()
    return row[0] if row and row[0] is not None else None


def group_stats(stocks, now):
    init_db()
    symbols = [s["symbol"] for s in stocks]
    current = sum(s["score"] for s in stocks) / len(stocks) if stocks else 0.0
    previous = _avg_for(symbols, end=now)
    yesterday = _avg_for(symbols, start=now - timedelta(days=2), end=now - timedelta(days=1))
    five_day = _avg_for(symbols, start=now - timedelta(days=6), end=now - timedelta(days=1))
    return current, tuple(current - x if x is not None else 0.0 for x in (previous, yesterday, five_day))


def fmt_delta(value):
    return f"{value:+.1f}"


def _float(value):
    return float(value or 0)


def is_derivative(symbol):
    return any(symbol.startswith(p) for p in ("ض", "ط", "ص", "هـ", "سکه")) and any(c.isdigit() for c in symbol)


def fetch_market_data(session=None):
    session = session or requests.Session()
    response = session.get(TSETMC_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    parts = response.text.split("@")
    if len(parts) < 4:
        raise ValueError("unexpected TSETMC response format")
    return parts[2].split(";"), parts[3].split(";")


def parse_market_data(stocks_raw, depth_raw):
    quotes = {}
    for raw in depth_raw:
        fields = raw.split(",")
        if len(fields) < 8:
            continue
        try:
            row = {"buy_price": _float(fields[4]), "sell_price": _float(fields[5]), "buy_volume": _float(fields[6]), "sell_volume": _float(fields[7])}
            quotes.setdefault(fields[0], []).append(row)
        except (ValueError, IndexError):
            continue

    result = []
    for raw in stocks_raw:
        fields = raw.split(",")
        if len(fields) < 23:
            continue
        try:
            instrument = fields[0]
            symbol = fields[2].replace("ي", "ی").replace("ك", "ک").strip()
            yesterday = _float(fields[13]); close = _float(fields[6]); last = _float(fields[7])
            trade_volume = _float(fields[9]); max_price = _float(fields[19]); min_price = _float(fields[20])
            if not symbol or yesterday <= 0 or is_derivative(symbol):
                continue
            last_pct = round((last - yesterday) / yesterday * 100, 2)
            rows = quotes.get(instrument, [])
            buy_volume = sum(r["buy_volume"] for r in rows if r["buy_volume"] > 0 and max_price > 0 and r["buy_price"] >= max_price - 1)
            sell_volume = sum(r["sell_volume"] for r in rows if r["sell_volume"] > 0 and min_price > 0 and r["sell_price"] <= min_price + 1)
            best_buy_price = max((r["buy_price"] for r in rows), default=0)
            sell_prices = [r["sell_price"] for r in rows if r["sell_price"] > 0]
            best_sell_price = min(sell_prices, default=0)
            buy_queue = buy_volume > 0 and best_buy_price >= max_price - 1
            sell_queue = sell_volume > 0 and best_sell_price <= min_price + 1
            if buy_queue and not sell_queue:
                side, volume, price = "buy", buy_volume, best_buy_price
            elif sell_queue and not buy_queue:
                side, volume, price = "sell", sell_volume, best_sell_price
            else:
                side, volume, price = None, 0, 0
            result.append({"symbol": symbol, "score": calculate_score(last_pct, volume if side == "buy" else 0, volume if side == "sell" else 0, trade_volume), "queue_value": queue_value_billion_toman(price, volume) if side else 0, "queue_side": side})
        except (ValueError, IndexError, ZeroDivisionError):
            continue
    return result


def build_group_message(title, stocks, now, limit=None):
    shown = stocks[:limit] if limit else stocks
    average, deltas = group_stats(shown, now)
    lines = [f"<b>{html.escape(title)}</b>", f"میانگین {average:+.1f} | قبل {fmt_delta(deltas[0])} | دیروز {fmt_delta(deltas[1])} | ۵روزه {fmt_delta(deltas[2])}", ""]
    lines.extend(format_report_line(i, stock) for i, stock in enumerate(shown, 1))
    return "\n".join(lines)


def send_telegram(text, session=None):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"{TELEGRAM_PROXY.rstrip('/')}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = (session or requests).post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def run_pipeline(session=None, now=None):
    now = now or datetime.now(TEHRAN)
    stocks_raw, depth_raw = fetch_market_data(session)
    data = parse_market_data(stocks_raw, depth_raw)
    stock_dict = {s["symbol"]: s for s in data}
    leveraged = sorted((stock_dict[s] for s in LEVERAGED_FUNDS if s in stock_dict), key=lambda s: s["score"], reverse=True)
    leaders = sorted((stock_dict[s] for s in LEADERS if s in stock_dict), key=lambda s: s["score"], reverse=True)
    if not leveraged and not leaders:
        logging.warning("No configured symbols found in TSETMC data")
        return 0
    init_db()
    save_scores(data, now)
    send_telegram(build_group_message("#اهرمی", leveraged, now), session)
    send_telegram(build_group_message("#لیدر", leaders, now, limit=10), session)
    return len(data)


def is_market_open(now=None):
    now = now or datetime.now(TEHRAN)
    return now.weekday() not in (3, 4) and now.replace(hour=8, minute=45, second=0, microsecond=0) <= now <= now.replace(hour=12, minute=35, second=0, microsecond=0)


def main():
    init_db()
    logging.info("Bourse Alert Bot started")
    while True:
        try:
            if is_market_open():
                run_pipeline()
                time.sleep(600)
            else:
                time.sleep(60)
        except Exception:
            logging.exception("Pipeline failed")
            time.sleep(30)


if __name__ == "__main__":
    main()
