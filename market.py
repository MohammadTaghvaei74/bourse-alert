import requests

AGRICULTURE_GROUP_CODE = "1"
HEADERS = {"User-Agent": "Mozilla/5.0"}

_printed_sample = False


def get_market_data():
    global _printed_sample
    url = f"http://cdn.tsetmc.com/api/ClosingPrice/GetRelatedCompany/{AGRICULTURE_GROUP_CODE}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"FETCH ERROR: {e}", flush=True)
        return []

    if not _printed_sample:
        print("RAW SAMPLE:", raw, flush=True)
        _printed_sample = True

    return []
