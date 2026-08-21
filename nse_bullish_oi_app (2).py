"""
NSE Bullish OI Scraper — Streamlit App
Finds contracts with Rise in OI + Rise in Price (long build-up) using
NSE's confirmed "live-analysis-oi-spurts-contracts" endpoint, which
returns data pre-bucketed into:
  - Rise-in-OI-Rise   <- what we want (long build-up)
  - Rise-in-OI-Slide
  - Slide-in-OI-Rise
  - Slide-in-OI-Slide

This matches the "Rise in OI and Rise in Price" filter on
nseindia.com/market-data/oi-change exactly, with real pChangeInOI and
pChange fields already computed server-side — no extra requests needed.

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
OI_SPURTS_CONTRACTS_URL = f"{BASE}/api/live-analysis-oi-spurts-contracts"
TARGET_BUCKET = "Rise-in-OI-Rise"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/market-data/oi-change",
}

REFRESH_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes


def get_session():
    """NSE requires cookies from a normal page load first, or API calls 401/403."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE, timeout=10)
    time.sleep(1)
    session.get(f"{BASE}/market-data/oi-change", timeout=10)
    time.sleep(1)
    return session


@st.cache_data(ttl=30 * 60, show_spinner=False)
def fetch_rise_in_oi_rise():
    session = get_session()
    resp = session.get(OI_SPURTS_CONTRACTS_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    rows = []
    for bucket_obj in data.get("data", []):
        if TARGET_BUCKET in bucket_obj:
            rows = bucket_obj[TARGET_BUCKET]
            break

    return rows, data


def build_dataframe(rows):
    records = []
    for r in rows:
        records.append({
            "Symbol": r.get("symbol"),
            "Instrument": r.get("instrument"),
            "Expiry": r.get("expiryDate"),
            "Strike": r.get("strikePrice"),
            "Type": r.get("optionType"),
            "OI": r.get("latestOI"),
            "Change in OI": r.get("changeInOI"),
            "% Chg in OI": r.get("pChangeInOI"),
            "LTP": r.get("ltp"),
            "Prev Close": r.get("prevClose"),
            "% Chg Price": r.get("pChange"),
            "Underlying Price": r.get("underlyingValue"),
        })
    df = pd.DataFrame(records)
    if not df.empty:
        df = df.sort_values("Change in OI", ascending=False).reset_index(drop=True)
    return df


# ---------------- UI ----------------

st.set_page_config(page_title="NSE Bullish OI Finder", layout="wide")
st.title("📈 NSE Bullish OI Finder")
st.caption("Rise in OI + Rise in Price → possible long build-up by big players")

refresh_count = st_autorefresh(interval=REFRESH_INTERVAL_MS, key="oi_autorefresh")

top_n = st.slider("How many rows to show?", min_value=3, max_value=30, value=5)
manual_refresh = st.button("Refresh now", type="secondary")

if manual_refresh:
    fetch_rise_in_oi_rise.clear()

with st.spinner("Fetching from NSE..."):
    try:
        rows, raw = fetch_rise_in_oi_rise()

        st.caption(
            f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
            f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min  •  "
            f"NSE timestamp: {raw.get('timestamp', 'n/a')}"
        )

        if not rows:
            st.warning("No contracts currently in the 'Rise in OI and Rise in Price' bucket.")
        else:
            df = build_dataframe(rows)
            st.success(f"Found {len(df)} contracts. Showing top {top_n} by Change in OI.")
            st.dataframe(df.head(top_n), use_container_width=True, hide_index=True)

        with st.expander("Raw API response (debug)"):
            st.json(raw)

    except requests.exceptions.HTTPError as e:
        st.error(f"NSE blocked the request ({e}). Try again in a few seconds.")
    except Exception as e:
        st.error(f"Something went wrong: {e}")

st.divider()
st.caption("⚠️ Informational only, not investment advice. Best run during market hours (9:15 AM–3:30 PM IST).")
