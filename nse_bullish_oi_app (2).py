"""
NSE Bullish OI Scraper — Streamlit App
Finds stocks with Rise in OI + Rise in Price (long build-up) by calling
NSE's own "Change in Open Interest" endpoint directly — the same one that
powers nseindia.com/market-data/oi-change with the "Rise in OI and Rise
in Price" filter already applied server-side.

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
# Same "liveEquity-derivatives" family used for Most Active Contracts
# (index=top20_contracts). The "Change in Open Interest" page's dropdown
# uses view=Rise-in-OI-Rise for exactly the filter we want.
CHANGE_IN_OI_URL = f"{BASE}/api/live-analysis-oi-spurts-underlyings"  # fallback, replaced below
CANDIDATE_URLS = [
    f"{BASE}/api/liveEquity-derivatives?index=Rise-in-OI-Rise",
    f"{BASE}/api/live-analysis-changeInOI?view=Rise-in-OI-Rise",
    f"{BASE}/api/change-in-oi?view=Rise-in-OI-Rise",
]

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

# Possible field-name variants NSE might use — we try each until one matches,
# since I can't verify the exact schema of this endpoint from my sandbox.
SYMBOL_KEYS = ["symbol", "underlying"]
INSTRUMENT_KEYS = ["instrumentType", "instrument"]
EXPIRY_KEYS = ["expiryDate", "expiry"]
STRIKE_KEYS = ["strikePrice"]
OPTTYPE_KEYS = ["optionType"]
OI_KEYS = ["openInterest", "latestOI"]
CHG_OI_KEYS = ["changeInOI", "chngInOI"]
PCHG_OI_KEYS = ["pchangeinOpenInterest", "changeInOIPercentage", "avgInOI", "pChangeInOI"]
LTP_KEYS = ["lastPrice", "ltp"]
PREV_CLOSE_KEYS = ["prevClose", "previousClose"]
PCHG_PRICE_KEYS = ["pChange", "changeInPricePercentage"]


def first_present(row, keys):
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE, timeout=10)
    time.sleep(1)
    session.get(f"{BASE}/market-data/oi-change", timeout=10)
    time.sleep(1)
    return session


@st.cache_data(ttl=30 * 60, show_spinner=False)
def fetch_bullish_data():
    """
    Tries each candidate URL for the 'Change in Open Interest' /
    'Rise in OI and Rise in Price' endpoint until one returns usable data.
    Returns (dataframe, raw_json, url_used, errors_by_url).
    """
    session = get_session()
    errors = {}

    for url in CANDIDATE_URLS:
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(rows, dict):
                flat = []
                for v in rows.values():
                    if isinstance(v, list):
                        flat.extend(v)
                rows = flat
            if rows:
                return rows, data, url, errors
        except Exception as e:
            errors[url] = str(e)
            time.sleep(0.5)

    return [], {}, None, errors


def build_dataframe(rows):
    records = []
    for r in rows:
        symbol = first_present(r, SYMBOL_KEYS)
        if not symbol:
            continue
        records.append({
            "Symbol": symbol,
            "Instrument": first_present(r, INSTRUMENT_KEYS),
            "Expiry": first_present(r, EXPIRY_KEYS),
            "Strike": first_present(r, STRIKE_KEYS),
            "Type": first_present(r, OPTTYPE_KEYS),
            "OI": first_present(r, OI_KEYS),
            "Change in OI": first_present(r, CHG_OI_KEYS),
            "% Chg in OI": first_present(r, PCHG_OI_KEYS),
            "LTP": first_present(r, LTP_KEYS),
            "Prev Close": first_present(r, PREV_CLOSE_KEYS),
        })
    df = pd.DataFrame(records)
    if not df.empty and "% Chg in OI" in df.columns:
        df["% Chg in OI"] = pd.to_numeric(df["% Chg in OI"], errors="coerce")
        df = df.sort_values("% Chg in OI", ascending=False).reset_index(drop=True)
    return df


# ---------------- UI ----------------

st.set_page_config(page_title="NSE Bullish OI Finder", layout="wide")
st.title("📈 NSE Bullish OI Finder")
st.caption("Rise in OI + Rise in Price → possible long build-up by big players")

refresh_count = st_autorefresh(interval=REFRESH_INTERVAL_MS, key="oi_autorefresh")

top_n = st.slider("How many rows to show?", min_value=3, max_value=30, value=5)
manual_refresh = st.button("Refresh now", type="secondary")

if manual_refresh:
    fetch_bullish_data.clear()

with st.spinner("Fetching from NSE..."):
    try:
        rows, raw, url_used, errors = fetch_bullish_data()

        st.caption(
            f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
            f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min"
        )

        if not rows:
            st.error(
                "Couldn't fetch data from any known endpoint variant. "
                "See 'Endpoint attempts (debug)' below for the exact errors — "
                "this usually means NSE changed the URL again, or is blocking "
                "this server's IP."
            )
        else:
            st.caption(f"Endpoint used: `{url_used}`")
            df = build_dataframe(rows)
            if df.empty:
                st.warning("Got a response, but couldn't parse recognizable fields. Check raw response below.")
            else:
                st.success(f"Found {len(df)} entries. Showing top {top_n}.")
                st.dataframe(df.head(top_n), use_container_width=True, hide_index=True)

        with st.expander("Endpoint attempts (debug)"):
            st.write("URLs tried, in order:")
            for u in CANDIDATE_URLS:
                if u == url_used:
                    st.write(f"✅ `{u}` — worked")
                elif u in errors:
                    st.write(f"❌ `{u}` — {errors[u]}")
                else:
                    st.write(f"⏭️ `{u}` — not tried (earlier one succeeded)")

        with st.expander("Raw API response (debug)"):
            st.json(raw)

    except Exception as e:
        st.error(f"Something went wrong: {e}")

st.divider()
st.caption("⚠️ Informational only, not investment advice. Best run during market hours (9:15 AM–3:30 PM IST).")
            
