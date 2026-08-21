"""
NSE Bullish OI Scraper — Streamlit App
Finds stocks with Rise in OI + Rise in Price (long build-up)
using NSE India's public OI Spurts data.

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
    """NSE requires cookies from a normal page load first, or API calls 401/403."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE, timeout=10)
    time.sleep(1)
    session.get(f"{BASE}/market-data/oi-spurts", timeout=10)
    time.sleep(1)
    return session


REFRESH_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes


@st.cache_data(ttl=30 * 60, show_spinner=False)  # cache matches refresh interval
def fetch_oi_spurts():
    session = get_session()
    resp = session.get(API_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def bullish_dataframe(data):
    """Filters for Rise in OI + Rise in Price. Adjust field names if NSE
    changes their JSON schema (use the raw JSON expander in the app to check)."""
    rows = data.get("data", data) if isinstance(data, dict) else data
    records = []

    for r in rows:
        try:
            oi_pct = float(r.get("changeInOI_Perc", r.get("perChange", 0)))
            price_pct = float(r.get("changeInPrice_Perc", r.get("pChange", 0)))
        except (TypeError, ValueError):
            continue

        if oi_pct > 0 and price_pct > 0:
            records.append({
                "Symbol": r.get("symbol", r.get("underlying", "")),
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

# Triggers an automatic app rerun every 30 minutes
refresh_count = st_autorefresh(interval=REFRESH_INTERVAL_MS, key="oi_autorefresh")

top_n = st.slider("How many stocks to show?", min_value=3, max_value=20, value=5)
manual_refresh = st.button("Refresh now", type="secondary")

if manual_refresh:
    fetch_oi_spurts.clear()  # bypass cache on manual click

with st.spinner("Fetching from NSE..."):
    try:
        raw = fetch_oi_spurts()
        df = bullish_dataframe(raw)

        st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  Auto-refresh #{refresh_count}  •  Next refresh in ~30 min")

        if df.empty:
            st.warning("No stocks matched Rise in OI + Rise in Price right now.")
        else:
            st.success(f"Found {len(df)} bullish stocks. Showing top {top_n}.")
            st.dataframe(df.head(top_n), use_container_width=True, hide_index=True)

        with st.expander("Raw API response (debug)"):
            st.json(raw)

    except requests.exceptions.HTTPError as e:
        st.error(f"NSE blocked the request ({e}). Try again in a few seconds.")
    except Exception as e:
        st.error(f"Something went wrong: {e}")

st.divider()
st.caption("⚠️ Informational only, not investment advice. Best run during market hours (9:15 AM–3:30 PM IST).")
