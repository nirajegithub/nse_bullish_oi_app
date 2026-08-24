"""
NSE OI Trend Finder — Streamlit App
Classifies F&O contracts into the four standard OI/Price trend buckets,
then enriches the top rows with historical technicals: Volume Spike,
RSI(14), 20-day SMA, and consecutive "days up" streak.

Endpoints used:
  - live-analysis-oi-spurts-contracts  -> OI/price trend buckets
  - historical/cm/equity               -> daily OHLCV history per symbol
    (used to compute RSI, SMA, volume spike, days-up streak)

These are descriptive stats based on current/historical data — not trade
signals, entries, stop-losses, or targets. Always do your own risk
management.

Auto-refreshes every 30 minutes.

Run locally (needs internet access to nseindia.com):

    pip install streamlit requests pandas streamlit-autorefresh
    streamlit run nse_bullish_oi_app.py
"""

import time
from datetime import datetime, timedelta

import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BASE = "https://www.nseindia.com"
OI_SPURTS_CONTRACTS_URL = f"{BASE}/api/live-analysis-oi-spurts-contracts"
HISTORICAL_URL = f"{BASE}/api/historical/cm/equity"
VOLUME_GAINERS_URL = f"{BASE}/api/live-analysis-volume-gainers"

BUCKET_INFO = {
    "Rise-in-OI-Rise": ("Rise in OI + Rise in Price", "Long Buildup", "🟢"),
    "Rise-in-OI-Slide": ("Rise in OI + Fall in Price", "Short Buildup", "🔴"),
    "Slide-in-OI-Rise": ("Fall in OI + Rise in Price", "Short Covering", "🟡"),
    "Slide-in-OI-Slide": ("Fall in OI + Fall in Price", "Long Unwinding", "🟠"),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/market-data/oi-change",
}

REFRESH_INTERVAL_MS = 15 * 60 * 1000  # 30 minutes
REQUEST_DELAY_SEC = 0.4

# Candidate field names for the historical endpoint (schema unverified from
# my sandbox — kept flexible so it degrades gracefully if names differ).
CLOSE_KEYS = ["CH_CLOSING_PRICE", "closePrice", "close"]
VOLUME_KEYS = ["CH_TOT_TRADED_QTY", "totalTradedQuantity", "volume"]
DATE_KEYS = ["CH_TIMESTAMP", "mTIMESTAMP", "date"]


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


def safe_get(session, url, retries=3, backoff=1.5, **kwargs):
    """GET with retry-and-backoff. NSE occasionally blocks or times out on a
    single attempt; a couple of retries with increasing delay recovers from
    most transient failures without the user having to click refresh."""
    last_error = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=10, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_error


@st.cache_data(ttl=15 * 60, show_spinner=False)
def fetch_all_buckets():
    session = get_session()
    resp = safe_get(session, OI_SPURTS_CONTRACTS_URL)
    data = resp.json()

    buckets = {}
    for bucket_obj in data.get("data", []):
        for key, rows in bucket_obj.items():
            buckets[key] = rows

    return buckets, data


@st.cache_data(ttl=30 * 60, show_spinner=False)
def fetch_technicals(symbols):
    """Fetch ~90 calendar days of daily history per symbol and compute
    RSI(14), SMA(20), volume spike ratio, and consecutive days-up streak."""
    session = get_session()
    today = datetime.now()
    frm = (today - timedelta(days=120)).strftime("%d-%m-%Y")
    to = today.strftime("%d-%m-%Y")

    results = {}
    debug_first_raw = None

    for symbol in symbols:
        try:
            resp = safe_get(
                session,
                HISTORICAL_URL,
                retries=2,
                params={"symbol": symbol, "series": '["EQ"]', "from": frm, "to": to},
            )
            payload = resp.json()
            rows = payload.get("data", [])
            if debug_first_raw is None and rows:
                debug_first_raw = {"symbol": symbol, "sample": rows[:2]}

            parsed = []
            for r in rows:
                close = first_present(r, CLOSE_KEYS)
                vol = first_present(r, VOLUME_KEYS)
                date = first_present(r, DATE_KEYS)
                if close is not None and vol is not None:
                    parsed.append({"date": date, "close": float(close), "volume": float(vol)})

            # Sort oldest -> newest (NSE usually returns newest first)
            parsed.sort(key=lambda x: x["date"] or "")

            if len(parsed) < 15:
                results[symbol] = None
            else:
                results[symbol] = compute_technicals(parsed)

        except Exception:
            results[symbol] = None
        time.sleep(REQUEST_DELAY_SEC)

    return results, debug_first_raw


