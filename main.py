import html
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import json
import os
import subprocess
import tempfile
import requests

DB_PATH = os.getenv("DB_PATH", "/root/bourse-alert/scores_history.db")
TSETMC_URL = "https://old.tsetmc.com/tsev2/data/MarketWatchPlus.aspx"
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "https://tg-proxy.m-taghvaei74.workers.dev")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004419199993")
TEHRAN = ZoneInfo("Asia/Tehran")

LEVERAGED_FUNDS = ["اهرم", "شتاب", "موج", "جهش", "توان", "نارنج", "بیدار"]
LEADERS = ["ذوب", "فملی", "فولاد", "تاپیکو", "شستا", "شبریز", "شتران", "وغدیر", "شپنا", "شبندر", "پالایش", "خگستر", "فارس", "خودرو", "وبصادر", "وبملت", "خساپا", "دارا یکم", "پارسان", "وتجارت"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def calculate_score(last_pct, buy_queue_volume, sell_queue_volume, trade_volume):
    effective_volume = max(float(trade_volume), 50_000.0)
    return round(float(last_pct) + float(buy_queue_volume) / effective_volume - float(sell_queue_volume) / effective_volume, 2)


def queue_value_billion_toman(price, volume):
    # TSE prices and values are in rials; divide by 10 for tomans.
    return int(float(price) * float(volume) / 10 / 1_000_000_000)


def ltr_signed(value, decimals=1):
    value = float(value)
    sign = "-" if value < 0 else ""
    return f"\u200e{sign}{abs(value):.{decimals}f}\u200e"


def format_report_line(index, stock):
    score = ltr_signed(stock["score"])
    previous_delta = ltr_signed(stock.get("previous_delta", 0.0))
    value = int(stock.get("queue_value", 0))
    parts = [f"{index}. {stock['symbol']}", score]
    if value and stock.get("queue_side") == "buy":
        parts.append(f"\u200e{value} B\u200e")
    elif value and stock.get("queue_side") == "sell":
        parts.append(f"\u200e-{value} B\u200e")
    parts.append(f"قبل {previous_delta}")
    return " | ".join(parts)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS history (symbol TEXT NOT NULL, score REAL NOT NULL, timestamp TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_symbol_time ON history(symbol, timestamp)")


def save_scores(stocks, now):
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("INSERT INTO history(symbol, score, timestamp) VALUES (?, ?, ?)", [(s["symbol"], s["score"], now.isoformat()) for s in stocks])
        conn.execute("DELETE FROM history WHERE timestamp < ?", ((now - timedelta(days=7)).isoformat(),))


def _snapshot_avg(symbols, start=None, end=None):
    """Average the latest score for each symbol in a time window."""
    if not symbols:
        return None
    marks = ",".join("?" for _ in symbols)
    clauses = [f"symbol IN ({marks})"]
    params = list(symbols)
    if start:
        clauses.append("timestamp >= ?")
        params.append(start.isoformat())
    if end:
        clauses.append("timestamp < ?")
        params.append(end.isoformat())
    query = f"""
        SELECT AVG(score) FROM (
            SELECT score,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
            FROM history
            WHERE {' AND '.join(clauses)}
        ) WHERE rn = 1
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(query, params).fetchone()
    return row[0] if row and row[0] is not None else None


def previous_scores(symbols, now):
    if not symbols:
        return {}
    marks = ",".join("?" for _ in symbols)
    query = f"""
        SELECT symbol, score FROM (
            SELECT symbol, score,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
            FROM history
            WHERE symbol IN ({marks}) AND timestamp < ?
        ) WHERE rn = 1
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(query, list(symbols) + [now.isoformat()]).fetchall()
    return {symbol: score for symbol, score in rows}


def group_stats(stocks, now):
    init_db()
    symbols = [s["symbol"] for s in stocks]
    current = sum(s["score"] for s in stocks) / len(stocks) if stocks else 0.0
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    previous = _snapshot_avg(symbols, end=now)
    yesterday = _snapshot_avg(symbols, start=today_start - timedelta(days=1), end=today_start)
    five_day = _snapshot_avg(symbols, start=today_start - timedelta(days=5), end=today_start)
    return current, tuple(current - x if x is not None else 0.0 for x in (previous, yesterday, five_day))


def fmt_delta(value):
    return ltr_signed(value)


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
    lines = [f"<b>{html.escape(title)}</b>", f"میانگین {ltr_signed(average)} | قبل {fmt_delta(deltas[0])}", ""]
    old_scores = previous_scores([s["symbol"] for s in shown], now)
    for stock in shown:
        stock["previous_delta"] = stock["score"] - old_scores.get(stock["symbol"], stock["score"])
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


def _chart_points(symbols, now):
    if not symbols:
        return []
    marks = ",".join("?" for _ in symbols)
    chart_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    query = f"SELECT symbol, score, timestamp FROM history WHERE symbol IN ({marks}) AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp, symbol"
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(query, list(symbols) + [chart_start.isoformat(), now.isoformat()]).fetchall()
    points = {}
    for symbol, score, timestamp in rows:
        points.setdefault(timestamp, {})[symbol] = score
    return [(timestamp, values) for timestamp, values in sorted(points.items())]


def create_score_chart(title, stocks, now, filename):
    """Create a sharp PNG with a broken Y-axis when an outlier compresses the data."""
    from PIL import Image, ImageDraw, ImageFont
    symbols = [s["symbol"] for s in stocks]
    points = _chart_points(symbols, now)
    if not points:
        return None
    width, height = 2400, 1500
    left, right, top, bottom = 170, 520, 120, 190
    plot_w = width - left - right
    colors = ["#1565c0", "#e65100", "#2e7d32", "#c62828", "#6a1b9a", "#00838f", "#ad1457", "#546e7a", "#ef6c00", "#283593"]
    values = [v for _, row in points for v in row.values()]
    if not values:
        return None
    values_sorted = sorted(values)
    q1 = values_sorted[len(values_sorted) // 4]
    q3 = values_sorted[(len(values_sorted) * 3) // 4]
    iqr = max(q3 - q1, 1.0)
    outlier = max(values) > q3 + 1.5 * iqr and max(values) - min(values) > 8
    lo = min(values); hi = max(values)
    if outlier:
        lower_hi = min(hi - 1, q3 + 0.75 * iqr)
        panels = [(top + 35, 610, lo, lower_hi), (700, 1050, lower_hi, hi)]
    else:
        panels = [(top + 35, height - bottom, lo, hi)]
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    def font(size):
        try: return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
        except OSError: return ImageFont.load_default()
    draw.rectangle((0, 0, width - 1, height - 1), outline="#bdbdbd", width=3)
    draw.text((width // 2, 35), title, fill="#111111", font=font(38), anchor="ma")
    def x_at(i): return left + i * plot_w / max(len(points) - 1, 1)
    def y_at(value, panel):
        y0, y1, low, high = panel
        return y1 - (value - low) * (y1 - y0) / max(high - low, 1e-9)
    for panel in panels:
        y0, y1, low, high = panel
        for tick in range(6):
            value = high - (high - low) * tick / 5
            y = int(y0 + (y1 - y0) * tick / 5)
            draw.line((left, y, left + plot_w, y), fill="#e0e0e0", width=2)
            draw.text((left - 18, y), f"{value:.1f}", fill="#333333", font=font(24), anchor="rm")
        draw.line((left, y0, left, y1), fill="#333333", width=4)
        draw.line((left, y1, left + plot_w, y1), fill="#333333", width=4)
    if outlier:
        mid = 655
        draw.line((left - 12, mid - 12, left + 12, mid + 12), fill="#333333", width=4)
        draw.line((left - 12, mid + 12, left + 12, mid + 36), fill="#333333", width=4)
        draw.line((left + plot_w - 12, mid - 12, left + plot_w + 12, mid + 12), fill="#333333", width=4)
        draw.line((left + plot_w - 12, mid + 12, left + plot_w + 12, mid + 36), fill="#333333", width=4)
    def draw_series(symbol, color, panel):
        segments = []; current = []
        for i, (_, row) in enumerate(points):
            if symbol in row and panel[2] <= row[symbol] <= panel[3]: current.append((int(x_at(i)), int(y_at(row[symbol], panel))))
            elif len(current) > 1: segments.append(current); current = []
        if len(current) > 1: segments.append(current)
        for segment in segments: draw.line(segment, fill=color, width=6, joint="curve")
    for idx, symbol in enumerate(symbols):
        for panel in panels: draw_series(symbol, colors[idx % len(colors)], panel)
    for panel in panels:
        y0, y1, low, high = panel
        avg_points = []
        for i, (_, row) in enumerate(points):
            vals = [row[s] for s in symbols if s in row and low <= row[s] <= high]
            if vals: avg_points.append((int(x_at(i)), int(y_at(sum(vals) / len(vals), panel))))
        if len(avg_points) > 1: draw.line(avg_points, fill="#000000", width=10, joint="curve")
    if points:
        label_count = min(8, len(points)); step = max(1, (len(points) - 1) // (label_count - 1))
        for i in range(0, len(points), step):
            timestamp = points[i][0]
            label = timestamp[11:16] if len(timestamp) >= 16 else str(i)
            draw.line((int(x_at(i)), height - bottom, int(x_at(i)), height - bottom + 12), fill="#333333", width=2)
            draw.text((int(x_at(i)), height - bottom + 25), label, fill="#333333", font=font(24), anchor="ma")
    draw.text((left + plot_w // 2, height - 35), "زمان (از ۹:۳۰)", fill="#222222", font=font(28), anchor="ma")
    draw.text((35, (top + height - bottom) // 2), "نمره", fill="#222222", font=font(28), anchor="mm")
    legend_y = top + 15
    for idx, symbol in enumerate(symbols):
        y = legend_y + idx * 52
        color = colors[idx % len(colors)]
        draw.line((width - right + 20, y, width - right + 85, y), fill=color, width=7)
        draw.text((width - right + 105, y), symbol, fill="#111111", font=font(27), anchor="lm")
    y = legend_y + len(symbols) * 52
    draw.line((width - right + 20, y, width - right + 85, y), fill="#000000", width=10)
    draw.text((width - right + 105, y), "میانگین", fill="#111111", font=font(27), anchor="lm")
    if outlier:
        draw.text((left + 20, 665), "مقیاس شکسته برای نمایش بهتر نقاط پرت", fill="#555555", font=font(22))
    img.save(filename, "PNG", optimize=True)
    return filename


def send_telegram_photo(filename, caption, session=None):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"{TELEGRAM_PROXY.rstrip('/')}/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    with open(filename, "rb") as image:
        response = (session or requests).post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption}, files={"photo": image}, timeout=30)
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
    chart_dir = os.path.join(os.path.dirname(DB_PATH) or ".", "charts")
    os.makedirs(chart_dir, exist_ok=True)
    leveraged_chart = create_score_chart("#اهرمی - روند نمره روزانه", leveraged, now, os.path.join(chart_dir, "leveraged.png"))
    leaders_chart = create_score_chart("#لیدر - روند نمره ۱۰ لیدر برتر", leaders[:10], now, os.path.join(chart_dir, "leaders.png"))
    if leveraged_chart:
        send_telegram_photo(leveraged_chart, "#اهرمی - نمودار روند نمره", session)
    if leaders_chart:
        send_telegram_photo(leaders_chart, "#لیدر - نمودار روند نمره ۶ لیدر برتر", session)
    return len(data)


def is_market_open(now=None):
    now = now or datetime.now(TEHRAN)
    return now.weekday() not in (3, 4) and now.replace(hour=9, minute=30, second=0, microsecond=0) <= now <= now.replace(hour=12, minute=35, second=0, microsecond=0)


def main():
    init_db()
    logging.info("Bourse Alert Bot started")
    while True:
        try:
            if is_market_open():
                run_pipeline()
                time.sleep(120)
            else:
                time.sleep(60)
        except Exception:
            logging.exception("Pipeline failed")
            time.sleep(30)


if __name__ == "__main__":
    main()
