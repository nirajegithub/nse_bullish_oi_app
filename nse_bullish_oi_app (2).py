"""
NSE Bullish OI Scraper — Streamlit App
Finds stocks with Rise in OI + Rise in Price (long build-up)
by combining NSE's OI Spurts data with per-symbol price data.

Auto-refreshes every 30 minutes.

Run locally (needs internet access to nseindia.com):

    pip install streamlit requests pandas streamlit-autorefresh
    streamlit run nse_bullish_oi_app.py
"""

import time
from datetime import datetime

import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BASE = "https://www.nseindia.com"
OI_URL = f"{BASE}/api/live-analysis-oi-spurts-underlyings"
QUOTE_URL = f"{BASE}/api/quote-equity"  # ?symbol=XXX  -> stable per-symbol endpoint

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/market-data/oi-spurts",
}

REFRESH_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes
REQUEST_DELAY_SEC = 0.4  # stay under NSE's ~3 req/sec limit


def get_session():
    """NSE requires cookies from a normal page load first, or API calls 401/403."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE, timeout=10)
    time.sleep(1)
    session.get(f"{BASE}/market-data/oi-spurts", timeout=10)
    time.sleep(1)
    return session


def flatten_oi_rows(oi_data):
    """The 'data' key holds sub-lists keyed by underlying type; flatten into one list."""
    rows = oi_data.get("data", {})
    flat = []
    if isinstance(rows, dict):
        for v in rows.values():
            if isinstance(v, list):
                flat.extend(v)
    elif isinstance(rows, list):
        flat = rows
    return flat


@st.cache_data(ttl=30 * 60, show_spinner=False)
def fetch_bullish_data():
    """
    1. Fetch OI Spurts data, compute % change in OI per symbol.
    2. Keep only symbols where OI % change > 0 (reduces how many price
       lookups we need to make, since we only care about bullish OI anyway).
    3. For each of those, fetch live price change % from the per-symbol
       quote endpoint.
    4. Return combined records + the raw OI JSON for debugging.
    """
    session = get_session()

    oi_resp = session.get(OI_URL, timeout=10)
    oi_resp.raise_for_status()
    oi_data = oi_resp.json()

    flat_rows = flatten_oi_rows(oi_data)

    oi_candidates = []
    for r in flat_rows:
        symbol = r.get("symbol")
        prev_oi = r.get("prevOI")
        change_in_oi = r.get("changeInOI")
        if symbol and prev_oi not in (None, 0) and change_in_oi is not None:
            oi_pct = (float(change_in_oi) / float(prev_oi)) * 100
            if oi_pct > 0:
                oi_candidates.append({"symbol": symbol, "oi_pct": oi_pct})

    records = []
    price_errors = 0
    for c in oi_candidates:
        symbol = c["symbol"]
        try:
            resp = session.get(QUOTE_URL, params={"symbol": symbol}, timeout=10)
            resp.raise_for_status()
            q = resp.json()
            p_change = q.get("priceInfo", {}).get("pChange")
            if p_change is not None and float(p_change) > 0:
                records.append({
                    "Symbol": symbol,
                    "OI Change %": round(c["oi_pct"], 2),
                    "Price Change %": round(float(p_change), 2),
                })
        except Exception:
            price_errors += 1
        time.sleep(REQUEST_DELAY_SEC)

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.sort_values("OI Change %", ascending=False).reset_index(drop=True)

    return df, oi_data, len(oi_candidates), price_errors


# ---------------- UI ----------------

st.set_page_config(page_title="NSE Bullish OI Finder", layout="centered")
st.title("📈 NSE Bullish OI Finder")
st.caption("Rise in OI + Rise in Price → possible long build-up by big players")

refresh_count = st_autorefresh(interval=REFRESH_INTERVAL_MS, key="oi_autorefresh")

top_n = st.slider("How many stocks to show?", min_value=3, max_value=20, value=5)
manual_refresh = st.button("Refresh now", type="secondary")

if manual_refresh:
    fetch_bullish_data.clear()

with st.spinner("Fetching from NSE (this can take 20-60s — checking price for each OI-rising stock)..."):
    try:
        df, oi_raw, num_candidates, num_errors = fetch_bullish_data()

        st.caption(
            f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
            f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min"
        )
        st.caption(f"Checked {num_candidates} stocks with rising OI  •  {num_errors} price lookups failed")

        if df.empty:
            st.warning("No stocks matched Rise in OI + Rise in Price right now.")
        else:
            st.success(f"Found {len(df)} bullish stocks. Showing top {top_n}.")
            st.dataframe(df.head(top_n), use_container_width=True, hide_index=True)

        with st.expander("Raw OI API response (debug)"):
            st.json(oi_raw)

    except requests.exceptions.HTTPError as e:
        st.error(f"NSE blocked the request ({e}). Try again in a few seconds.")
    except Exception as e:
        st.error(f"Something went wrong: {e}")

st.divider()
st.caption("⚠️ Informational only, not investment advice. Best run during market hours (9:15 AM–3:30 PM IST).")
