"""
NSE Bullish OI Scraper — Streamlit App
Finds stocks with Rise in OI + Rise in Price (long build-up)
by combining NSE's OI Spurts data with NSE's F&O price-change data.

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
# Gives lastPrice / change / pChange for every F&O-eligible stock
PRICE_URL = f"{BASE}/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"

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


def get_session():
    """NSE requires cookies from a normal page load first, or API calls 401/403."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE, timeout=10)
    time.sleep(1)
    session.get(f"{BASE}/market-data/oi-spurts", timeout=10)
    time.sleep(1)
    return session


@st.cache_data(ttl=30 * 60, show_spinner=False)
def fetch_oi_and_price():
    """Fetches both OI data and price-change data in one session and returns both raw JSONs."""
    session = get_session()

    oi_resp = session.get(OI_URL, timeout=10)
    oi_resp.raise_for_status()
    oi_data = oi_resp.json()

    time.sleep(0.5)

    price_resp = session.get(PRICE_URL, timeout=10)
    price_resp.raise_for_status()
    price_data = price_resp.json()

    return oi_data, price_data


def bullish_dataframe(oi_data, price_data):
    """
    Joins OI-spurts data (changeInOI, prevOI) with price data (pChange) by symbol,
    then filters for Rise in OI % + Rise in Price %.
    """
    # --- Build OI % change lookup: symbol -> oi_pct_change ---
    oi_rows = oi_data.get("data", {})
    # The "data" key holds sub-lists keyed by underlying type (e.g. "" for stocks);
    # flatten all of them into one list of records.
    flat_oi_rows = []
    if isinstance(oi_rows, dict):
        for v in oi_rows.values():
            if isinstance(v, list):
                flat_oi_rows.extend(v)
    elif isinstance(oi_rows, list):
        flat_oi_rows = oi_rows

    oi_lookup = {}
    for r in flat_oi_rows:
        symbol = r.get("symbol")
        prev_oi = r.get("prevOI")
        change_in_oi = r.get("changeInOI")
        if symbol and prev_oi not in (None, 0) and change_in_oi is not None:
            oi_pct = (float(change_in_oi) / float(prev_oi)) * 100
            oi_lookup[symbol] = oi_pct

    # --- Build price % change lookup: symbol -> pChange ---
    price_rows = price_data.get("data", [])
    price_lookup = {}
    for r in price_rows:
        symbol = r.get("symbol")
        p_change = r.get("pChange")
        if symbol and p_change is not None:
            price_lookup[symbol] = float(p_change)

    # --- Join and filter ---
    records = []
    for symbol, oi_pct in oi_lookup.items():
        price_pct = price_lookup.get(symbol)
        if price_pct is None:
            continue
        if oi_pct > 0 and price_pct > 0:
            records.append({
                "Symbol": symbol,
                "OI Change %": round(oi_pct, 2),
                "Price Change %": round(price_pct, 2),
            })

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.sort_values("OI Change %", ascending=False).reset_index(drop=True)
    return df


# ---------------- UI ----------------

st.set_page_config(page_title="NSE Bullish OI Finder", layout="centered")
st.title("📈 NSE Bullish OI Finder")
st.caption("Rise in OI + Rise in Price → possible long build-up by big players")

refresh_count = st_autorefresh(interval=REFRESH_INTERVAL_MS, key="oi_autorefresh")

top_n = st.slider("How many stocks to show?", min_value=3, max_value=20, value=5)
manual_refresh = st.button("Refresh now", type="secondary")

if manual_refresh:
    fetch_oi_and_price.clear()

with st.spinner("Fetching from NSE..."):
    try:
        oi_raw, price_raw = fetch_oi_and_price()
        df = bullish_dataframe(oi_raw, price_raw)

        st.caption(
            f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
            f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min"
        )

        if df.empty:
            st.warning("No stocks matched Rise in OI + Rise in Price right now.")
        else:
            st.success(f"Found {len(df)} bullish stocks. Showing top {top_n}.")
            st.dataframe(df.head(top_n), use_container_width=True, hide_index=True)

        with st.expander("Raw OI API response (debug)"):
            st.json(oi_raw)
        with st.expander("Raw Price API response (debug)"):
            st.json(price_raw)

    except requests.exceptions.HTTPError as e:
        st.error(f"NSE blocked the request ({e}). Try again in a few seconds.")
    except Exception as e:
        st.error(f"Something went wrong: {e}")

st.divider()
st.caption("⚠️ Informational only, not investment advice. Best run during market hours (9:15 AM–3:30 PM IST).")
