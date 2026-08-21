"""
NSE Bullish OI Scraper
Finds top stocks with Rise in OI + Rise in Price (long build-up)
using NSE India's public OI Spurts API.

Run locally (this needs to reach nseindia.com, which is blocked
from Claude's sandbox network):

    pip install requests
    python nse_bullish_oi_scraper.py
"""

import requests
import time

BASE = "https://www.nseindia.com"
API_URL = f"{BASE}/api/live-analysis-oi-spurts-underlyings"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/market-data/oi-spurts",
}


def get_session():
    """NSE requires a valid cookie set from a normal page load first,
    or every API call returns 401/403."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Hit the homepage first to collect cookies
    session.get(BASE, timeout=10)
    time.sleep(1)
    # Hit the actual OI spurts page to get page-specific cookies
    session.get(f"{BASE}/market-data/oi-spurts", timeout=10)
    time.sleep(1)
    return session


def fetch_oi_spurts():
    session = get_session()
    resp = session.get(API_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def top_bullish_stocks(data, top_n=5):
    """
    Filters for Rise in OI + Rise in Price, sorts by % change in OI.
    Adjust field names below if NSE changes their JSON schema —
    check the raw response with print(data) if this KeyErrors.
    """
    rows = data.get("data", data) if isinstance(data, dict) else data
    bullish = []

    for r in rows:
        try:
            change_in_oi_pct = float(r.get("changeInOI_Perc", r.get("perChange", 0)))
            price_change_pct = float(r.get("changeInPrice_Perc", r.get("pChange", 0)))
        except (TypeError, ValueError):
            continue

        if change_in_oi_pct > 0 and price_change_pct > 0:
            bullish.append({
                "symbol": r.get("symbol", r.get("underlying", "")),
                "oi_change_pct": change_in_oi_pct,
                "price_change_pct": price_change_pct,
            })

    bullish.sort(key=lambda x: x["oi_change_pct"], reverse=True)
    return bullish[:top_n]


if __name__ == "__main__":
    raw = fetch_oi_spurts()
    top5 = top_bullish_stocks(raw, top_n=5)

    print("\nTop 5 Bullish Stocks (Rise in OI + Rise in Price)\n" + "-" * 50)
    for i, s in enumerate(top5, 1):
        print(f"{i}. {s['symbol']:<15} OI +{s['oi_change_pct']:.2f}%   Price +{s['price_change_pct']:.2f}%")
