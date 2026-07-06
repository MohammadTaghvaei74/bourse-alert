import os
import requests

API_KEY = os.environ.get("BRSAPI_KEY")
URL = f"https://Api.BrsApi.ir/Tsetmc/AllSymbols.php?key={API_KEY}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

_printed_sample = False


def get_market_data():
    global _printed_sample

    if not API_KEY:
        print("BRSAPI_KEY not set", flush=True)
        return []

    try:
        resp = requests.get(URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"FETCH ERROR: {e}", flush=True)
        return []

    if not _printed_sample:
        print("RAW SAMPLE:", str(raw)[:2000], flush=True)
        _printed_sample = True

    return []
