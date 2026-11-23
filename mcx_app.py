import time
import math
import requests
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st
import pyotp
from SmartApi import SmartConnect

# ================= PAGE CONFIG =================
st.set_page_config(
    page_title="MCX Crude & Natural Gas Pro",
    page_icon="🛢️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 1rem; padding-left: 0.5rem; padding-right: 0.5rem; }
    .decision-box { padding: 15px; border-radius: 10px; text-align: center; font-weight: bold; margin-bottom: 12px; color: #000; }
    .bullish { background-color: #00cc66; border: 2px solid #008040; }
    .bearish { background-color: #ff6666; border: 2px solid #b30000; color: #fff; }
    .neutral { background-color: #ffeb9c; border: 2px solid #cc9900; }
    .stDataFrame { font-size: 13px; }
</style>
""", unsafe_allow_html=True)

# ================= CREDENTIALS =================
API_KEY = st.secrets.get("API_KEY", "voT6BGPU")
CLIENT_CODE = st.secrets.get("CLIENT_CODE", "AAAA317311")
PASSWORD = st.secrets.get("PASSWORD", "7047")
TOTP_SECRET = st.secrets.get("TOTP_SECRET", "IW4DZWTJ2CAMBGMP2A7WOTLRNI")

# ================= BLACK-SCHOLES IV & DELTA =================
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_price(S, K, T, r, sigma, opt_type):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return (S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)) if opt_type == "CE" else (K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1))

def calc_iv_and_delta(S, K, T_days, market_price, opt_type):
    T = max(T_days, 0.25) / 365.0
    r = 0.07
    if market_price <= 0.2:
        return 0, (1.0 if (opt_type == "CE" and S > K) else (-1.0 if (opt_type == "PE" and S < K) else 0.0))
    low, high, iv = 0.01, 3.5, 0.25
    for _ in range(20):
        iv = (low + high) / 2.0
        if bs_price(S, K, T, r, iv, opt_type) > market_price:
            high = iv
        else:
            low = iv
    d1 = (math.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
    delta = norm_cdf(d1) if opt_type == "CE" else (norm_cdf(d1) - 1.0)
    return int(round(iv * 100)), round(delta, 2)

# ================= CACHED SESSION & MCX MASTER =================
@st.cache_resource(ttl=14400)
def get_smartapi_session():
    smartApi = SmartConnect(API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    session = smartApi.generateSession(CLIENT_CODE, PASSWORD, totp)
    return smartApi if session.get("status") else None

@st.cache_data(ttl=86400)
def get_mcx_master(commodity_name):
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    df = pd.DataFrame(requests.get(url, timeout=25).json())
    mcx_df = df[(df['exch_seg'] == 'MCX') & (df['name'] == commodity_name)].copy()
    mcx_df['expiry_dt'] = pd.to_datetime(mcx_df['expiry'], format='%d%b%Y')
    today = pd.Timestamp.today().normalize()

    # Nearest Future Contract (Spot/Future Reference ke liye)
    futs = mcx_df[(mcx_df['instrumenttype'] == 'FUTCOM') & (mcx_df['expiry_dt'] >= today)].sort_values('expiry_dt')
    nearest_fut = futs.iloc[0]

    # Nearest Option Chain Contracts
    opts = mcx_df[(mcx_df['instrumenttype'] == 'OPTFUT') & (mcx_df['expiry_dt'] >= today)].copy()
    nearest_opt_exp = opts['expiry_dt'].min()
    current_opts = opts[opts['expiry_dt'] == nearest_opt_exp].copy()
    current_opts['strike_price'] = (current_opts['strike'].astype(float) / 100.0)
    days_to_exp = max(1, (nearest_opt_exp - today).days)

    return nearest_fut, current_opts, nearest_opt_exp.strftime('%d-%b-%Y'), days_to_exp

def get_mcx_technicals(_smartApi, fut_token):
    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=3)
        params = {
            "exchange": "MCX",
            "symboltoken": str(fut_token),
            "interval": "FIVE_MINUTE",
            "fromdate": from_date.strftime("%Y-%m-%d 09:00"),
            "todate": to_date.strftime("%Y-%m-%d 23:30")
        }
        res = _smartApi.getCandleData(params)
        if not res.get("status") or not res.get("data"):
            return None

        df = pd.DataFrame(res["data"], columns=["time", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)

        # RSI (14)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi_val = int(round((100 - (100 / (1 + rs))).iloc[-1]))

        # Intraday VWAP & Fibonacci
        df['date'] = pd.to_datetime(df['time']).dt.date
        today_df = df[df['date'] == df['date'].iloc[-1]].copy()
        today_df['tp'] = (today_df['high'] + today_df['low'] + today_df['close']) / 3
        vwap_val = round(today_df['tp'].expanding().mean().iloc[-1], 1)

        swing_high = float(today_df['high'].max())
        swing_low = float(today_df['low'].min())
        diff = max(swing_high - swing_low, 1.0)
        fib_382 = round(swing_high - (0.382 * diff), 1)
        fib_500 = round(swing_high - (0.500 * diff), 1)
        fib_618 = round(swing_high - (0.618 * diff), 1)

        # Supertrend (10, 2)
        hl2 = (df['high'] + df['low']) / 2
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(10).mean()
        upperband = hl2 + (2.0 * atr)
        lowerband = hl2 - (2.0 * atr)
        in_uptrend = [True] * len(df)

        for i in range(1, len(df)):
            if df['close'].iloc[i] > upperband.iloc[i - 1]:
                in_uptrend[i] = True
            elif df['close'].iloc[i] < lowerband.iloc[i - 1]:
                in_uptrend[i] = False
            else:
                in_uptrend[i] = in_uptrend[i - 1]
                if in_uptrend[i] and lowerband.iloc[i] < lowerband.iloc[i - 1]:
                    lowerband.iloc[i] = lowerband.iloc[i - 1]
                if not in_uptrend[i] and upperband.iloc[i] > upperband.iloc[i - 1]:
                    upperband.iloc[i] = upperband.iloc[i - 1]

        return {
            "rsi": rsi_val, "vwap": vwap_val, "st_bullish": in_uptrend[-1],
            "fib_382": fib_382, "fib_500": fib_500, "fib_618": fib_618
        }
    except Exception:
        return None

# ================= MAIN APP =================
def main():
    st.title("🛢️ MCX Live Option Chain Pro (Crude Oil & Natural Gas)")

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        commodity = st.selectbox("Commodity Chunein:", ["CRUDEOIL", "NATURALGAS"])
    with c2:
        auto_refresh = st.toggle("🔄 Auto-Refresh Live Data (Har 5 Sec)", value=True)
    with c3:
        if st.button("Refresh Now"):
            st.rerun()

    step = 50 if commodity == "CRUDEOIL" else 5
    is_ng = (commodity == "NATURALGAS")

    smartApi = get_smartapi_session()
    if not smartApi:
        st.error("Angel One Login Failed! Secrets check karein.")
        return

    fut_row, opts_df, expiry_str, days_to_exp = get_mcx_master(commodity)
    fut_token = fut_row['token']
    fut_symbol = fut_row['symbol']

    fut_res = smartApi.ltpData("MCX", fut_symbol, str(fut_token))
    if not fut_res.get("status"):
        time.sleep(2)
        st.rerun()

    fut_ltp = round(float(fut_res["data"]["ltp"]), 1) if is_ng else int(round(float(fut_res["data"]["ltp"])))
    atm_strike = round(fut_ltp / step) * step
    if not is_ng:
        atm_strike = int(atm_strike)

    strikes = [round(atm_strike + (i * step), 1) if is_ng else int(atm_strike + (i * step)) for i in range(-7, 8)]

    sub = opts_df[opts_df['strike_price'].isin(strikes)]
    tokens = sub['token'].tolist()
    token_map = dict(zip(sub['token'], zip(sub['strike_price'], sub['symbol'])))

    mkt_res = smartApi.getMarketData("FULL", {"MCX": tokens})
    if not mkt_res.get("status"):
        time.sleep(2)
        st.rerun()

    fetched = mkt_res["data"].get("fetched", [])
    chain = {k: {"CE": {}, "PE": {}} for k in strikes}

    for item in fetched:
        tok = item.get("symbolToken")
        if tok not in token_map:
            continue
        k, sym = token_map[tok]
        k = round(k, 1) if is_ng else int(k)
        side = "CE" if sym.endswith("CE") else "PE"
        ltp_val = round(float(item.get("ltp", 0)), 1) if is_ng else int(round(float(item.get("ltp", 0))))
        chain[k][side] = {
            "ltp": ltp_val,
            "oi": int(item.get("opnInterest", 0)),
            "vol": int(item.get("tradeVolume", 0))
        }

    ce_ois = {k: chain[k]["CE"].get("oi", 0) for k in strikes}
    pe_ois = {k: chain[k]["PE"].get("oi", 0) for k in strikes}
    max_ce_oi_k = max(ce_ois, key=ce_ois.get)
    max_pe_oi_k = max(pe_ois, key=pe_ois.get)

    major_eor = round(max_ce_oi_k + chain[max_ce_oi_k]["CE"].get("ltp", 0), 1) if is_ng else int(round(max_ce_oi_k + chain[max_ce_oi_k]["CE"].get("ltp", 0)))
    major_eos = round(max_pe_oi_k - chain[max_pe_oi_k]["PE"].get("ltp", 0), 1) if is_ng else int(round(max_pe_oi_k - chain[max_pe_oi_k]["PE"].get("ltp", 0)))

    tot_ce_oi, tot_pe_oi = sum(ce_ois.values()), sum(pe_ois.values())
    pcr_val = round(tot_pe_oi / tot_ce_oi, 2) if tot_ce_oi > 0 else 1.0

    tech = get_mcx_technicals(smartApi, fut_token)
    vwap_val = (tech["vwap"] if is_ng else int(round(tech["vwap"]))) if tech else fut_ltp
    rsi_val = tech["rsi"] if tech else 50
    st_bullish = tech["st_bullish"] if tech else (fut_ltp >= vwap_val)
    fib_500 = (tech["fib_500"] if is_ng else int(round(tech["fib_500"]))) if tech else fut_ltp
    fib_618 = (tech["fib_618"] if is_ng else int(round(tech["fib_618"]))) if tech else fut_ltp

    # ================= 5-FACTOR SIGNAL SCORING =================
    bull_score, bear_score = 0, 0
    if fut_ltp > vwap_val: bull_score += 1
    elif fut_ltp < vwap_val: bear_score += 1
    if st_bullish: bull_score += 1
    else: bear_score += 1
    if rsi_val > 55: bull_score += 1
    elif rsi_val < 45: bear_score += 1
    if pcr_val >= 1.10: bull_score += 1
    elif pcr_val <= 0.85: bear_score += 1
    if fut_ltp >= fib_500: bull_score += 1
    elif fut_ltp <= fib_618: bear_score += 1

    basis_txt = f"VWAP: {vwap_val} | ST: {'UP' if st_bullish else 'DOWN'} | RSI: {rsi_val} | PCR: {pcr_val} | Fib 0.50: {fib_500}"

    if bull_score >= 4:
        box_class = "bullish"
        dir_title = f"🟢 {commodity} BULLISH — UPAR JAYEGA ({bull_score}/5 Confirmations)"
        rec_ltp = chain[atm_strike]["CE"].get("ltp", 0)
        sl = round(rec_ltp * 0.85, 1) if is_ng else int(round(rec_ltp * 0.85))
        tgt1 = round(rec_ltp * 1.22, 1) if is_ng else int(round(rec_ltp * 1.22))
        tgt2 = round(rec_ltp * 1.40, 1) if is_ng else int(round(rec_ltp * 1.40))
        action_msg = f"ENTRY: BUY {atm_strike} CE @ ₹{rec_ltp}  |  SL: ₹{sl}  |  EXIT 1: ₹{tgt1}  |  EXIT 2: ₹{tgt2}"
    elif bear_score >= 4:
        box_class = "bearish"
        dir_title = f"🔴 {commodity} BEARISH — NEECHE JAYEGA ({bear_score}/5 Confirmations)"
        rec_ltp = chain[atm_strike]["PE"].get("ltp", 0)
        sl = round(rec_ltp * 0.85, 1) if is_ng else int(round(rec_ltp * 0.85))
        tgt1 = round(rec_ltp * 1.22, 1) if is_ng else int(round(rec_ltp * 1.22))
        tgt2 = round(rec_ltp * 1.40, 1) if is_ng else int(round(rec_ltp * 1.40))
        action_msg = f"ENTRY: BUY {atm_strike} PE @ ₹{rec_ltp}  |  SL: ₹{sl}  |  EXIT 1: ₹{tgt1}  |  EXIT 2: ₹{tgt2}"
    else:
        box_class = "neutral"
        dir_title = f"🟡 {commodity} SIDEWAYS / WAIT (Bull: {bull_score}/5 | Bear: {bear_score}/5)"
        action_msg = f"Breakout ka wait karein: Above {major_eor} (CE) ya Below {major_eos} (PE)"

    st.markdown(f"""
    <div class="decision-box {box_class}">
        <div style="font-size: 18px;">{dir_title}</div>
        <div style="font-size: 16px; margin-top: 6px;">{action_msg}</div>
        <div style="font-size: 12px; margin-top: 5px; opacity: 0.9;">{basis_txt}</div>
    </div>
    """, unsafe_allow_html=True)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric(f"{commodity} Future", f"{fut_ltp}")
    m2.metric("VWAP / RSI", f"{vwap_val} / {rsi_val}")
    m3.metric("Resistance (EOR)", f"{max_ce_oi_k} ({major_eor})")
    m4.metric("Support (EOS)", f"{max_pe_oi_k} ({major_eos})")
    m5.metric("PCR (OI)", f"{pcr_val}")

    table_rows = []
    for k in strikes:
        ce, pe = chain[k]["CE"], chain[k]["PE"]
        ce_ltp, ce_oi, ce_vol = ce.get("ltp", 0), ce.get("oi", 0), ce.get("vol", 0)
        pe_ltp, pe_oi, pe_vol = pe.get("ltp", 0), pe.get("oi", 0), pe.get("vol", 0)
        ce_iv, ce_d = calc_iv_and_delta(fut_ltp, k, days_to_exp, ce_ltp, "CE")
        pe_iv, pe_d = calc_iv_and_delta(fut_ltp, k, days_to_exp, pe_ltp, "PE")

        table_rows.append({
            "CE IV|Δ": f"{ce_iv}%|{ce_d}",
            "CE OI": ce_oi,
            "CE Vol": ce_vol,
            "CE SL": round(ce_ltp * 0.85, 1) if is_ng else int(round(ce_ltp * 0.85)),
            "CE Exit1": round(ce_ltp * 1.22, 1) if is_ng else int(round(ce_ltp * 1.22)),
            "CE LTP": ce_ltp,
            "EOR": round(k + ce_ltp, 1) if is_ng else int(round(k + ce_ltp)),
            "STRIKE": f"👉 {k}" if k == atm_strike else f"{k}",
            "EOS": round(k - pe_ltp, 1) if is_ng else int(round(k - pe_ltp)),
            "PE LTP": pe_ltp,
            "PE Exit1": round(pe_ltp * 1.22, 1) if is_ng else int(round(pe_ltp * 1.22)),
            "PE SL": round(pe_ltp * 0.85, 1) if is_ng else int(round(pe_ltp * 0.85)),
            "PE Vol": pe_vol,
            "PE OI": pe_oi,
            "PE IV|Δ": f"{pe_iv}%|{pe_d}"
        })

    df_chain = pd.DataFrame(table_rows)

    def highlight_matrix(row):
        styles = [''] * len(row)
        strike_val = float(str(row["STRIKE"]).replace("👉 ", ""))
        styles[7] = 'background-color: #0f243e; color: white; font-weight: bold;'
        if strike_val == atm_strike:
            styles = ['background-color: #fff200; color: black; font-weight: bold;'] * len(row)
            styles[7] = 'background-color: #000000; color: #fff200; font-weight: bold;'
        else:
            if strike_val < fut_ltp:
                styles[5] = 'background-color: #ffcccc; color: black;'
            else:
                styles[9] = 'background-color: #ccffcc; color: black;'
            if strike_val == max_ce_oi_k:
                styles[1] = 'background-color: #ffe600; color: black; font-weight: bold;'
            if strike_val == max_pe_oi_k:
                styles[13] = 'background-color: #00e673; color: black; font-weight: bold;'
        return styles

    st.dataframe(df_chain.style.apply(highlight_matrix, axis=1), use_container_width=True, height=580, hide_index=True)
    st.caption(f"Future: {fut_symbol} | Option Expiry: {expiry_str} | Updated: {datetime.now().strftime('%H:%M:%S')} IST")

    if auto_refresh:
        time.sleep(5)
        st.rerun()

if __name__ == "__main__":
    main()
