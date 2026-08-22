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
import json
from datetime import datetime, timedelta

import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

BASE = "https://www.nseindia.com"
CHARTING_BASE = "https://charting.nseindia.com"
OI_SPURTS_CONTRACTS_URL = f"{BASE}/api/live-analysis-oi-spurts-contracts"
HISTORICAL_URL = f"{BASE}/api/historical/cm/equity"
VOLUME_GAINERS_URL = f"{BASE}/api/live-analysis-volume-gainers"
CHARTING_HISTORICAL_URL = f"{CHARTING_BASE}/v1/charts/symbolHistoricalData"

# Symbol -> NSE charting token. Confirmed working: HDFCBANK. To add more
# symbols, capture the token from NSE's own quote page (DevTools -> Network
# -> click the charting button -> find the request with that symbol's token)
# and add it here.
SYMBOL_TOKENS = {
    "HDFCBANK": 1333,
}

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

REFRESH_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes
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


@st.cache_data(ttl=30 * 60, show_spinner=False)
def fetch_all_buckets():
    session = get_session()
    resp = session.get(OI_SPURTS_CONTRACTS_URL, timeout=10)
    resp.raise_for_status()
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
            resp = session.get(
                HISTORICAL_URL,
                params={"symbol": symbol, "series": '["EQ"]', "from": frm, "to": to},
                timeout=10,
            )
            resp.raise_for_status()
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
    resp = session.get(VOLUME_GAINERS_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data", [])
    return rows, data


def build_volume_gainers_df(rows):
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
    if not df.empty:
        df = df.sort_values("Volume Spike vs 1wk (%)", ascending=False).reset_index(drop=True)
    return df


@st.cache_data(ttl=15 * 60, show_spinner=False)
def fetch_candles(symbol, token, days=180):
    """Fetch daily OHLCV candles from NSE's charting backend (confirmed
    working format: {status, data: [{time, open, high, low, close, volume}]}).
    `token` is NSE's internal numeric ID for the symbol — see SYMBOL_TOKENS."""
    now = datetime.now()
    from_ts = int((now - timedelta(days=days)).timestamp())
    to_ts = int(now.timestamp())

    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(
        CHARTING_HISTORICAL_URL,
        params={
            "fromDate": from_ts,
            "toDate": to_ts,
            "symbol": f"{symbol}-EQ",
            "token": token,
            "symbolType": "Equity",
            "chartType": "D",
            "timeInterval": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    candles = payload.get("data", [])
    # Sort oldest -> newest, dedupe by time just in case
    candles.sort(key=lambda c: c["time"])
    return candles


def compute_ema(values, period):
    """Standard EMA: seeded with SMA of the first `period` values."""
    if len(values) < period:
        return [None] * len(values)
    ema = [None] * (period - 1)
    sma = sum(values[:period]) / period
    ema.append(sma)
    multiplier = 2 / (period + 1)
    for v in values[period:]:
        ema.append((v - ema[-1]) * multiplier + ema[-1])
    return ema


def compute_pivot_points(prev_high, prev_low, prev_close):
    """Standard (classic) floor-trader pivot points using the prior day's H/L/C."""
    pp = (prev_high + prev_low + prev_close) / 3
    r1 = 2 * pp - prev_low
    s1 = 2 * pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    r3 = prev_high + 2 * (pp - prev_low)
    s3 = prev_low - 2 * (prev_high - pp)
    return {"PP": pp, "R1": r1, "R2": r2, "R3": r3, "S1": s1, "S2": s2, "S3": s3}


def compute_fibonacci(swing_high, swing_low):
    """Standard Fibonacci retracement levels between a recent swing high and low."""
    diff = swing_high - swing_low
    levels = {}
    for pct in [0, 23.6, 38.2, 50, 61.8, 78.6, 100]:
        levels[f"Fib {pct}%"] = swing_high - diff * (pct / 100)
    return levels


def render_lightweight_chart(candles, ema9, pivots, fib_levels, symbol, height=520):
    """Renders a candlestick chart using TradingView's open-source
    Lightweight Charts library, with EMA9 as an overlay line and
    pivot/fibonacci levels as horizontal price lines."""
    chart_candles = [
        {
            "time": c["time"] // 1000,  # library expects unix seconds
            "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
        }
        for c in candles
    ]
    ema_series = [
        {"time": c["time"] // 1000, "value": v}
        for c, v in zip(candles, ema9) if v is not None
    ]

    price_lines = []
    for label, value in pivots.items():
        color = "#2196F3" if label == "PP" else ("#4CAF50" if label.startswith("R") else "#F44336")
        price_lines.append({"price": value, "color": color, "title": label})
    for label, value in fib_levels.items():
        price_lines.append({"price": value, "color": "#9C27B0", "title": label})

    price_lines_js = json.dumps(price_lines)
    candles_js = json.dumps(chart_candles)
    ema_js = json.dumps(ema_series)

    html = f"""
    <div id="chart_container" style="width:100%; height:{height}px;"></div>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <script>
      const container = document.getElementById('chart_container');
      const chart = LightweightCharts.createChart(container, {{
        width: container.clientWidth,
        height: {height},
        layout: {{ background: {{ color: '#ffffff' }}, textColor: '#333' }},
        grid: {{ vertLines: {{ color: '#eee' }}, horzLines: {{ color: '#eee' }} }},
        timeScale: {{ timeVisible: false, borderColor: '#ccc' }},
      }});

      const candleSeries = chart.addCandlestickSeries({{
        upColor: '#26a69a', downColor: '#ef5350',
        borderVisible: false,
        wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      }});
      candleSeries.setData({candles_js});

      const emaSeries = chart.addLineSeries({{
        color: '#FF9800', lineWidth: 2, title: 'EMA 9',
      }});
      emaSeries.setData({ema_js});

      const priceLines = {price_lines_js};
      priceLines.forEach(function(pl) {{
        candleSeries.createPriceLine({{
          price: pl.price,
          color: pl.color,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title: pl.title,
        }});
      }});

      chart.timeScale().fitContent();
      new ResizeObserver(entries => {{
        chart.applyOptions({{ width: container.clientWidth }});
      }}).observe(container);
    </script>
    """
    components.html(html, height=height + 20)


def tradingview_url(symbol):
    """Build a TradingView chart link for an NSE symbol. TradingView's free
    tier doesn't support pre-loading specific indicators via URL, but EMA 9,
    Pivot Points Standard, and Fib Retracement are all built-in indicators
    the user can add themselves in a couple of clicks once the chart opens."""
    if not symbol:
        return None
    return f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"


def build_dataframe(rows, bucket_key):
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
    if not df.empty:
        df = df.sort_values("Change in OI", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)
    return df


# ---------------- UI ----------------

st.set_page_config(page_title="NSE OI Trend Finder", layout="wide")
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
selected_view = st.selectbox("Which view do you want to see?", list(view_options.keys()))
bucket_key = view_options[selected_view]
is_volume_view = bucket_key == "VOLUME_GAINERS"

top_n = st.slider("How many rows to show?", min_value=3, max_value=25, value=10)

col1, col2 = st.columns(2)
with col1:
    min_volume = st.number_input("Minimum Volume", min_value=0, value=500000, step=50000)
with col2:
    min_price = st.number_input("Minimum Price (LTP)", min_value=0.0, value=250.0, step=10.0)

show_technicals = False
if not is_volume_view:
    show_technicals = st.checkbox(
        "Add RSI/SMA/Days-Up (experimental — separate endpoint, unverified schema)", value=False
    )
manual_refresh = st.button("Refresh now", type="secondary")

if manual_refresh:
    fetch_all_buckets.clear()
    fetch_technicals.clear()
    fetch_volume_gainers.clear()

with st.spinner("Fetching from NSE..."):
    try:
        if is_volume_view:
            vg_rows, vg_raw = fetch_volume_gainers()
            st.caption(
                f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
                f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min  •  "
                f"NSE timestamp: {vg_raw.get('timestamp', 'n/a')}"
            )
            if not vg_rows:
                st.warning("No volume-gainer data returned right now.")
            else:
                vg_df_full = build_volume_gainers_df(vg_rows)
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
                    st.success(f"Showing top {len(vg_df)} by 1-week volume spike %.")
                    st.dataframe(
                        vg_df,
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

            st.caption(
                f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
                f"Auto-refresh #{refresh_count}  •  Next refresh in ~30 min  •  "
                f"NSE timestamp: {raw.get('timestamp', 'n/a')}"
            )

            rows = buckets.get(bucket_key, [])
            if not rows:
                st.warning(f"No contracts currently in the '{selected_view}' bucket.")
            else:
                df_full = build_dataframe(rows, bucket_key)
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

                    st.success(f"Showing top {len(df_top)} of {len(df_filtered)} matching contracts by |Change in OI|.")
                    st.dataframe(
                        df_top,
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
        st.subheader("🕯️ Candlestick Chart — EMA9 + Pivot Points + Fibonacci")
        st.caption(
            "Built from NSE's own charting data. Currently only symbols with a known chart "
            "token are supported (see note below) — this is a growing list."
        )

        available_symbols = sorted(SYMBOL_TOKENS.keys())
        chart_symbol = st.selectbox("Symbol", available_symbols, key="chart_symbol_select")

        if st.button("Load chart", key="load_chart_btn"):
            try:
                token = SYMBOL_TOKENS[chart_symbol]
                with st.spinner(f"Fetching {chart_symbol} price history..."):
                    candles = fetch_candles(chart_symbol, token, days=180)

                if len(candles) < 20:
                    st.warning("Not enough historical data returned to compute indicators.")
                else:
                    closes = [c["close"] for c in candles]
                    ema9 = compute_ema(closes, 9)

                    prev = candles[-2]  # prior completed day for pivot calc
                    pivots = compute_pivot_points(prev["high"], prev["low"], prev["close"])

                    lookback = candles[-60:] if len(candles) >= 60 else candles
                    swing_high = max(c["high"] for c in lookback)
                    swing_low = min(c["low"] for c in lookback)
                    fib_levels = compute_fibonacci(swing_high, swing_low)

                    render_lightweight_chart(candles, ema9, pivots, fib_levels, chart_symbol)

                    last_close = closes[-1]
                    last_ema9 = ema9[-1]
                    if last_ema9 is not None:
                        cross_note = "above" if last_close > last_ema9 else "below"
                        st.caption(
                            f"Latest close ₹{last_close:.2f} is currently **{cross_note} EMA9** "
                            f"(₹{last_ema9:.2f}). Pivot (PP): ₹{pivots['PP']:.2f}."
                        )
            except requests.exceptions.HTTPError as e:
                st.error(f"NSE blocked the chart data request ({e}). Try again shortly.")
            except Exception as e:
                st.error(f"Couldn't load chart: {e}")

        st.caption(
            f"⚙️ Only {len(SYMBOL_TOKENS)} symbol(s) currently supported: {', '.join(sorted(SYMBOL_TOKENS.keys()))}. "
            "To add a symbol, its NSE charting 'token' needs to be captured once via browser DevTools "
            "and added to the app's SYMBOL_TOKENS list."
        )

        st.divider()
        st.markdown("**What these mean:**")
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

        with st.expander("Raw API response (debug)"):
            st.json(raw)
        if not is_volume_view and show_technicals and tech_debug:
            with st.expander("Raw historical API sample (debug) — experimental endpoint"):
                st.json(tech_debug)

    except requests.exceptions.HTTPError as e:
        st.error(f"NSE blocked the request ({e}). Try again in a few seconds.")
    except Exception as e:
        st.error(f"Something went wrong: {e}")

st.divider()
st.caption("⚠️ Informational only, not investment advice. Best run during market hours (9:15 AM–3:30 PM IST).")
