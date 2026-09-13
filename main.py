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
    """Render a high-resolution SVG chart and convert it to PNG with Pillow."""
    symbols = [s["symbol"] for s in stocks]
    points = _chart_points(symbols, now)
    if not points:
        return None
    width, height = 1800, 1050
    left, right, top, bottom = 110, 460, 90, 120
    plot_w, plot_h = width-left-right, height-top-bottom
    values = [v for _, row in points for v in row.values()]
    if not values:
        return None
    lo, hi = min(values), max(values)
    span = max(hi-lo, 1.0); lo -= span*.08; hi += span*.08
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    def x_at(i): return left + (i * plot_w / max(len(points)-1, 1))
    def y_at(v): return top + (hi-v) * plot_h / (hi-lo)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">', '<rect width="100%" height="100%" fill="white"/>', '<style>text{font-family:DejaVu Sans;font-size:22px} .title{font-size:30px;font-weight:bold}</style>']
    parts.append(f'<text x="{width//2}" y="45" text-anchor="middle" class="title">{title}</text>')
    for i in range(6):
        y = top + plot_h*i/5; val = hi-(hi-lo)*i/5
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" stroke="#dddddd"/>')
        parts.append(f'<text x="{left-15}" y="{y+8:.1f}" text-anchor="end">{val:.1f}</text>')
    parts.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#333" stroke-width="3"/>')
    parts.append(f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#333" stroke-width="3"/>')
    for idx, symbol in enumerate(symbols):
        coords = [(x_at(i), y_at(row[symbol])) for i, (_, row) in enumerate(points) if symbol in row]
        if len(coords) > 1:
            parts.append(f'<polyline fill="none" stroke="{colors[idx%len(colors)]}" stroke-width="4" points="' + " ".join(f"{x:.1f},{y:.1f}" for x,y in coords) + '"/>')
    avg = []
    for i, (_, row) in enumerate(points):
        vals = [row[s] for s in symbols if s in row]
        if vals: avg.append((x_at(i), y_at(sum(vals)/len(vals))))
    if len(avg) > 1:
        parts.append('<polyline fill="none" stroke="#000000" stroke-width="8" points="' + " ".join(f"{x:.1f},{y:.1f}" for x,y in avg) + '"/>')
    for idx, symbol in enumerate(symbols):
        y = top + idx*42
        parts.append(f'<line x1="{width-right+20}" y1="{y}" x2="{width-right+65}" y2="{y}" stroke="{colors[idx%len(colors)]}" stroke-width="6"/>')
        parts.append(f'<text x="{width-right+80}" y="{y+8}">{symbol}</text>')
    y = top + len(symbols)*42
    parts.append(f'<line x1="{width-right+20}" y1="{y}" x2="{width-right+65}" y2="{y}" stroke="#000" stroke-width="8"/>')
    parts.append(f'<text x="{width-right+80}" y="{y+8}">میانگین</text>')
    parts.append(f'<text x="{left+plot_w/2}" y="{height-25}" text-anchor="middle">زمان (از ۹:۳۰)</text><text x="25" y="{top+plot_h/2}" transform="rotate(-90 25 {top+plot_h/2})" text-anchor="middle">نمره</text></svg>')
    svg = "".join(parts)
    svg_path = filename + ".svg"
    with open(svg_path, "w", encoding="utf-8") as f: f.write(svg)
    try:
        from PIL import Image, ImageDraw, ImageFont
        scale = 2
        img = Image.new("RGB", (width * scale, height * scale), "white")
        draw = ImageDraw.Draw(img)
        def font(size):
            try:
                return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size * scale)
            except OSError:
                return ImageFont.load_default()
        draw.rectangle((0, 0, width * scale - 1, height * scale - 1), outline="#bbbbbb", width=3)
        draw.text((width * scale // 2, 28 * scale), title, fill="black", font=font(30), anchor="ma")
        for i in range(6):
            y = int((top + plot_h * i / 5) * scale)
            draw.line((left * scale, y, (left + plot_w) * scale, y), fill="#dddddd", width=2)
            val = hi - (hi - lo) * i / 5
            draw.text(((left - 15) * scale, y), f"{val:.1f}", fill="#333333", font=font(20), anchor="rm")
        for idx, symbol in enumerate(symbols):
            coords = [(int(x * scale), int(y * scale)) for x, y in [(x_at(i), y_at(row[symbol])) for i, (_, row) in enumerate(points) if symbol in row]]
            if len(coords) > 1:
                draw.line(coords, fill=colors[idx % len(colors)], width=5 * scale, joint="curve")
            ly = int((top + idx * 42) * scale)
            draw.line(((width - right + 20) * scale, ly, (width - right + 65) * scale, ly), fill=colors[idx % len(colors)], width=6 * scale)
            draw.text(((width - right + 80) * scale, ly), symbol, fill="black", font=font(22), anchor="lm")
        if len(avg) > 1:
            draw.line([(int(x * scale), int(y * scale)) for x, y in avg], fill="black", width=8 * scale, joint="curve")
        ly = int((top + len(symbols) * 42) * scale)
        draw.line(((width - right + 20) * scale, ly, (width - right + 65) * scale, ly), fill="black", width=8 * scale)
        draw.text(((width - right + 80) * scale, ly), "میانگین", fill="black", font=font(22), anchor="lm")
        img.save(filename, "PNG", optimize=True)

    finally:
        try: os.remove(svg_path)
        except OSError: pass
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
