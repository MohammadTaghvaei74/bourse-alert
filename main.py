import gzip
import hashlib
import html
import json
import logging
import os
import sqlite3
import statistics
import time
from collections import defaultdict
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
        conn.execute("""CREATE TABLE IF NOT EXISTS market_snapshots (
            timestamp TEXT PRIMARY KEY,
            count INTEGER NOT NULL,
            average REAL NOT NULL,
            median REAL NOT NULL,
            buy_value_hmt REAL NOT NULL,
            sell_value_hmt REAL NOT NULL,
            leader_average REAL NOT NULL DEFAULT 0,
            leader_median REAL NOT NULL DEFAULT 0
        )""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
        if "leader_average" not in columns:
            conn.execute("ALTER TABLE market_snapshots ADD COLUMN leader_average REAL NOT NULL DEFAULT 0")
        if "leader_median" not in columns:
            conn.execute("ALTER TABLE market_snapshots ADD COLUMN leader_median REAL NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_time ON market_snapshots(timestamp)")

        conn.execute("""CREATE TABLE IF NOT EXISTS stock_snapshots (
            timestamp TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            instrument TEXT,
            industry TEXT,
            instrument_type TEXT,
            yesterday_price REAL,
            close_price REAL,
            last_price REAL,
            last_pct REAL,
            trade_volume REAL,
            trade_value_toman REAL,
            buy_queue_volume REAL,
            sell_queue_volume REAL,
            buy_queue_value_toman REAL,
            sell_queue_value_toman REAL,
            score REAL,
            valid INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(timestamp, symbol)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_snapshots_date_symbol ON stock_snapshots(trading_date, symbol)")
        conn.execute("""CREATE TABLE IF NOT EXISTS group_snapshots (
            timestamp TEXT NOT NULL,
            group_name TEXT NOT NULL,
            count INTEGER NOT NULL,
            average REAL NOT NULL,
            median REAL NOT NULL,
            turnover_toman REAL NOT NULL,
            buy_value_hmt REAL NOT NULL,
            sell_value_hmt REAL NOT NULL,
            net_queue_hmt REAL NOT NULL,
            PRIMARY KEY(timestamp, group_name)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS industry_detail_snapshots (
            timestamp TEXT NOT NULL,
            industry TEXT NOT NULL,
            count INTEGER NOT NULL,
            average REAL NOT NULL,
            median REAL NOT NULL,
            turnover_toman REAL NOT NULL,
            buy_value_hmt REAL NOT NULL,
            sell_value_hmt REAL NOT NULL,
            net_queue_hmt REAL NOT NULL,
            PRIMARY KEY(timestamp, industry)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS raw_tse_snapshots (
            timestamp TEXT PRIMARY KEY,
            trading_date TEXT NOT NULL,
            stocks_path TEXT NOT NULL,
            depth_path TEXT NOT NULL,
            stocks_bytes INTEGER NOT NULL,
            depth_bytes INTEGER NOT NULL,
            stocks_sha256 TEXT NOT NULL,
            depth_sha256 TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_stock_summary (
            trading_date TEXT NOT NULL, symbol TEXT NOT NULL, industry TEXT,
            snapshot_count INTEGER NOT NULL, final_volume REAL, final_turnover_toman REAL,
            average_score REAL, median_score REAL, min_score REAL, max_score REAL, last_score REAL,
            max_buy_queue_toman REAL, max_sell_queue_toman REAL,
            PRIMARY KEY(trading_date, symbol)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_market_summary (
            trading_date TEXT PRIMARY KEY, snapshot_count INTEGER NOT NULL,
            traded_stock_count INTEGER NOT NULL, average_score REAL, median_score REAL,
            final_turnover_toman REAL, final_buy_value_hmt REAL, final_sell_value_hmt REAL,
            average_net_queue_hmt REAL, final_net_queue_hmt REAL,
            turnover_ratio_3_10 REAL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_market_stats (
            trading_date TEXT PRIMARY KEY,
            turnover_hmt REAL NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_snapshots_timestamp ON stock_snapshots(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_group_snapshots_timestamp ON group_snapshots(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_industry_detail_snapshots_timestamp ON industry_detail_snapshots(timestamp)")
        conn.commit()


def save_scores(stocks, now):
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("INSERT INTO history(symbol, score, timestamp) VALUES (?, ?, ?)", [(s["symbol"], s["score"], now.isoformat()) for s in stocks])
        conn.execute("DELETE FROM history WHERE timestamp < ?", ((now - timedelta(days=31)).isoformat(),))


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
            instrument_type = fields[25].strip() if len(fields) > 25 else ""
            description = fields[3].replace("ي", "ی").replace("ك", "ک").strip() if len(fields) > 3 else ""
            eligible_market_stock = (instrument_type in {"N1", "N2"} and not any(c.isdigit() for c in symbol)
                                     and not symbol.endswith("ح") and not description.startswith("صندوق"))
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
            buy_value_toman = best_buy_price * buy_volume / 10 if buy_queue else 0.0
            sell_value_toman = best_sell_price * sell_volume / 10 if sell_queue else 0.0
            if buy_queue and not sell_queue:
                side, volume, price = "buy", buy_volume, best_buy_price
            elif sell_queue and not buy_queue:
                side, volume, price = "sell", sell_volume, best_sell_price
            else:
                side, volume, price = None, 0, 0
            result.append({"instrument": instrument, "symbol": symbol, "score": calculate_score(last_pct, buy_volume if buy_queue else 0, sell_volume if sell_queue else 0, trade_volume), "trade_volume": trade_volume, "trade_value_toman": _float(fields[10]) / 10, "eligible_market_stock": eligible_market_stock, "instrument_type": instrument_type, "description": description, "yesterday_price": yesterday, "close_price": close, "last_price": last, "last_pct": last_pct, "buy_queue_volume": buy_volume if buy_queue else 0, "sell_queue_volume": sell_volume if sell_queue else 0, "buy_queue_value_toman": buy_value_toman, "sell_queue_value_toman": sell_value_toman, "queue_value": queue_value_billion_toman(price, volume) if side else 0, "queue_side": side})
        except (ValueError, IndexError, ZeroDivisionError):
            continue
    return result


def market_summary_values(stocks):
    eligible = [s for s in stocks if s.get("eligible_market_stock") and float(s.get("trade_volume", 0)) > 0]
    scores = [float(s["score"]) for s in eligible]
    average = statistics.mean(scores) if scores else 0.0
    median = statistics.median(scores) if scores else 0.0
    buy_value_hmt = sum(float(s.get("buy_queue_value_toman", 0)) for s in eligible) / 1_000_000_000_000
    sell_value_hmt = sum(float(s.get("sell_queue_value_toman", 0)) for s in eligible) / 1_000_000_000_000
    return len(scores), average, median, buy_value_hmt, sell_value_hmt


def market_allocation_signal(market_median, leader_median):
    gap = float(market_median) - float(leader_median)
    if gap > 1.0:
        return "تمایل شدید به سهام هم‌وزن"
    if gap >= 0.5:
        return "تمایل به سهام هم‌وزن"
    if gap < -1.0:
        return "تمایل شدید به لیدرها"
    if gap <= -0.5:
        return "تمایل به سهام لیدرها"
    return "تمایل خاصی وجود ندارد"


def classify_median(value):
    if value > 2: return "عالی"
    if value > 1: return "خوب"
    if value >= -1: return "معمولی"
    if value >= -2: return "بد"
    return "افتضاح"


def classify_imbalance(value):
    if value > 20: return "عالی"
    if value > 5: return "خوب"
    if value >= -5: return "معمولی"
    if value >= -20: return "بد"
    return "افتضاح"


def classify_turnover_ratio(value):
    if value is None: return "داده کافی نیست"
    if value > 1.5: return "عالی"
    if value >= 1.2: return "خوب"
    if value >= 0.8: return "معمولی"
    if value >= 0.6: return "بد"
    return "افتضاح"


def status_stars(status):
    """Convert the five-level status labels to a compact visual score."""
    return {
        "عالی": "⭐⭐⭐⭐⭐",
        "خوب": "⭐⭐⭐⭐",
        "معمولی": "⭐⭐⭐",
        "بد": "⭐⭐",
        "افتضاح": "⭐",
    }.get(status, "—")


def update_daily_turnover(stocks, now):
    turnover_hmt = sum(float(s.get("trade_value_toman", 0)) for s in stocks if s.get("eligible_market_stock") and float(s.get("trade_volume", 0)) > 0) / 1_000_000_000_000
    date = now.date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO daily_market_stats VALUES (?, ?, ?)", (date, turnover_hmt, now.isoformat()))
        rows = conn.execute("SELECT turnover_hmt FROM daily_market_stats ORDER BY trading_date DESC LIMIT 10").fetchall()
    values = [row[0] for row in rows]
    ratio = sum(values[:3]) / 3 / (sum(values[:10]) / len(values)) if len(values) >= 10 and sum(values[:10]) else None
    return turnover_hmt, ratio, classify_turnover_ratio(ratio)


CONFIGURED_INDUSTRIES = (
    "فلزات اساسی", "خودرو", "محصولات دارویی", "محصولات غذایی", "سیمان",
    "شرکتهای چند رشته ای", "شیمیایی", "بانک", "فراورده های نفتی",
    "استخراج کانه فلزی", "زراعت", "محصولات فلزی", "کاشی سرامیک",
)

INDUSTRY_NAMES = {
    "فلزات اساسي": "فلزات اساسی", "فلزات اساسی": "فلزات اساسی",
    "محصولات دارويي": "محصولات دارویی", "محصولات دارویی": "محصولات دارویی",
    "محصولات غذايي": "محصولات غذایی", "محصولات غذایی": "محصولات غذایی",
    "سيمان": "سیمان", "سیمان": "سیمان", "سیمان، آهک و گچ": "سیمان",
    "شيميايي": "شیمیایی", "شیمیایی": "شیمیایی", "محصولات شیمیایی": "شیمیایی",
    "بانکها و موسسات اعتباری": "بانک", "بانک": "بانک",
    "محصولات دارویی": "محصولات دارویی", "مواد و محصولات دارویی": "محصولات دارویی",
    "محصولات غذایی و آشامیدنی به جز قند و شکر": "محصولات غذایی",
    "خودرو و ساخت قطعات": "خودرو",
    "سیمان، آهک و گچ": "سیمان",
    "محصولات شیمیایی": "شیمیایی",
    "فراورده های نفتی، کک و سوخت هسته ای": "فراورده های نفتی",
    "استخراج کانه های فلزی": "استخراج کانه فلزی",
    "ساخت محصولات فلزی": "محصولات فلزی",
    "شرکتهای چند رشته ای صنعتی": "شرکتهای چند رشته ای",
    "زراعت و خدمات وابسته": "زراعت",
    "محصولات غذايي": "محصولات غذایی", "محصولات غذایی": "محصولات غذایی",
    "محصولات غذایی و آشامیدنی به جز قند و شکر": "محصولات غذایی",
    "خودرو و ساخت قطعات": "خودرو",
    "فراورده هاي نفتي": "فراورده های نفتی", "فراورده های نفتی": "فراورده های نفتی",
    "فراورده های نفتی، کک و سوخت هسته ای": "فراورده های نفتی",
    "استخراج کانه هاي فلزي": "استخراج کانه فلزی", "استخراج کانه فلزی": "استخراج کانه فلزی",
    "استخراج کانه های فلزی": "استخراج کانه فلزی",
    "محصولات فلزي": "محصولات فلزی", "محصولات فلزی": "محصولات فلزی", "ساخت محصولات فلزی": "محصولات فلزی",
    "کاشي و سراميک": "کاشی سرامیک", "کاشی و سرامیک": "کاشی سرامیک",
    "چند رشته ای": "شرکتهای چند رشته ای", "شرکتهای چند رشته ای": "شرکتهای چند رشته ای",
    "شرکتهای چند رشته ای صنعتی": "شرکتهای چند رشته ای",
    "زراعت و خدمات وابسته": "زراعت", "زراعت": "زراعت",
}


def normalize_industry(name):
    normalized = str(name or "").replace("ي", "ی").replace("ك", "ک").strip()
    return INDUSTRY_NAMES.get(normalized, normalized or None)


def instrument_type_label(code):
    return {"N1": "سهام بازار بورس", "N2": "فرابورس - بازار پایه"}.get(code, code)


def attach_industries(stocks, session=None):
    """Attach TSE sector names; cache them so only unknown instruments hit TSE."""
    cache_path = os.path.join(os.path.dirname(DB_PATH) or ".", "industry_cache.json")
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    client = session or requests.Session()
    changed = False
    for stock in stocks:
        if not (stock.get("eligible_market_stock") and float(stock.get("trade_volume", 0)) > 0):
            continue
        code = stock.get("instrument")
        if not code:
            continue
        if code not in cache or not cache.get(code):
            try:
                payload = client.get(f"https://cdn.tsetmc.com/api/Instrument/GetInstrumentInfo/{code}", timeout=10).json()
                sector = payload.get("instrumentInfo", {}).get("sector", {}).get("lSecVal")
                cache[code] = normalize_industry(sector)
                changed = True
            except (OSError, ValueError, requests.RequestException):
                continue
        stock["industry"] = cache.get(code)
        stock["industry_raw"] = cache.get(f"{code}:raw") or stock.get("industry")
    if changed:
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
    return stocks


def industry_stats(stocks):
    """Return traded, configured industries with median and top stock scores."""
    grouped = defaultdict(list)
    for stock in stocks:
        industry = stock.get("industry")
        if (
            industry in CONFIGURED_INDUSTRIES
            and stock.get("eligible_market_stock")
            and float(stock.get("trade_volume", 0)) > 0
        ):
            grouped[industry].append(stock)
    return [
        {
            "industry": industry,
            "median": statistics.median(float(stock["score"]) for stock in members),
            "top_stocks": sorted(members, key=lambda stock: float(stock["score"]), reverse=True)[:5],
        }
        for industry, members in grouped.items()
    ]


def save_industry_snapshot(values, now):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS industry_snapshots (timestamp TEXT NOT NULL, industry TEXT NOT NULL, median REAL NOT NULL, PRIMARY KEY(timestamp, industry))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO industry_snapshots(timestamp, industry, median) VALUES (?, ?, ?)",
            [(now.isoformat(), industry, float(median)) for industry, median in values.items()],
        )


def build_industry_message(stocks, now, limit=None):
    current = industry_stats(stocks)
    if not current:
        return "#صنایع"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS industry_snapshots (timestamp TEXT NOT NULL, industry TEXT NOT NULL, median REAL NOT NULL, PRIMARY KEY(timestamp, industry))"
        )
        old_rows = conn.execute(
            "SELECT industry, median FROM industry_snapshots WHERE timestamp < ? ORDER BY timestamp DESC",
            (now.isoformat(),),
        ).fetchall()
    previous = {}
    for industry, median in old_rows:
        previous.setdefault(industry, median)
    ranked = sorted(current, key=lambda item: item["median"], reverse=True)
    if limit:
        ranked = ranked[:limit]
    lines = ["#صنایع"]
    for index, item in enumerate(ranked, 1):
        delta = item["median"] - float(previous.get(item["industry"], 0.0))
        lines.append(f"{index}. {item['industry']} | {ltr_signed(item['median'])} | قبل ‎{delta:+.1f}‎")
        for stock_index, stock in enumerate(item["top_stocks"], 1):
            lines.append(f"   {stock_index}) {stock['symbol']} | {ltr_signed(stock['score'])}")
    return "\n".join(lines)


def market_summary(stocks, previous=None, leader_stats=None, turnover=None):
    count, _average, median, buy_hmt, sell_hmt = market_summary_values(stocks)
    imbalance = buy_hmt - sell_hmt
    median_status = classify_median(median)
    imbalance_status = classify_imbalance(imbalance)
    ratio_status = turnover[2] if turnover is not None else "داده کافی نیست"
    lines = [
        "📊 <b>#وضعیت_بازار</b>",
        "",
        f"📈 نمره میانه: {status_stars(median_status)}",
        f"⚖️ سربار تقاضا خالص: {status_stars(imbalance_status)}",
        f"💧 نسبت ارزش معاملات ۳ به ۱۰ روزه: {status_stars(ratio_status)}",
        "",
    ]
    if leader_stats is not None:
        leader_average, leader_median = leader_stats
        allocation_gap = median - leader_median
        allocation_signal = market_allocation_signal(median, leader_median)
        lines.extend([
            f"📍 اختلاف میانه کل بازار و لیدرها: {ltr_signed(allocation_gap)}",
            f"🧭 تمایل پول: {allocation_signal}",
            "",
        ])
    lines.extend([
        "🏦 <b>کل بازار</b>",
        f"میانه: {ltr_signed(median)} | قبل: {ltr_signed(median - previous[2]) if previous else ltr_signed(0)}",
        f"تعداد سهام معامله‌شده: {count}",
        "",
    ])
    if leader_stats is not None:
        leader_average, leader_median = leader_stats
        previous_leader_median = previous[6] if previous and len(previous) > 6 else None
        leader_median_delta = leader_median - previous_leader_median if previous_leader_median is not None else 0
        lines.extend([
            "👑 <b>لیدرها</b>",
            f"میانه: {ltr_signed(leader_median)} | قبل: {ltr_signed(leader_median_delta)}",
            "",
        ])
    buy_delta = buy_hmt - previous[3] if previous else 0
    sell_delta = sell_hmt - previous[4] if previous else 0
    lines.extend([
        f"🟢 خرید: {buy_hmt:.2f} همت | قبل: {buy_delta:.2f} همت",
        f"🔴 فروش: {sell_hmt:.2f} همت | قبل: {sell_delta:.2f} همت",
        f"⚖️ اختلاف صف: {imbalance:.2f} همت",
    ])
    if turnover is not None:
        turnover_hmt, turnover_ratio, turnover_status = turnover
        ratio_text = f"{turnover_ratio:.2f}" if turnover_ratio is not None else "داده کافی نیست"
        lines.extend([
            "",
            "💧 <b>ارزش معاملات</b>",
            f"امروز: {turnover_hmt:.0f} همت | نسبت ۳/۱۰روزه: {ratio_text}",
        ])
    return "\n".join(lines)


def save_market_snapshot(values, now, leader_average=0.0, leader_median=0.0):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (now.isoformat(), *values, leader_average, leader_median))
        conn.execute("DELETE FROM market_snapshots WHERE timestamp < ?", ((now - timedelta(days=7)).isoformat(),))


def previous_market_snapshot(now):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT count, average, median, buy_value_hmt, sell_value_hmt, leader_average, leader_median FROM market_snapshots WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1", (now.isoformat(),)).fetchone()
    return tuple(row) if row else None


def market_group_average_median(stocks):
    scores = [float(s["score"]) for s in stocks
              if s.get("eligible_market_stock") and float(s.get("trade_volume", 0)) > 0]
    return (statistics.mean(scores), statistics.median(scores)) if scores else (0.0, 0.0)



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
    chart_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
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


def _snapshot_points(now):
    start = now.replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT timestamp, average, median, leader_average, leader_median, buy_value_hmt, sell_value_hmt FROM market_snapshots WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp", (start, now.isoformat())).fetchall()
    return rows


def should_send_market_report(now):
    """Send the market-status text every two minutes from 09:00."""
    return (
        now.minute % 2 == 0
        and now >= now.replace(hour=9, minute=0, second=0, microsecond=0)
        and now <= now.replace(hour=12, minute=30, second=59, microsecond=0)
    )


def should_send_market_chart(now):
    """Keep charts and detailed reports on the ten-minute schedule."""
    return (
        now.minute % 10 == 0
        and now >= now.replace(hour=9, minute=30, second=0, microsecond=0)
        and now <= now.replace(hour=12, minute=30, second=59, microsecond=0)
    )



def seconds_until_next_minute(now):
    """Keep the polling loop aligned so it cannot drift past report minutes."""
    return max(0.1, 60 - now.second - now.microsecond / 1_000_000)


def should_save_snapshot(now):
    """Persist historical data on ten-minute boundaries."""
    return now.minute % 10 == 0


def save_detailed_snapshot(stocks, now, raw_stocks=None, raw_depth=None):
    """Save stock, group, industry and compressed raw snapshots, then prune at 31 days."""
    timestamp = now.isoformat()
    trading_date = now.date().isoformat()
    eligible = [s for s in stocks if s.get("eligible_market_stock") and float(s.get("trade_volume", 0)) > 0]
    stock_rows = [(timestamp, trading_date, s.get("symbol"), s.get("instrument"), s.get("industry"), instrument_type_label(s.get("instrument_type")), s.get("yesterday_price"), s.get("close_price"), s.get("last_price"), s.get("last_pct"), s.get("trade_volume"), s.get("trade_value_toman"), s.get("buy_queue_volume", 0), s.get("sell_queue_volume", 0), s.get("buy_queue_value_toman", 0), s.get("sell_queue_value_toman", 0), s.get("score"), 1) for s in eligible]
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("INSERT OR REPLACE INTO stock_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", stock_rows)
        groups = {"کل بازار": eligible, "لیدرها": [s for s in eligible if s.get("symbol") in LEADERS], "اهرمی‌ها": [s for s in eligible if s.get("symbol") in LEVERAGED_FUNDS]}
        for name, group in groups.items():
            scores = [float(s["score"]) for s in group]
            turnover = sum(float(s.get("trade_value_toman", 0)) for s in group)
            buy = sum(float(s.get("buy_queue_value_toman", 0)) for s in group) / 1e12
            sell = sum(float(s.get("sell_queue_value_toman", 0)) for s in group) / 1e12
            conn.execute("INSERT OR REPLACE INTO group_snapshots VALUES (?,?,?,?,?,?,?,?,?)", (timestamp, name, len(scores), statistics.mean(scores) if scores else 0, statistics.median(scores) if scores else 0, turnover, buy, sell, buy-sell))
        industry_groups = defaultdict(list)
        for s in eligible:
            if s.get("industry"):
                industry_groups[s["industry"]].append(s)
        for industry, group in industry_groups.items():
            scores = [float(s["score"]) for s in group]
            turnover = sum(float(s.get("trade_value_toman", 0)) for s in group)
            buy = sum(float(s.get("buy_queue_value_toman", 0)) for s in group) / 1e12
            sell = sum(float(s.get("sell_queue_value_toman", 0)) for s in group) / 1e12
            conn.execute("INSERT OR REPLACE INTO industry_detail_snapshots VALUES (?,?,?,?,?,?,?,?,?)", (timestamp, industry, len(scores), statistics.mean(scores), statistics.median(scores), turnover, buy, sell, buy-sell))
        cutoff = (now - timedelta(days=31)).isoformat()
        for table in ("stock_snapshots", "group_snapshots", "industry_detail_snapshots", "raw_tse_snapshots"):
            conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM daily_stock_summary WHERE trading_date < ?", ((now - timedelta(days=31)).date().isoformat(),))
        conn.execute("DELETE FROM daily_market_summary WHERE trading_date < ?", ((now - timedelta(days=31)).date().isoformat(),))
        rows = conn.execute("SELECT trading_date, symbol, MAX(industry), COUNT(*), MAX(trade_volume), MAX(trade_value_toman), AVG(score), MIN(score), MAX(score), MAX(buy_queue_value_toman), MAX(sell_queue_value_toman) FROM stock_snapshots WHERE trading_date = ? GROUP BY trading_date, symbol", (trading_date,)).fetchall()
        conn.executemany("INSERT OR REPLACE INTO daily_stock_summary(trading_date,symbol,industry,snapshot_count,final_volume,final_turnover_toman,average_score,median_score,min_score,max_score,last_score,max_buy_queue_toman,max_sell_queue_toman) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [(r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[6],r[7],r[8],r[8],r[9],r[10]) for r in rows])
        latest = conn.execute("SELECT score, trade_value_toman, buy_queue_value_toman, sell_queue_value_toman FROM stock_snapshots WHERE trading_date = ? AND timestamp = (SELECT MAX(timestamp) FROM stock_snapshots WHERE trading_date = ?)", (trading_date,trading_date)).fetchall()
        if latest:
            scores=[r[0] for r in latest]; buy=sum(r[2] for r in latest)/1e12; sell=sum(r[3] for r in latest)/1e12
            snapshot_count = conn.execute("SELECT COUNT(DISTINCT timestamp) FROM stock_snapshots WHERE trading_date = ?", (trading_date,)).fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO daily_market_summary VALUES (?,?,?,?,?,?,?,?,?,?,?)", (trading_date, snapshot_count, len(latest), sum(scores)/len(scores), statistics.median(scores), sum(r[1] for r in latest), buy, sell, buy-sell, buy-sell, None))
    if raw_stocks is not None and raw_depth is not None:
        raw_dir = os.path.join(os.path.dirname(DB_PATH) or ".", "raw_tse")
        os.makedirs(raw_dir, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        paths = []
        for label, payload in (("stocks", raw_stocks), ("depth", raw_depth)):
            path = os.path.join(raw_dir, f"{stamp}_{label}.json.gz")
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            with gzip.open(path, "wb", compresslevel=6) as fh: fh.write(data)
            paths.append((path, len(data), hashlib.sha256(data).hexdigest()))
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO raw_tse_snapshots VALUES (?,?,?,?,?,?,?,?)", (timestamp, trading_date, paths[0][0], paths[1][0], paths[0][1], paths[1][1], paths[0][2], paths[1][2]))



def create_market_charts(now, chart_dir):
    from PIL import Image, ImageDraw, ImageFont
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        def rtl(text):
            return get_display(arabic_reshaper.reshape(str(text)))
    except ImportError:
        def rtl(text):
            return str(text)

    rows = _snapshot_points(now)
    if len(rows) < 2:
        return []
    os.makedirs(chart_dir, exist_ok=True)
    font_paths = [
        "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    def font(size):
        for font_path in font_paths:
            try:
                return ImageFont.truetype(font_path, size)
            except OSError:
                continue
        return ImageFont.load_default()
    colors = ["#1565c0", "#d84315", "#2e7d32", "#8e24aa"]
    labels = ["میانه کل بازار", "میانه لیدرها"]
    times = [r[0] for r in rows]
    def render(title, series, legend, path, ylabel):
        width, height = 2200, 1250; left, right, top, bottom = 150, 430, 120, 180
        pw, ph = width-left-right, height-top-bottom
        vals = [v for line in series for v in line]; low, high = min(vals), max(vals)
        pad = max((high-low)*.12, 1); low -= pad; high += pad
        img = Image.new("RGB", (width,height), "white"); draw=ImageDraw.Draw(img)
        def x(i): return left + i*pw/max(len(rows)-1,1)
        def y(v): return top + (high-v)*ph/max(high-low,1e-9)
        draw.rectangle((0,0,width-1,height-1), outline="#bdbdbd", width=3)
        draw.text((width//2,35), rtl(title), fill="#111", font=font(38), anchor="ma")
        for k in range(7):
            val=high-(high-low)*k/6; yy=int(top+ph*k/6)
            draw.line((left,yy,left+pw,yy), fill="#e0e0e0", width=2); draw.text((left-18,yy),f"{val:.1f}",fill="#333",font=font(23),anchor="rm")
        for j,line in enumerate(series):
            draw.line([(int(x(i)),int(y(v))) for i,v in enumerate(line)], fill=colors[j], width=7, joint="curve")
        step=max(1,(len(rows)-1)//7)
        for i in range(0,len(rows),step):
            draw.text((int(x(i)),height-bottom+25),times[i][11:16],fill="#333",font=font(24),anchor="ma")
        draw.text((left+pw//2,height-35),rtl("زمان"),fill="#222",font=font(28),anchor="ma"); draw.text((35,(top+height-bottom)//2),rtl(ylabel),fill="#222",font=font(28),anchor="mm")
        for j,name in enumerate(legend):
            yy=top+15+j*58; draw.line((width-right+20,yy,width-right+85,yy),fill=colors[j],width=8); draw.text((width-right+105,yy),rtl(name),fill="#111",font=font(27),anchor="lm")
        img.save(path,"PNG",optimize=True); return path
    # Columns are: timestamp, market average, market median,
    # leader average, leader median, buy queue, sell queue.
    score_path = render(
        "#وضعیت بازار - روند میانه نمره",
        [
            [r[2] for r in rows],
            [r[4] for r in rows],
        ],
        labels,
        os.path.join(chart_dir, "market_scores.png"),
        "نمره",
    )
    return [score_path]


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
    send_market_report = should_send_market_report(now)
    send_detailed_reports = should_send_market_chart(now)
    stocks_raw, depth_raw = fetch_market_data(session)
    data = parse_market_data(stocks_raw, depth_raw)
    stock_dict = {s["symbol"]: s for s in data}
    leveraged = sorted((stock_dict[s] for s in LEVERAGED_FUNDS if s in stock_dict), key=lambda s: s["score"], reverse=True)
    leaders = sorted((stock_dict[s] for s in LEADERS if s in stock_dict), key=lambda s: s["score"], reverse=True)
    if not leveraged and not leaders:
        logging.warning("No configured symbols found in TSETMC data")
        return 0
    init_db()
    persist_snapshot = should_save_snapshot(now)
    if persist_snapshot:
        save_scores(data, now)

    # Group reports must not wait for the optional/slow industry lookup.
    # A TSE sector API/cache failure should not suppress #اهرمی and #لیدر.
    market_values = market_summary_values(data)
    market_previous = previous_market_snapshot(now)
    leader_average, leader_median = market_group_average_median(leaders)
    turnover = update_daily_turnover(data, now)
    if persist_snapshot:
        save_market_snapshot(market_values, now, leader_average, leader_median)
    if send_market_report:
        send_telegram(market_summary(data, market_previous, (leader_average, leader_median), turnover), session)
    if send_detailed_reports:
        send_telegram(build_group_message("#اهرمی", leveraged, now), session)
        send_telegram(build_group_message("#لیدر", leaders, now, limit=10), session)

    # Industry enrichment is optional and must happen after the group reports.
    attach_industries(data, session)
    industry_values = {item["industry"]: item["median"] for item in industry_stats(data)}
    industry_message = build_industry_message(data, now, limit=5)
    if persist_snapshot:
        save_industry_snapshot(industry_values, now)
        save_detailed_snapshot(data, now, stocks_raw, depth_raw)
    if send_detailed_reports:
        send_telegram(industry_message, session)
    if send_detailed_reports:
        chart_dir = os.path.join(os.path.dirname(DB_PATH) or ".", "charts")
        os.makedirs(chart_dir, exist_ok=True)
        leveraged_chart = create_score_chart("#اهرمی - روند نمره روزانه", leveraged, now, os.path.join(chart_dir, "leveraged.png"))
        leaders_chart = create_score_chart("#لیدر - روند نمره ۱۰ لیدر برتر", leaders[:10], now, os.path.join(chart_dir, "leaders.png"))
        if leveraged_chart:
            send_telegram_photo(leveraged_chart, "#اهرمی - نمودار روند نمره", session)
        if leaders_chart:
            send_telegram_photo(leaders_chart, "#لیدر - نمودار روند نمره ۱۰ لیدر برتر", session)
        market_charts = create_market_charts(now, chart_dir)
        for chart, caption in zip(market_charts, ("#وضعیت بازار - ۲ روند میانه نمره",)):
            send_telegram_photo(chart, caption, session)
    return len(data)


def is_market_open(now=None):
    now = now or datetime.now(TEHRAN)
    return now.weekday() not in (3, 4) and now.replace(hour=9, minute=0, second=0, microsecond=0) <= now <= now.replace(hour=12, minute=35, second=0, microsecond=0)


def main():
    init_db()
    logging.info("Bourse Alert Bot started")
    while True:
        try:
            if is_market_open():
                run_pipeline()
                time.sleep(seconds_until_next_minute(datetime.now(TEHRAN)))
            else:
                time.sleep(60)
        except Exception:
            logging.exception("Pipeline failed")
            time.sleep(30)


def send_current_market_summary():
    stocks_raw, depth_raw = fetch_market_data()
    data = parse_market_data(stocks_raw, depth_raw)
    values = market_summary_values(data)
    now = datetime.now(TEHRAN)
    previous = previous_market_snapshot(now)
    stock_dict = {s["symbol"]: s for s in data}
    leaders = [stock_dict[s] for s in LEADERS if s in stock_dict]
    leader_stats = market_group_average_median(leaders)
    init_db()
    turnover = update_daily_turnover(data, now)
    message = market_summary(data, previous, leader_stats, turnover)
    result = send_telegram(message)
    save_market_snapshot(values, now, *leader_stats)
    return result


if __name__ == "__main__":
    if "--market-summary" in os.sys.argv:
        send_current_market_summary()
    else:
        main()