def compute_technicals(parsed):
    closes = [p["close"] for p in parsed]
    volumes = [p["volume"] for p in parsed]

    # RSI(14) - Wilder's smoothing
    rsi = None
    if len(closes) >= 15:
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        avg_gain = sum(gains[:14]) / 14
        avg_loss = sum(losses[:14]) / 14
        for i in range(14, len(deltas)):
            avg_gain = (avg_gain * 13 + gains[i]) / 14
            avg_loss = (avg_loss * 13 + losses[i]) / 14
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

    # SMA(20)
    sma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 5 else None

    # Volume spike: today's volume vs avg of prior 20 days (excluding today)
    vol_spike = None
    if len(volumes) >= 6:
        today_vol = volumes[-1]
        prior = volumes[max(0, len(volumes) - 21):-1]
        if prior:
            avg_prior_vol = sum(prior) / len(prior)
            if avg_prior_vol > 0:
                vol_spike = today_vol / avg_prior_vol

    # Consecutive "days up" streak (close > previous close), most recent backward
    days_up = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            days_up += 1
        else:
            break

    return {
        "RSI(14)": round(rsi, 1) if rsi is not None else None,
        "SMA20": round(sma20, 2) if sma20 is not None else None,
        "Volume Spike (x avg)": round(vol_spike, 2) if vol_spike is not None else None,
        "Days Up Streak": days_up,
    }


@st.cache_data(ttl=30 * 60, show_spinner=False)
def fetch_volume_gainers():
    """Confirmed-working NSE endpoint: today's volume vs 1-week and 2-week
    average volume, computed server-side by NSE. This is the reliable
    volume-spike source (unlike the historical-derived calc above)."""
    session = get_session()
    resp = safe_get(session, VOLUME_GAINERS_URL)
    data = resp.json()
    rows = data.get("data", [])
    return rows, data


def build_volume_gainers_df(rows, sort_col="Volume Spike vs 1wk (%)"):
    records = []
    for r in rows:
        symbol = r.get("symbol")
        records.append({
            "Symbol": symbol,
            "Company": r.get("companyName"),
            "LTP": r.get("ltp"),
            "% Chg Price": r.get("pChange"),
            "Volume": r.get("volume"),
            "1wk Avg Volume": r.get("week1AvgVolume"),
            "Volume Spike vs 1wk (%)": r.get("week1volChange"),
            "2wk Avg Volume": r.get("week2AvgVolume"),
            "Volume Spike vs 2wk (%)": r.get("week2volChange"),
            "Turnover (₹ Cr)": r.get("turnover"),
            "Chart": tradingview_url(symbol),
        })
    df = pd.DataFrame(records)
    if not df.empty and sort_col in df.columns:
        df = df.sort_values(sort_col, ascending=False).reset_index(drop=True)
    return df


def format_nse_timestamp(raw_dict):
    """NSE timestamps look like '21-Aug-2026 15:40:13'. Falls back gracefully
    if the format ever changes."""
    ts = raw_dict.get("timestamp") if isinstance(raw_dict, dict) else None
    return ts or "unknown"


def show_freshness_banner(raw_dict):
    nse_ts = format_nse_timestamp(raw_dict)
    fetched_at = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    st.info(f"📅 **Data as of (NSE):** {nse_ts}  •  🔄 **Fetched by app at:** {fetched_at}", icon="🕒")


def tradingview_url(symbol):
    """Build a TradingView chart link for an NSE symbol. TradingView's free
    tier doesn't support pre-loading specific indicators via URL, but EMA 9,
    Pivot Points Standard, and Fib Retracement are all built-in indicators
    the user can add themselves in a couple of clicks once the chart opens."""
    if not symbol:
        return None
    return f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"


def build_dataframe(rows, bucket_key, sort_col="Change in OI"):
    label, trend, emoji = BUCKET_INFO.get(bucket_key, (bucket_key, bucket_key, ""))
    records = []
    for r in rows:
        symbol = r.get("symbol")
        records.append({
            "Symbol": symbol,
            "Trend": f"{emoji} {trend}",
            "Expiry": r.get("expiryDate"),
            "Strike": r.get("strikePrice"),
            "Type": r.get("optionType"),
            "Change in OI": r.get("changeInOI"),
            "% Chg in OI": r.get("pChangeInOI"),
            "LTP": r.get("ltp"),
            "% Chg Price": r.get("pChange"),
            "Volume": r.get("volume"),
            "Chart": tradingview_url(symbol),
        })
    df = pd.DataFrame(records)
    if not df.empty and sort_col in df.columns:
        df = df.sort_values(sort_col, key=lambda s: s.abs(), ascending=False).reset_index(drop=True)
    return df


