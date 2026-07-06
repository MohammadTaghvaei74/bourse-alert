import json
import os
import time

import pytse_client as tse

AGRI_GROUP_KEYWORD = "زراع"  # matches "زراعت و خدمات وابسته" sector name
CACHE_FILE = "agri_symbols_cache.json"
REFRESH_SECONDS = 24 * 60 * 60  # rebuild the symbol list once a day

_cache = {"symbols": [], "built_at": 0}
_ticker_cache = {}


def _load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"symbols": [], "built_at": 0}
    return {"symbols": [], "built_at": 0}


def _save_cache(data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print("CACHE SAVE FAILED:", e, flush=True)


def _discover_agriculture_symbols():
    print("DISCOVERING AGRICULTURE SYMBOLS (first run can take a few minutes)...", flush=True)
    all_data = tse.download(symbols="all", write_to_csv=False)
    matched = []

    for symbol in all_data.keys():
        try:
            ticker = tse.Ticker(symbol)
            if AGRI_GROUP_KEYWORD in ticker.group_name:
                matched.append(symbol)
        except Exception as e:
            print(f"SKIP {symbol}: {e}", flush=True)
            continue

    print(f"AGRICULTURE SYMBOLS FOUND: {len(matched)}", flush=True)
    return matched


def _get_agriculture_symbols():
    global _cache
    now = time.time()

    if not _cache["symbols"]:
        _cache = _load_cache()

    if not _cache["symbols"] or (now - _cache["built_at"] > REFRESH_SECONDS):
        symbols = _discover_agriculture_symbols()
        if symbols:
            _cache = {"symbols": symbols, "built_at": now}
            _save_cache(_cache)

    return _cache["symbols"]


def _get_ticker(symbol):
    if symbol not in _ticker_cache:
        _ticker_cache[symbol] = tse.Ticker(symbol)
    return _ticker_cache[symbol]


def get_market_data():
    symbols = _get_agriculture_symbols()
    rows = []

    for symbol in symbols:
        try:
            ticker = _get_ticker(symbol)
            info = ticker.get_ticker_real_time_info_response()

            rows.append({
                "symbol": symbol,
                "tvol": info.volume or 0,
                "pl": info.last_price or 0,
                "tmax": ticker.sta_max or 0,
                "tmin": ticker.sta_min or 0,
                "qd1": info.best_demand_vol or 0,
                "qo1": info.best_supply_vol or 0,
            })
        except Exception as e:
            print(f"MARKET DATA ERROR {symbol}: {e}", flush=True)
            continue

    return rows
