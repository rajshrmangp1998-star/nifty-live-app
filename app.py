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
    page_title="Nifty Live Option Chain Pro",
    page_icon="📈",
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

# Session State for Locked Trade (Taaki Entry Bar-Bar Change Na Ho)
if "locked_nifty_trade" not in st.session_state:
    st.session_state.locked_nifty_trade = None

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
    if market_price <= 0.5:
        return 0, (1.0 if (opt_type == "CE" and S > K) else (-1.0 if (opt_type == "PE" and S < K) else 0.0))
    low, high, iv = 0.01, 3.0, 0.15
    for _ in range(20):
        iv = (low + high) / 2.0
        if bs_price(S, K, T, r, iv, opt_type) > market_price:
            high = iv
        else:
            low = iv
    d1 = (math.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
    delta = norm_cdf(d1) if opt_type == "CE" else (norm_cdf(d1) - 1.0)
    return int(round(iv * 100)), round(delta, 2)

# ================= CACHED SESSION & MASTER DATA =================
@st.cache_resource(ttl=14400)
def get_smartapi_session():
    smartApi = SmartConnect(API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    session = smartApi.generateSession(CLIENT_CODE, PASSWORD, totp)
    return smartApi if session.get("status") else None

@st.cache_data(ttl=86400)
def get_nifty_option_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    df = pd.DataFrame(requests.get(url, timeout=20).json())
    nifty_opts = df[(df['name'] == 'NIFTY') & (df['instrumenttype'] == 'OPTIDX')].copy()
    nifty_opts['expiry_dt'] = pd.to_datetime(nifty_opts['expiry'], format='%d%b%Y')
    today = pd.Timestamp.today().normalize()
    nearest_expiry = nifty_opts[nifty_opts['expiry_dt'] >= today]['expiry_dt'].min()
    current_opts = nifty_opts[nifty_opts['expiry_dt'] == nearest_expiry].copy()
    current_opts['strike_price'] = (current_opts['strike'].astype(float) / 100.0).astype(int)
    days_to_exp = max(1, (nearest_expiry - today).days)
    return current_opts, nearest_expiry.strftime('%d-%b-%Y'), days_to_exp

def get_technical_and_fib(_smartApi):
    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=3)
        params = {
            "exchange": "NSE",
            "symboltoken": "99926000",
            "interval": "FIVE_MINUTE",
            "fromdate": from_date.strftime("%Y-%m-%d 09:15"),
            "todate": to_date.strftime("%Y-%m-%d 15:30")
        }
        res = _smartApi.getCandleData(params)
        if not res.get("status") or not res.get("data"):
            return None

        df = pd.DataFrame(res["data"], columns=["time", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)

        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi_val = int(round((100 - (100 / (1 + rs))).iloc[-1]))

        df['date'] = pd.to_datetime(df['time']).dt.date
        today_df = df[df['date'] == df['date'].iloc[-1]].copy()
        today_df['tp'] = (today_df['high'] + today_df['low'] + today_df['close']) / 3
        vwap_val = int(round(today_df['tp'].expanding().mean().iloc[-1]))

        swing_high = float(today_df['high'].max())
        swing_low = float(today_df['low'].min())
        diff = max(swing_high - swing_low, 20.0)
        fib_382 = int(round(swing_high - (0.382 * diff)))
        fib_500 = int(round(swing_high - (0.500 * diff)))
        fib_618 = int(round(swing_high - (0.618 * diff)))

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

def calculate_max_pain(strikes, ce_oi_map, pe_oi_map):
    min_pain = float("inf")
    max_pain_strike = strikes[len(strikes) // 2]
    for exp_price in strikes:
        pain = sum(((exp_price - k) * ce_oi_map.get(k, 0)) if exp_price > k else ((k - exp_price) * pe_oi_map.get(k, 0)) for k in strikes)
        if pain < min_pain:
            min_pain = pain
            max_pain_strike = exp_price
    return int(max_pain_strike)

# ================= MAIN LIVE APP =================
def main():
    st.title("📈 Nifty 50 Live Option Chain Pro")

    col_t1, col_t2, col_t3 = st.columns([2, 1, 1])
    with col_t1:
        auto_refresh = st.toggle("🔄 Auto-Refresh Live LTP (5 Sec)", value=True)
    with col_t2:
        if st.button("Refresh Now"):
            st.rerun()
    with col_t3:
        if st.button("🔓 Reset Locked Trade"):
            st.session_state.locked_nifty_trade = None
            st.rerun()

    smartApi = get_smartapi_session()
    if not smartApi:
        st.error("Angel One Login Failed! Secrets check karein.")
        return

    opts_df, expiry_str, days_to_exp = get_nifty_option_master()

    spot_res = smartApi.ltpData("NSE", "Nifty 50", "99926000")
    if not spot_res.get("status"):
        time.sleep(2)
        st.rerun()

    spot_ltp = int(round(float(spot_res["data"]["ltp"])))
    atm_strike = int(round(spot_ltp / 50) * 50)

    # Agar koi trade locked hai toh uski strike bhi chain me zaroor rakhein
    locked = st.session_state.locked_nifty_trade
    center_k = locked["strike"] if locked else atm_strike
    strikes = [center_k + (i * 50) for i in range(-8, 9)]

    sub = opts_df[opts_df['strike_price'].isin(strikes)]
    tokens = sub['token'].tolist()
    token_map = dict(zip(sub['token'], zip(sub['strike_price'], sub['symbol'])))

    mkt_res = smartApi.getMarketData("FULL", {"NFO": tokens})
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
        side = "CE" if sym.endswith("CE") else "PE"
        chain[k][side] = {
            "ltp": int(round(float(item.get("ltp", 0)))),
            "oi": int(item.get("opnInterest", 0)),
            "vol": int(item.get("tradeVolume", 0))
        }

    ce_ois = {k: chain[k]["CE"].get("oi", 0) for k in strikes}
    pe_ois = {k: chain[k]["PE"].get("oi", 0) for k in strikes}
    max_ce_oi_k = int(max(ce_ois, key=ce_ois.get))
    max_pe_oi_k = int(max(pe_ois, key=pe_ois.get))

    major_eor = int(round(max_ce_oi_k + chain[max_ce_oi_k]["CE"].get("ltp", 0)))
    major_eos = int(round(max_pe_oi_k - chain[max_pe_oi_k]["PE"].get("ltp", 0)))
    max_pain_k = calculate_max_pain(strikes, ce_ois, pe_ois)

    tot_ce_oi, tot_pe_oi = sum(ce_ois.values()), sum(pe_ois.values())
    pcr_val = round(tot_pe_oi / tot_ce_oi, 2) if tot_ce_oi > 0 else 1.0

    tech = get_technical_and_fib(smartApi)
    vwap_val = int(tech["vwap"]) if tech else spot_ltp
    rsi_val = int(tech["rsi"]) if tech else 50
    st_bullish = tech["st_bullish"] if tech else (spot_ltp >= vwap_val)
    fib_382 = int(tech["fib_382"]) if tech else spot_ltp + 30
    fib_500 = int(tech["fib_500"]) if tech else spot_ltp
    fib_618 = int(tech["fib_618"]) if tech else spot_ltp - 30

    bull_score, bear_score = 0, 0
    if spot_ltp > vwap_val: bull_score += 1
    elif spot_ltp < vwap_val: bear_score += 1
    if st_bullish: bull_score += 1
    else: bear_score += 1
    if rsi_val > 55: bull_score += 1
    elif rsi_val < 45: bear_score += 1
    if pcr_val >= 1.10: bull_score += 1
    elif pcr_val <= 0.85: bear_score += 1
    if spot_ltp >= fib_500: bull_score += 1
    elif spot_ltp <= fib_618: bear_score += 1

    basis_txt = f"VWAP: {vwap_val} | ST: {'UP' if st_bullish else 'DOWN'} | RSI: {rsi_val} | PCR: {pcr_val} | Fib 0.50: {fib_500}"

    # ================= LOCKED ENTRY & SEPARATE LIVE LTP LOGIC =================
    if locked is not None:
        # Pehle se lock kiye hue trade ka sirf LIVE LTP update karein (Entry/SL/Exit fixed rahenge)
        live_opt_ltp = int(chain[locked["strike"]][locked["side"]].get("ltp", locked["entry"]))
        pts_diff = live_opt_ltp - locked["entry"]
        pts_str = f"+{pts_diff}" if pts_diff >= 0 else f"{pts_diff}"

        # Check agar SL ya Final Exit 2 hit ho gaya ho
        if live_opt_ltp <= locked["sl"]:
            box_class = "bearish"
            dir_title = f"❌ STOP LOSS HIT — {locked['strike']} {locked['side']} (Auto-Unlocking)"
            action_msg = f"FIXED ENTRY: ₹{locked['entry']}  |  LIVE LTP: ₹{live_opt_ltp} ({pts_str} pts)  |  SL: ₹{locked['sl']}"
            st.session_state.locked_nifty_trade = None
        elif live_opt_ltp >= locked["exit2"]:
            box_class = "bullish"
            dir_title = f"🎯 FINAL TARGET 2 HIT — {locked['strike']} {locked['side']} (Auto-Unlocking)"
            action_msg = f"FIXED ENTRY: ₹{locked['entry']}  |  LIVE LTP: ₹{live_opt_ltp} ({pts_str} pts)  |  EXIT 2: ₹{locked['exit2']}"
            st.session_state.locked_nifty_trade = None
        else:
            box_class = "bullish" if locked["side"] == "CE" else "bearish"
            status_tag = "EXIT 1 CROSSED (Trail SL to Entry!)" if live_opt_ltp >= locked["exit1"] else "TRADE ACTIVE"
            dir_title = f"🔒 {status_tag} — BUY {locked['strike']} {locked['side']} (Locked @ {locked['time']})"
            action_msg = (
                f"FIXED ENTRY: ₹{locked['entry']}  |  LIVE LTP: ₹{live_opt_ltp} ({pts_str} pts)  ||  "
                f"SL: ₹{locked['sl']}  |  EXIT 1: ₹{locked['exit1']}  |  EXIT 2: ₹{locked['exit2']}"
            )
    else:
        # Naya Signal Check karein aur milte hi LOCK kar dein
        if bull_score >= 4:
            rec_ltp = int(chain[atm_strike]["CE"].get("ltp", 0))
            _, rec_delta = calc_iv_and_delta(spot_ltp, atm_strike, days_to_exp, rec_ltp, "CE")
            d = max(abs(rec_delta), 0.45)
            risk_pts = max(int(round(rec_ltp * 0.15)), int(round(20 * d)))
            sl = max(rec_ltp - risk_pts, 5)
            tgt1 = int(round(rec_ltp + (risk_pts * 1.5)))
            tgt2 = int(round(rec_ltp + max((major_eor - spot_ltp) * d, risk_pts * 2.5)))

            st.session_state.locked_nifty_trade = {
                "side": "CE", "strike": atm_strike, "entry": rec_ltp,
                "sl": sl, "exit1": tgt1, "exit2": tgt2,
                "time": datetime.now().strftime("%H:%M")
            }
            st.rerun()

        elif bear_score >= 4:
            rec_ltp = int(chain[atm_strike]["PE"].get("ltp", 0))
            _, rec_delta = calc_iv_and_delta(spot_ltp, atm_strike, days_to_exp, rec_ltp, "PE")
            d = max(abs(rec_delta), 0.45)
            risk_pts = max(int(round(rec_ltp * 0.15)), int(round(20 * d)))
            sl = max(rec_ltp - risk_pts, 5)
            tgt1 = int(round(rec_ltp + (risk_pts * 1.5)))
            tgt2 = int(round(rec_ltp + max((spot_ltp - major_eos) * d, risk_pts * 2.5)))

            st.session_state.locked_nifty_trade = {
                "side": "PE", "strike": atm_strike, "entry": rec_ltp,
                "sl": sl, "exit1": tgt1, "exit2": tgt2,
                "time": datetime.now().strftime("%H:%M")
            }
            st.rerun()

        else:
            box_class = "neutral"
            dir_title = f"🟡 NO TRADE / WAIT (Bull: {bull_score}/5 | Bear: {bear_score}/5)"
            action_msg = f"Clear Entry ke liye Fib 0.382 ({fib_382}) ke upar CE ya Fib 0.618 ({fib_618}) ke neeche PE ka wait karein"

    st.markdown(f"""
    <div class="decision-box {box_class}">
        <div style="font-size: 18px;">{dir_title}</div>
        <div style="font-size: 16px; margin-top: 6px;">{action_msg}</div>
        <div style="font-size: 12px; margin-top: 5px; opacity: 0.9;">{basis_txt}</div>
    </div>
    """, unsafe_allow_html=True)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Nifty Spot", f"{spot_ltp}")
    m2.metric("VWAP / RSI", f"{vwap_val} / {rsi_val}")
    m3.metric("Fib 0.38 / 0.62", f"{fib_382} / {fib_618}")
    m4.metric("Resistance (EOR)", f"{max_ce_oi_k} ({major_eor})")
    m5.metric("Support (EOS)", f"{max_pe_oi_k} ({major_eos})")
    m6.metric("PCR / Max Pain", f"{pcr_val} / {max_pain_k}")

    # ================= OPTION CHAIN MATRIX TABLE (LIVE LTP ALAG DIKHEGA) =================
    table_rows = []
    for k in strikes:
        ce, pe = chain[k]["CE"], chain[k]["PE"]
        ce_ltp, ce_oi, ce_vol = int(ce.get("ltp", 0)), int(ce.get("oi", 0)), int(ce.get("vol", 0))
        pe_ltp, pe_oi, pe_vol = int(pe.get("ltp", 0)), int(pe.get("oi", 0)), int(pe.get("vol", 0))

        ce_iv, ce_d = calc_iv_and_delta(spot_ltp, k, days_to_exp, ce_ltp, "CE")
        pe_iv, pe_d = calc_iv_and_delta(spot_ltp, k, days_to_exp, pe_ltp, "PE")
        d_ce, d_pe = max(abs(ce_d), 0.20), max(abs(pe_d), 0.20)

        ce_risk = max(int(round(ce_ltp * 0.15)), int(round(20 * d_ce)))
        pe_risk = max(int(round(pe_ltp * 0.15)), int(round(20 * d_pe)))

        table_rows.append({
            "CE IV|Δ": f"{ce_iv}%|{ce_d}",
            "CE OI": ce_oi,
            "CE Vol": ce_vol,
            "CE SL": max(ce_ltp - ce_risk, 5),
            "CE Exit1": int(round(ce_ltp + ce_risk * 1.5)),
            "CE Live LTP": ce_ltp,
            "EOR": int(round(k + ce_ltp)),
            "STRIKE": f"👉 {k}" if k == atm_strike else f"{k}",
            "EOS": int(round(k - pe_ltp)),
            "PE Live LTP": pe_ltp,
            "PE Exit1": int(round(pe_ltp + pe_risk * 1.5)),
            "PE SL": max(pe_ltp - pe_risk, 5),
            "PE Vol": pe_vol,
            "PE OI": pe_oi,
            "PE IV|Δ": f"{pe_iv}%|{pe_d}"
        })

    df_chain = pd.DataFrame(table_rows)

    def highlight_matrix(row):
        styles = [''] * len(row)
        strike_num = int(str(row["STRIKE"]).replace("👉 ", ""))
        styles[7] = 'background-color: #0f243e; color: white; font-weight: bold;'
        if strike_num == atm_strike:
            styles = ['background-color: #fff200; color: black; font-weight: bold;'] * len(row)
            styles[7] = 'background-color: #000000; color: #fff200; font-weight: bold;'
        else:
            if strike_num < spot_ltp:
                styles[5] = 'background-color: #ffcccc; color: black;'
            else:
                styles[9] = 'background-color: #ccffcc; color: black;'
            if strike_num == max_ce_oi_k:
                styles[1] = 'background-color: #ffe600; color: black; font-weight: bold;'
            if strike_num == max_pe_oi_k:
                styles[13] = 'background-color: #00e673; color: black; font-weight: bold;'
        return styles

    styled_df = df_chain.style.apply(highlight_matrix, axis=1).format({
        "CE OI": "{:,d}", "CE Vol": "{:,d}", "CE SL": "{:d}", "CE Exit1": "{:d}",
        "CE Live LTP": "{:d}", "EOR": "{:d}", "EOS": "{:d}", "PE Live LTP": "{:d}",
        "PE Exit1": "{:d}", "PE SL": "{:d}", "PE Vol": "{:,d}", "PE OI": "{:,d}"
    })

    st.dataframe(styled_df, use_container_width=True, height=620, hide_index=True)
    st.caption(f"Expiry: {expiry_str} | Updated: {datetime.now().strftime('%H:%M:%S')} IST")

    if auto_refresh:
        time.sleep(5)
        st.rerun()

if __name__ == "__main__":
    main()