def style_change_columns(df):
    """Color % change columns green/red based on sign, for faster scanning."""
    color_cols = [c for c in ["% Chg in OI", "% Chg Price", "Volume Spike vs 1wk (%)"] if c in df.columns]

    def colorize(val):
        if pd.isna(val):
            return ""
        if val > 0:
            return "color: #1a7f37; font-weight: 600;"
        if val < 0:
            return "color: #cf222e; font-weight: 600;"
        return ""

    return df.style.map(colorize, subset=color_cols) if color_cols else df


# ---------------- UI ----------------

st.set_page_config(page_title="NSE OI Trend Finder", layout="wide")

st.markdown(
    """
    <style>
        .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
        h1 { font-size: 1.5rem !important; margin-bottom: 0.2rem !important; }
        h3 { font-size: 1.05rem !important; }
        .stCaption, .st-emotion-cache-1629p8f p { font-size: 0.8rem !important; }
        div[data-testid="stMetricValue"] { font-size: 1.1rem !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📈 NSE OI Trend Finder")
st.caption("OI + Price trend classification, enriched with RSI, SMA, volume spike & days-up streak")

refresh_count = st_autorefresh(interval=REFRESH_INTERVAL_MS, key="oi_autorefresh")

view_options = {
    "🟢 Long Buildup (Rise in OI + Rise in Price)": "Rise-in-OI-Rise",
    "🔴 Short Buildup (Rise in OI + Fall in Price)": "Rise-in-OI-Slide",
    "🟡 Short Covering (Fall in OI + Rise in Price)": "Slide-in-OI-Rise",
    "🟠 Long Unwinding (Fall in OI + Fall in Price)": "Slide-in-OI-Slide",
    "🔊 Volume Gainers (today vs 1wk/2wk avg)": "VOLUME_GAINERS",
}
selected_view = st.selectbox("View", list(view_options.keys()), label_visibility="collapsed")
bucket_key = view_options[selected_view]
is_volume_view = bucket_key == "VOLUME_GAINERS"

with st.expander("⚙️ Filters", expanded=False):
    top_n = st.slider("Rows to show", min_value=3, max_value=25, value=10)
    col1, col2 = st.columns(2)
    with col1:
        min_volume = st.number_input("Min Volume", min_value=0, value=500000, step=50000)
    with col2:
        min_price = st.number_input("Min Price (LTP)", min_value=0.0, value=250.0, step=10.0)

    if is_volume_view:
        sort_col = st.selectbox(
            "Sort by",
            ["Volume Spike vs 1wk (%)", "Volume Spike vs 2wk (%)", "% Chg Price", "Volume"],
        )
    else:
        sort_col = st.selectbox("Sort by", ["Change in OI", "% Chg in OI", "% Chg Price", "Volume"])

    show_technicals = False
    if not is_volume_view:
        show_technicals = st.checkbox("Add RSI/SMA/Days-Up (experimental, slower)", value=False)

manual_refresh = st.button("🔄 Refresh now")

if manual_refresh:
    fetch_all_buckets.clear()
    fetch_technicals.clear()
    fetch_volume_gainers.clear()

with st.spinner("Fetching from NSE..."):
    try:
        if is_volume_view:
            vg_rows, vg_raw = fetch_volume_gainers()
            show_freshness_banner(vg_raw)
            st.caption(f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min")
            if not vg_rows:
                st.warning("No volume-gainer data returned right now.")
            else:
                vg_df_full = build_volume_gainers_df(vg_rows, sort_col=sort_col)
                vg_df_filtered = vg_df_full[
                    (vg_df_full["Volume"] >= min_volume) & (vg_df_full["LTP"] >= min_price)
                ]
                vg_df = vg_df_filtered.head(top_n)
                st.caption(
                    f"Filters applied: Volume ≥ {min_volume:,} and Price ≥ ₹{min_price:,.0f}  •  "
                    f"{len(vg_df_filtered)} of {len(vg_df_full)} rows match."
                )
                if vg_df.empty:
                    st.warning("No rows match your Volume/Price filters. Try lowering them.")
                else:
                    st.success(f"Showing top {len(vg_df)}, sorted by {sort_col}.")
                    st.dataframe(
                        style_change_columns(vg_df),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Chart": st.column_config.LinkColumn("Chart", display_text="📊 Open Chart")
                        },
                    )
            raw = vg_raw  # for the debug expander below

        else:
            buckets, raw = fetch_all_buckets()
            tech_debug = None
            show_freshness_banner(raw)
            st.caption(f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min")

            rows = buckets.get(bucket_key, [])
            if not rows:
                st.warning(f"No contracts currently in the '{selected_view}' bucket.")
            else:
                df_full = build_dataframe(rows, bucket_key, sort_col=sort_col)
                df_filtered = df_full[
                    (df_full["Volume"] >= min_volume) & (df_full["LTP"] >= min_price)
                ]
                st.caption(
                    f"Filters applied: Volume ≥ {min_volume:,} and Price ≥ ₹{min_price:,.0f}  •  "
                    f"{len(df_filtered)} of {len(df_full)} rows match."
                )

                if df_filtered.empty:
                    st.warning("No rows match your Volume/Price filters. Try lowering them.")
                else:
                    df_top = df_filtered.head(top_n).copy()

                    # Join in confirmed-real volume-spike data where the symbol
                    # also appears in NSE's volume-gainers list. Only add the
                    # column if at least one row actually has a value — otherwise
                    # it's just a blank column cluttering the grid.
                    try:
                        vg_rows, _ = fetch_volume_gainers()
                        vg_lookup = {r.get("symbol"): r for r in vg_rows}
                        spike_col = df_top["Symbol"].map(
                            lambda s: vg_lookup.get(s, {}).get("week1volChange")
                        )
                        if spike_col.notna().any():
                            df_top["Volume Spike vs 1wk (%)"] = spike_col
                    except Exception:
                        pass

                    tech_debug = None
                    if show_technicals and not df_top.empty:
                        unique_symbols = tuple(sorted(set(df_top["Symbol"].dropna())))
                        with st.spinner(f"Fetching price history for {len(unique_symbols)} symbols..."):
                            tech_results, tech_debug = fetch_technicals(unique_symbols)

                        for col in ["RSI(14)", "SMA20", "Days Up Streak"]:
                            col_data = df_top["Symbol"].map(
                                lambda s: (tech_results.get(s) or {}).get(col)
                            )
                            if col_data.notna().any():
                                df_top[col] = col_data

                        missing = [s for s in unique_symbols if tech_results.get(s) is None]
                        if missing:
                            st.caption(f"⚠️ Couldn't compute RSI/SMA for: {', '.join(missing)} (unverified endpoint — see debug)")

                    st.success(f"Showing top {len(df_top)} of {len(df_filtered)} matching contracts, sorted by {sort_col}.")
                    st.dataframe(
                        style_change_columns(df_top),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Chart": st.column_config.LinkColumn("Chart", display_text="📊 Open Chart")
                        },
                    )
                    st.caption(
                        "Charts open on TradingView. EMA 9, Pivot Points Standard, and Fib Retracement are "
                        "built-in indicators there — add them via the chart's indicator search (they aren't "
                        "preset, since TradingView's free tier doesn't support that via URL)."
                    )
                    st.caption(
                        "Volume Spike here is blank unless the symbol is also in NSE's top volume-gainers "
                        "list today — switch to the '🔊 Volume Gainers' view for the full ranked list."
                    )

        st.divider()
        with st.expander("ℹ️ What these mean"):
            st.markdown(
                "- 🟢 **Long Buildup** — new money entering long positions; often read as bullish continuation\n"
                "- 🔴 **Short Buildup** — new money entering short positions; often read as bearish continuation\n"
                "- 🟡 **Short Covering** — shorts closing out as price rises; can reverse quickly once covering ends\n"
                "- 🟠 **Long Unwinding** — longs exiting as price falls; often profit-booking or stop-outs\n"
                "- **Volume Spike vs 1wk/2wk (%)** — how much today's volume exceeds the recent average, computed by NSE directly\n"
                "- **RSI(14)** *(experimental)* — momentum: traditionally >70 = overbought, <30 = oversold\n"
                "- **SMA20** *(experimental)* — 20-day average closing price, a common trend reference line\n"
                "- **Days Up Streak** *(experimental)* — consecutive daily closes higher than the previous close"
            )
            st.caption(
                "These are descriptive statistics from current and historical NSE data — not entry/exit "
                "signals, stop-losses, or targets. Combine with your own analysis and risk management."
            )

        with st.expander("🛠️ Debug: raw API response"):
            st.json(raw)
            if not is_volume_view and show_technicals and tech_debug:
                st.markdown("**Raw historical API sample (experimental endpoint):**")
                st.json(tech_debug)

    except requests.exceptions.HTTPError as e:
        st.error(f"NSE blocked the request ({e}). Try again in a few seconds.")
    except Exception as e:
        st.error(f"Something went wrong: {e}")

st.caption("⚠️ Informational only, not investment advice. Best run during market hours (9:15 AM–3:30 PM IST).")
