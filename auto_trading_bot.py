import json
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd
import requests
import ta
from flask import Flask
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ============================================================
# ULTRA A+ BTCUSDT FUTURES v5 MICRO-50
# ------------------------------------------------------------
# Purpose:
# - Built from the supplied v4 engine.
# - Tuned from the supplied 5-year BTCUSDT 15m/1h/4h data.
# - Designed for a small TESTNET balance (~$50).
#
# IMPORTANT:
# - TESTNET is the default.
# - ONE-WAY position mode is required.
# - Default risk is 1.0% of available USDT per trade.
# - A $30/month profit is a GOAL/ALERT only, not a forced-trading
#   condition. The backtest does NOT support guaranteeing $30/month
#   from a $50 account without taking extreme risk.
#
# 5-year tuning used for this v5:
# - MIN_SCORE 55
# - 1H trend remains a HARD gate
# - 15m trend remains a HARD gate
# - 4H trend becomes a SCORE factor, not a hard gate
# - 1H/4H ADX >= 18
# - 15m reversal + no-impulse + VWAP + directional volume stay HARD
# - Momentum contributes to score but is NOT a hard gate
# - Dynamic structural RR kept between 2.0R and 3.0R
#
# This produced the best balance I found among the tested variants:
# about 66 trades / 5 years (~13/year), PF ~1.35, with conservative
# 1% risk. Out-of-sample (2025-01 onward) was positive in the supplied
# 5-year set, but the sample is still small. Forward test first.
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Ultra A+ v5 Micro-50 is running"

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ------------------------- SETTINGS ---------------------------
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
TIMEFRAME = "15m"
TREND_1H = "1h"
TREND_4H = "4h"

TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
API_KEY = os.getenv("BINANCE_TESTNET_KEY") if TESTNET else os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_TESTNET_SECRET") if TESTNET else os.getenv("BINANCE_SECRET_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Strategy tuning
MIN_SCORE = int(os.getenv("MIN_SCORE", "55"))
MIN_RR = float(os.getenv("MIN_RR", "2.0"))
MAX_RR = float(os.getenv("MAX_RR", "3.0"))

# Micro-account risk
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))  # 1% default
MAX_ACCEPTED_ACTUAL_RISK = float(os.getenv("MAX_ACCEPTED_ACTUAL_RISK", "0.05"))  # 5% hard ceiling

MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "3"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "90"))

# Safety overlays. These STOP NEW ENTRIES; they do not force exits.
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.03"))      # 3%
MAX_MONTHLY_LOSS_PCT = float(os.getenv("MAX_MONTHLY_LOSS_PCT", "0.10"))  # 10%
MONTHLY_PROFIT_TARGET_USDT = float(os.getenv("MONTHLY_PROFIT_TARGET_USDT", "30"))

LEVERAGE = int(os.getenv("LEVERAGE", "3"))
USE_ISOLATED = os.getenv("USE_ISOLATED", "true").lower() == "true"
REQUIRE_ONE_WAY = os.getenv("REQUIRE_ONE_WAY", "true").lower() == "true"

# Break-even remains OFF by default, preserving the v4 behavior.
ENABLE_BREAKEVEN = os.getenv("ENABLE_BREAKEVEN", "false").lower() == "true"
BREAKEVEN_TRIGGER_R = float(os.getenv("BREAKEVEN_TRIGGER_R", "1.25"))
BREAKEVEN_BUFFER_ATR = float(os.getenv("BREAKEVEN_BUFFER_ATR", "0.05"))
BREAKEVEN_CHECK_SECONDS = int(os.getenv("BREAKEVEN_CHECK_SECONDS", "300"))

LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "60"))

CANDLES_15M = int(os.getenv("CANDLES_15M", "240"))
CANDLES_1H = int(os.getenv("CANDLES_1H", "240"))
CANDLES_4H = int(os.getenv("CANDLES_4H", "240"))

STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/ultra_a_plus_v5_micro50_state.json"))

if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing Binance API credentials in environment variables")

client = Client(
    API_KEY,
    API_SECRET,
    testnet=TESTNET,
    requests_params={"timeout": 12},
)

SYMBOL_FILTERS = {
    "step_size": 0.001,
    "min_qty": 0.001,
    "max_qty": 1_000_000.0,
    "tick_size": 0.10,
    "min_notional": 0.0,
}

STATE = {
    "last_signal_candle": None,
    "last_trade_time": 0.0,
    "daily_trade_count": 0,
    "daily_date": None,
    "consecutive_losses": 0,

    "daily_start_balance": 0.0,
    "monthly_start_balance": 0.0,
    "monthly_key": None,

    "position": None,
    "entry_price": 0.0,
    "stop_price": 0.0,
    "target_price": 0.0,
    "atr_at_entry": 0.0,
    "qty": 0.0,
    "entry_time": 0,
    "entry_order_id": None,
    "stop_order_id": None,
    "tp_order_id": None,

    "breakeven_done": False,
    "last_be_check": 0.0,
}

# ------------------------- UTILITIES --------------------------
def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
            },
            timeout=8,
        )
    except Exception as exc:
        print(f"Telegram error: {exc}")

def save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"State save warning: {exc}")

def load_state() -> None:
    try:
        if STATE_FILE.exists():
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for key in STATE:
                if key in raw:
                    STATE[key] = raw[key]
    except Exception as exc:
        print(f"State load warning: {exc}")

def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    value_d = Decimal(str(value))
    step_d = Decimal(str(step))
    return float(
        (value_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
    )

def round_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    value_d = Decimal(str(value))
    tick_d = Decimal(str(tick))
    return float(
        (value_d / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
    )

def utc_day_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())

def utc_month_key() -> str:
    return time.strftime("%Y-%m", time.gmtime())

def cooldown_ok() -> bool:
    return (time.time() - float(STATE["last_trade_time"])) >= COOLDOWN_MINUTES * 60

# ---------------------- EXCHANGE SETUP ------------------------
def load_exchange_filters() -> None:
    info = client.futures_exchange_info()
    symbol_info = next((s for s in info["symbols"] if s["symbol"] == SYMBOL), None)
    if not symbol_info:
        raise RuntimeError(f"Symbol not found: {SYMBOL}")

    # Reset defaults each run.
    SYMBOL_FILTERS.update({
        "step_size": 0.001,
        "min_qty": 0.001,
        "max_qty": 1_000_000.0,
        "tick_size": 0.10,
        "min_notional": 0.0,
    })

    for f in symbol_info.get("filters", []):
        ftype = f.get("filterType")
        if ftype == "MARKET_LOT_SIZE":
            SYMBOL_FILTERS["step_size"] = float(f["stepSize"])
            SYMBOL_FILTERS["min_qty"] = float(f["minQty"])
            SYMBOL_FILTERS["max_qty"] = float(f.get("maxQty", SYMBOL_FILTERS["max_qty"]))
        elif ftype == "LOT_SIZE" and SYMBOL_FILTERS["step_size"] == 0.001:
            SYMBOL_FILTERS["step_size"] = float(f["stepSize"])
            SYMBOL_FILTERS["min_qty"] = float(f["minQty"])
            SYMBOL_FILTERS["max_qty"] = float(f.get("maxQty", SYMBOL_FILTERS["max_qty"]))
        elif ftype == "PRICE_FILTER":
            SYMBOL_FILTERS["tick_size"] = float(f["tickSize"])
        elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
            SYMBOL_FILTERS["min_notional"] = float(
                f.get("notional", f.get("minNotional", 0.0))
            )

    print(f"Exchange filters loaded: {SYMBOL_FILTERS}")

def configure_account() -> None:
    try:
        if REQUIRE_ONE_WAY:
            mode = client.futures_get_position_mode()
            if bool(mode.get("dualSidePosition")):
                raise RuntimeError(
                    "HEDGE MODE detected. v5 requires Binance Futures ONE-WAY position mode."
                )

        client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

        if USE_ISOLATED:
            try:
                client.futures_change_margin_type(
                    symbol=SYMBOL,
                    marginType="ISOLATED",
                )
            except BinanceAPIException as exc:
                print(f"Margin type note: {exc}")

    except Exception as exc:
        raise RuntimeError(f"Account configuration failed: {exc}") from exc

# ---------------------- MARKET DATA ---------------------------
KLINE_COLUMNS = [
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "qav",
    "num_trades",
    "taker_base_vol",
    "taker_quote_vol",
    "ignore",
]

def get_klines(interval: str, limit: int) -> pd.DataFrame:
    klines = client.futures_klines(
        symbol=SYMBOL,
        interval=interval,
        limit=limit,
    )
    df = pd.DataFrame(klines, columns=KLINE_COLUMNS)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_numeric(df["time"], errors="coerce").astype("int64")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema20"] = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()

    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()

    macd = ta.trend.MACD(
        df["close"],
        window_fast=12,
        window_slow=26,
        window_sign=9,
    )
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["adx"] = ta.trend.ADXIndicator(
        df["high"],
        df["low"],
        df["close"],
        window=14,
    ).adx()

    atr = ta.volatility.AverageTrueRange(
        df["high"],
        df["low"],
        df["close"],
        window=14,
    )
    df["atr"] = atr.average_true_range()

    df["vol_sma20"] = df["volume"].rolling(20).mean()

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vwap48"] = (
        (typical * df["volume"]).rolling(48).sum()
        / df["volume"].rolling(48).sum()
    )

    df["support48"] = df["low"].rolling(48).min()
    df["resistance48"] = df["high"].rolling(48).max()
    df["swing_low12"] = df["low"].rolling(12).min()
    df["swing_high12"] = df["high"].rolling(12).max()

    return df

def closed(df: pd.DataFrame) -> pd.Series:
    return df.iloc[-2]

# ---------------------- SIGNAL ENGINE -------------------------
def candle_features(row: pd.Series, prev: pd.Series) -> dict:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])

    po = float(prev["open"])
    pc = float(prev["close"])

    body = abs(c - o)
    lower = min(o, c) - l
    upper = h - max(o, c)
    rng = max(h - l, 1e-12)

    bullish_engulf = (
        pc < po and c > o and c >= po and o <= pc
    )
    bearish_engulf = (
        pc > po and c < o and c <= po and o >= pc
    )

    bullish_rejection = (
        body > 0 and lower >= 1.8 * body and c > o
    )
    bearish_rejection = (
        body > 0 and upper >= 1.8 * body and c < o
    )

    return {
        "bullish": bullish_engulf or bullish_rejection,
        "bearish": bearish_engulf or bearish_rejection,
        "bullish_engulf": bullish_engulf,
        "bearish_engulf": bearish_engulf,
        "close_near_high": (h - c) <= 0.25 * rng,
        "close_near_low": (c - l) <= 0.25 * rng,
    }

def liquidity_sweep(row: pd.Series, prev: pd.Series, side: str) -> bool:
    if side == "BUY":
        return (
            float(row["low"]) < float(prev["low"])
            and float(row["close"]) > float(prev["low"])
        )
    return (
        float(row["high"]) > float(prev["high"])
        and float(row["close"]) < float(prev["high"])
    )

def impulse_risk(row: pd.Series, atr: float, side: str) -> bool:
    if atr <= 0:
        return True

    body = abs(float(row["close"]) - float(row["open"]))
    rng = max(float(row["high"]) - float(row["low"]), 1e-12)
    body_atr = body / atr
    body_fraction = body / rng

    if side == "BUY":
        bearish_body = float(row["close"]) < float(row["open"])
        close_near_low = (
            float(row["close"]) - float(row["low"])
        ) <= 0.25 * rng

        return (
            bearish_body
            and body_atr >= 1.15
            and body_fraction >= 0.60
            and close_near_low
        )

    bullish_body = float(row["close"]) > float(row["open"])
    close_near_high = (
        float(row["high"]) - float(row["close"])
    ) <= 0.25 * rng

    return (
        bullish_body
        and body_atr >= 1.15
        and body_fraction >= 0.60
        and close_near_high
    )

def trend_slope_ok(df: pd.DataFrame, idx: int, fast_col: str) -> bool:
    if idx < 2:
        return False

    a = float(df.iloc[idx][fast_col])
    b = float(df.iloc[idx - 2][fast_col])
    return a > b

def build_signal(
    df15: pd.DataFrame,
    df1h: pd.DataFrame,
    df4h: pd.DataFrame,
) -> Tuple[Optional[str], Optional[dict]]:

    row = closed(df15)
    row_idx = len(df15) - 2

    prev = df15.iloc[-3]
    prev2 = df15.iloc[-4]

    h1 = closed(df1h)
    h1_prev = df1h.iloc[-3]

    h4 = closed(df4h)
    h4_prev = df4h.iloc[-3]

    needed = [
        row["atr"],
        row["adx"],
        row["rsi"],
        row["macd_hist"],
        row["vwap48"],
        row["vol_sma20"],
        h1["adx"],
        h4["adx"],
        h1["ema200"],
        h4["ema200"],
    ]

    if any(pd.isna(v) for v in needed):
        return None, None

    price = float(row["close"])
    atr = float(row["atr"])

    if price <= 0 or atr <= 0:
        return None, None

    atr_pct = atr / price
    vol_ratio = (
        float(row["volume"] / row["vol_sma20"])
        if float(row["vol_sma20"]) > 0
        else 0.0
    )

    candle = candle_features(row, prev)

    # ---------------- Regime ----------------
    h4_bull = (
        float(h4["close"]) > float(h4["ema50"]) > float(h4["ema200"])
        and float(h4["ema50"]) > float(h4_prev["ema50"])
    )
    h4_bear = (
        float(h4["close"]) < float(h4["ema50"]) < float(h4["ema200"])
        and float(h4["ema50"]) < float(h4_prev["ema50"])
    )

    h4_strong = float(h4["adx"]) >= 18

    h1_bull = (
        float(h1["close"])
        > float(h1["ema20"])
        > float(h1["ema50"])
        > float(h1["ema200"])
        and float(h1["ema20"]) > float(h1_prev["ema20"])
    )
    h1_bear = (
        float(h1["close"])
        < float(h1["ema20"])
        < float(h1["ema50"])
        < float(h1["ema200"])
        and float(h1["ema20"]) < float(h1_prev["ema20"])
    )

    h1_strong = float(h1["adx"]) >= 18

    # v5: 15m trend remains a hard gate.
    m15_bull = (
        float(row["close"]) > float(row["ema20"]) > float(row["ema50"])
        and trend_slope_ok(df15, row_idx, "ema20")
    )

    m15_bear = (
        float(row["close"]) < float(row["ema20"]) < float(row["ema50"])
        and not trend_slope_ok(df15, row_idx, "ema20")
    )

    if atr_pct < 0.0007 or atr_pct > 0.035:
        return None, None

    rsi = float(row["rsi"])
    rsi_prev = float(prev["rsi"])

    macd_hist = float(row["macd_hist"])
    macd_prev = float(prev["macd_hist"])

    vwap = float(row["vwap48"])

    # ---------------- Hard BUY ----------------
    bullish_structure = (
        float(row["close"]) > float(prev["high"])
        or candle["bullish_engulf"]
        or (
            liquidity_sweep(row, prev, "BUY")
            and candle["close_near_high"]
        )
    )

    buy_reversal = (
        bullish_structure
        and float(row["close"]) > float(row["open"])
    )

    buy_not_overextended = (
        price - float(row["ema20"])
    ) <= 1.35 * atr

    buy_no_impulse = (
        not impulse_risk(row, atr, "BUY")
        and not impulse_risk(prev, atr, "BUY")
    )

    buy_vwap_ok = price >= vwap

    buy_volume_ok = (
        vol_ratio >= 1.15
        and float(row["close"]) > float(row["open"])
    )

    buy_hard_ok = all([
        h1_bull,
        h1_strong,
        m15_bull,
        buy_reversal,
        buy_not_overextended,
        buy_no_impulse,
        buy_vwap_ok,
        buy_volume_ok,
    ])

    # ---------------- Hard SELL ----------------
    bearish_structure = (
        float(row["close"]) < float(prev["low"])
        or candle["bearish_engulf"]
        or (
            liquidity_sweep(row, prev, "SELL")
            and candle["close_near_low"]
        )
    )

    sell_reversal = (
        bearish_structure
        and float(row["close"]) < float(row["open"])
    )

    sell_not_overextended = (
        float(row["ema20"]) - price
    ) <= 1.35 * atr

    sell_no_impulse = (
        not impulse_risk(row, atr, "SELL")
        and not impulse_risk(prev, atr, "SELL")
    )

    sell_vwap_ok = price <= vwap

    sell_volume_ok = (
        vol_ratio >= 1.15
        and float(row["close"]) < float(row["open"])
    )

    sell_hard_ok = all([
        h1_bear,
        h1_strong,
        m15_bear,
        sell_reversal,
        sell_not_overextended,
        sell_no_impulse,
        sell_vwap_ok,
        sell_volume_ok,
    ])

    # ---------------- Score ----------------
    buy = 0
    buy_reasons = []

    if h4_bull:
        buy += 20
        buy_reasons.append("4H trend bullish")

    if h4_strong:
        buy += 5
        buy_reasons.append("4H ADX strong")

    if h1_bull:
        buy += 15
        buy_reasons.append("1H trend aligned")

    if h1_strong:
        buy += 5
        buy_reasons.append("1H ADX strong")

    if m15_bull:
        buy += 8
        buy_reasons.append("15m trend aligned")

    if price > vwap:
        buy += 5
        buy_reasons.append("above VWAP")

    # Momentum contributes to score, NOT a hard gate in v5.
    if 38 <= rsi <= 55 and rsi > rsi_prev:
        buy += 5
        buy_reasons.append("RSI recovering")

    if macd_hist > 0 and macd_hist >= macd_prev:
        buy += 5
        buy_reasons.append("MACD improving")

    if buy_volume_ok:
        buy += 5
        buy_reasons.append("bullish volume")

    if liquidity_sweep(row, prev, "BUY"):
        buy += 5
        buy_reasons.append("bullish liquidity sweep")

    if candle["bullish"]:
        buy += 7
        buy_reasons.append("bullish candle confirmation")

    if float(row["close"]) > float(prev["high"]):
        buy += 10
        buy_reasons.append("previous high reclaimed")

    sell = 0
    sell_reasons = []

    if h4_bear:
        sell += 20
        sell_reasons.append("4H trend bearish")

    if h4_strong:
        sell += 5
        sell_reasons.append("4H ADX strong")

    if h1_bear:
        sell += 15
        sell_reasons.append("1H trend aligned")

    if h1_strong:
        sell += 5
        sell_reasons.append("1H ADX strong")

    if m15_bear:
        sell += 8
        sell_reasons.append("15m trend aligned")

    if price < vwap:
        sell += 5
        sell_reasons.append("below VWAP")

    if 45 <= rsi <= 62 and rsi < rsi_prev:
        sell += 5
        sell_reasons.append("RSI rolling over")

    if macd_hist < 0 and macd_hist <= macd_prev:
        sell += 5
        sell_reasons.append("MACD weakening")

    if sell_volume_ok:
        sell += 5
        sell_reasons.append("bearish volume")

    if liquidity_sweep(row, prev, "SELL"):
        sell += 5
        sell_reasons.append("bearish liquidity sweep")

    if candle["bearish"]:
        sell += 7
        sell_reasons.append("bearish candle confirmation")

    if float(row["close"]) < float(prev["low"]):
        sell += 10
        sell_reasons.append("previous low broken")

    # ---------------- Side selection ----------------
    if buy_hard_ok and buy >= MIN_SCORE and buy >= sell + 10:
        side = "BUY"
        score = buy
        reasons = buy_reasons
    elif sell_hard_ok and sell >= MIN_SCORE and sell >= buy + 10:
        side = "SELL"
        score = sell
        reasons = sell_reasons
    else:
        return None, None

    # ---------------- SL / TP ----------------
    support = float(row["support48"])
    resistance = float(row["resistance48"])
    swing_low = float(row["swing_low12"])
    swing_high = float(row["swing_high12"])

    if side == "BUY":
        structural_sl = swing_low - 0.20 * atr
        atr_sl = price - 1.45 * atr
        stop = min(structural_sl, atr_sl)
        risk_distance = price - stop
        room = resistance - price

    else:
        structural_sl = swing_high + 0.20 * atr
        atr_sl = price + 1.45 * atr
        stop = max(structural_sl, atr_sl)
        risk_distance = stop - price
        room = price - support

    if risk_distance <= 0 or room <= 0:
        return None, None

    if risk_distance > 2.60 * atr:
        return None, None

    structural_rr = room / risk_distance

    if structural_rr < MIN_RR:
        return None, None

    rr = min(
        MAX_RR,
        max(MIN_RR, structural_rr * 0.90),
    )

    target = (
        price + rr * risk_distance
        if side == "BUY"
        else price - rr * risk_distance
    )

    if side == "BUY" and not (stop < price < target):
        return None, None

    if side == "SELL" and not (stop > price > target):
        return None, None

    setup = {
        "side": side,
        "score": int(score),
        "price": price,
        "stop": stop,
        "target": target,
        "risk_distance": risk_distance,
        "rr": rr,
        "atr": atr,
        "vol_ratio": vol_ratio,
        "rsi": rsi,
        "rsi_prev": rsi_prev,
        "candle_time": int(row["time"]),
        "reasons": reasons,
    }

    return side, setup

# --------------------- ACCOUNT / POSITION ---------------------
def get_available_usdt() -> float:
    balances = client.futures_account_balance()

    for item in balances:
        if item.get("asset") == "USDT":
            return max(0.0, float(item.get("availableBalance", 0.0)))

    return 0.0

def get_position() -> Tuple[Optional[str], float, float]:
    positions = client.futures_position_information(symbol=SYMBOL)

    for p in positions:
        if p.get("symbol") != SYMBOL:
            continue

        amt = float(p.get("positionAmt", 0.0))

        if abs(amt) > 0:
            side = "BUY" if amt > 0 else "SELL"
            return side, abs(amt), float(p.get("entryPrice", 0.0))

    return None, 0.0, 0.0

def calculate_quantity(entry: float, stop: float) -> float:
    balance = get_available_usdt()

    if balance <= 0:
        return 0.0

    risk_usdt = balance * RISK_PER_TRADE
    stop_distance = abs(entry - stop)

    if stop_distance <= 0:
        return 0.0

    raw_qty = risk_usdt / stop_distance
    qty = round_step(raw_qty, SYMBOL_FILTERS["step_size"])

    # Small-account safety:
    # do NOT force minQty if it would exceed our hard actual-risk ceiling.
    if qty < SYMBOL_FILTERS["min_qty"]:
        min_qty = SYMBOL_FILTERS["min_qty"]
        actual_risk = min_qty * stop_distance / balance

        if actual_risk > MAX_ACCEPTED_ACTUAL_RISK:
            return 0.0

        qty = min_qty

    qty = min(qty, SYMBOL_FILTERS["max_qty"])

    notional = qty * entry
    min_notional = SYMBOL_FILTERS.get("min_notional", 0.0)

    if min_notional and notional < min_notional:
        return 0.0

    actual_risk_pct = (qty * stop_distance) / balance

    if actual_risk_pct > MAX_ACCEPTED_ACTUAL_RISK:
        return 0.0

    return qty

# -------------------- PROTECTIVE ORDERS -----------------------
def cancel_protection_orders() -> None:
    try:
        orders = client.futures_get_open_orders(symbol=SYMBOL)
    except Exception as exc:
        print(f"Open-orders lookup warning: {exc}")
        return

    for order in orders:
        cid = str(order.get("clientOrderId", ""))

        if cid.startswith("A5_"):
            try:
                client.futures_cancel_order(
                    symbol=SYMBOL,
                    orderId=order["orderId"],
                )
            except Exception as exc:
                print(
                    f"Cancel order warning {order.get('orderId')}: {exc}"
                )

def place_protection(
    side: str,
    stop: float,
    target: float,
) -> Tuple[dict, dict]:

    close_side = "SELL" if side == "BUY" else "BUY"

    stop = round_tick(
        stop,
        SYMBOL_FILTERS["tick_size"],
    )
    target = round_tick(
        target,
        SYMBOL_FILTERS["tick_size"],
    )

    stamp = int(time.time() * 1000) % 1_000_000_000

    stop_order = client.futures_create_order(
        symbol=SYMBOL,
        side=close_side,
        type="STOP_MARKET",
        stopPrice=stop,
        closePosition=True,
        workingType="MARK_PRICE",
        newClientOrderId=f"A5_SL_{stamp}",
    )

    try:
        tp_order = client.futures_create_order(
            symbol=SYMBOL,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=target,
            closePosition=True,
            workingType="MARK_PRICE",
            newClientOrderId=f"A5_TP_{stamp}",
        )

    except Exception as exc:
        print(f"TP placement failed; SL kept active: {exc}")
        raise

    return stop_order, tp_order

def emergency_close(side: str, qty: float) -> None:
    close_side = "SELL" if side == "BUY" else "BUY"

    try:
        client.futures_create_order(
            symbol=SYMBOL,
            side=close_side,
            type="MARKET",
            quantity=qty,
            reduceOnly=True,
            newOrderRespType="RESULT",
        )
    except Exception as exc:
        print(f"EMERGENCY CLOSE FAILED: {exc}")
        send_telegram(
            f"🚨 *EMERGENCY CLOSE FAILED*\n`{exc}`"
        )

# ----------------------- TRADE HISTORY ------------------------
def realized_pnl_since(start_ms: int) -> float:
    if start_ms <= 0:
        return 0.0

    try:
        trades = client.futures_account_trades(
            symbol=SYMBOL,
            startTime=max(0, start_ms - 2_000),
            limit=100,
        )

        realized = 0.0

        for trade in trades:
            realized += float(trade.get("realizedPnl", 0.0))
            realized -= float(trade.get("commission", 0.0))

        return realized

    except Exception as exc:
        print(f"PnL lookup warning: {exc}")
        return 0.0

def reset_position_state() -> None:
    for key, value in {
        "position": None,
        "entry_price": 0.0,
        "stop_price": 0.0,
        "target_price": 0.0,
        "atr_at_entry": 0.0,
        "qty": 0.0,
        "entry_time": 0,
        "entry_order_id": None,
        "stop_order_id": None,
        "tp_order_id": None,
        "breakeven_done": False,
        "last_be_check": 0.0,
    }.items():
        STATE[key] = value

def recover_position_once() -> None:
    side, qty, entry = get_position()

    if side:
        STATE["position"] = side
        STATE["entry_price"] = entry
        STATE["qty"] = qty

        orders = client.futures_get_open_orders(symbol=SYMBOL)

        stops = [
            o for o in orders
            if o.get("type") == "STOP_MARKET"
            and str(o.get("clientOrderId", "")).startswith("A5_")
        ]

        tps = [
            o for o in orders
            if o.get("type") == "TAKE_PROFIT_MARKET"
            and str(o.get("clientOrderId", "")).startswith("A5_")
        ]

        if stops:
            STATE["stop_order_id"] = stops[0].get("orderId")
            STATE["stop_price"] = float(
                stops[0].get("stopPrice", 0.0)
            )

        if tps:
            STATE["tp_order_id"] = tps[0].get("orderId")
            STATE["target_price"] = float(
                tps[0].get("stopPrice", 0.0)
            )

        if not stops or not tps:
            send_telegram(
                "⚠️ *Recovered position is missing protection.*"
            )

    else:
        reset_position_state()

    save_state()

# ------------------- ACCOUNT SAFETY WINDOWS ------------------
def initialize_risk_windows() -> None:
    balance = get_available_usdt()
    today = utc_day_key()
    month = utc_month_key()

    changed = False

    if STATE["daily_date"] != today:
        STATE["daily_date"] = today
        STATE["daily_trade_count"] = 0
        STATE["consecutive_losses"] = 0
        STATE["daily_start_balance"] = balance
        changed = True

    if STATE["monthly_key"] != month:
        STATE["monthly_key"] = month
        STATE["monthly_start_balance"] = balance
        changed = True

    if changed:
        save_state()

def safety_window_ok() -> bool:
    balance = get_available_usdt()

    daily_start = float(STATE.get("daily_start_balance", 0.0))
    monthly_start = float(STATE.get("monthly_start_balance", 0.0))

    if daily_start > 0:
        daily_change = (balance - daily_start) / daily_start
        if daily_change <= -MAX_DAILY_LOSS_PCT:
            return False

    if monthly_start > 0:
        monthly_change = (balance - monthly_start) / monthly_start
        if monthly_change <= -MAX_MONTHLY_LOSS_PCT:
            return False

    return True

def monthly_target_reached() -> bool:
    balance = get_available_usdt()
    start = float(STATE.get("monthly_start_balance", 0.0))

    if start <= 0:
        return False

    return (balance - start) >= MONTHLY_PROFIT_TARGET_USDT

def can_trade() -> bool:
    initialize_risk_windows()

    if STATE["position"] is not None:
        return False

    if int(STATE["daily_trade_count"]) >= MAX_DAILY_TRADES:
        return False

    if int(STATE["consecutive_losses"]) >= MAX_CONSECUTIVE_LOSSES:
        return False

    if not cooldown_ok():
        return False

    if not safety_window_ok():
        return False

    # Profit target is advisory/optional. We do NOT force-stop the
    # bot by default because this is a research/testnet target.
    return True

# ---------------------- BREAK-EVEN ----------------------------
def maybe_move_to_breakeven(
    current_price: float,
    current_atr: float,
) -> None:

    if not ENABLE_BREAKEVEN:
        return

    if not STATE["position"] or STATE["breakeven_done"]:
        return

    if (
        time.time() - float(STATE["last_be_check"])
    ) < BREAKEVEN_CHECK_SECONDS:
        return

    STATE["last_be_check"] = time.time()

    side = STATE["position"]
    entry = float(STATE["entry_price"])
    current_stop = float(STATE["stop_price"])

    initial_risk = abs(entry - current_stop)

    if initial_risk <= 0 or current_atr <= 0:
        return

    if side == "BUY":
        trigger = entry + BREAKEVEN_TRIGGER_R * initial_risk
        new_stop = entry + BREAKEVEN_BUFFER_ATR * current_atr
        should_move = (
            current_price >= trigger
            and new_stop > current_stop
        )
    else:
        trigger = entry - BREAKEVEN_TRIGGER_R * initial_risk
        new_stop = entry - BREAKEVEN_BUFFER_ATR * current_atr
        should_move = (
            current_price <= trigger
            and new_stop < current_stop
        )

    if not should_move:
        return

    try:
        orders = client.futures_get_open_orders(symbol=SYMBOL)

        for order in orders:
            cid = str(order.get("clientOrderId", ""))

            if (
                order.get("type") == "STOP_MARKET"
                and cid.startswith("A5_")
            ):
                try:
                    client.futures_cancel_order(
                        symbol=SYMBOL,
                        orderId=order["orderId"],
                    )
                except Exception:
                    pass

        close_side = "SELL" if side == "BUY" else "BUY"

        new_stop = round_tick(
            new_stop,
            SYMBOL_FILTERS["tick_size"],
        )

        client.futures_create_order(
            symbol=SYMBOL,
            side=close_side,
            type="STOP_MARKET",
            stopPrice=new_stop,
            closePosition=True,
            workingType="MARK_PRICE",
            newClientOrderId=(
                f"A5_BE_{int(time.time() * 1000) % 1_000_000_000}"
            ),
        )

        STATE["stop_price"] = new_stop
        STATE["breakeven_done"] = True
        save_state()

        send_telegram(
            f"🔒 *Break-even moved* {side}\n"
            f"New SL: `${new_stop:.2f}`"
        )

    except Exception as exc:
        print(f"Break-even error: {exc}")

# ----------------------- TRADE EXECUTION ----------------------
def open_trade(setup: dict) -> bool:
    side = setup["side"]
    signal_price = float(setup["price"])
    signal_stop = float(setup["stop"])
    rr = float(setup["rr"])

    try:
        mark = float(
            client.futures_mark_price(symbol=SYMBOL)["markPrice"]
        )
    except Exception as exc:
        print(f"Mark price lookup failed: {exc}")
        return False

    # Do not chase a stale signal.
    chase_limit = 0.45 * float(setup["atr"])

    if abs(mark - signal_price) > chase_limit:
        print(
            "Trade rejected: signal moved too far "
            f"({abs(mark - signal_price):.2f} > {chase_limit:.2f})"
        )
        return False

    if side == "BUY":
        stop = min(signal_stop, mark - 1e-9)
        risk_distance = mark - stop
        target = mark + rr * risk_distance
    else:
        stop = max(signal_stop, mark + 1e-9)
        risk_distance = stop - mark
        target = mark - rr * risk_distance

    if risk_distance <= 0:
        return False

    qty = calculate_quantity(mark, stop)

    if qty <= 0:
        print(
            "Trade rejected: quantity too small or actual risk "
            "would exceed the safety ceiling."
        )
        return False

    try:
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=qty,
            newOrderRespType="RESULT",
            newClientOrderId=(
                f"A5_ENT_{int(time.time() * 1000) % 1_000_000_000}"
            ),
        )

        avg_price = float(
            order.get("avgPrice")
            or order.get("price")
            or mark
        )

        if avg_price <= 0:
            avg_price = mark

        if side == "BUY":
            actual_stop = avg_price - risk_distance
            actual_target = avg_price + rr * risk_distance
        else:
            actual_stop = avg_price + risk_distance
            actual_target = avg_price - rr * risk_distance

        try:
            stop_order, tp_order = place_protection(
                side,
                actual_stop,
                actual_target,
            )
        except Exception:
            emergency_close(side, qty)
            raise

        STATE.update({
            "position": side,
            "entry_price": avg_price,
            "stop_price": actual_stop,
            "target_price": actual_target,
            "atr_at_entry": setup["atr"],
            "qty": qty,
            "entry_time": int(time.time() * 1000),
            "entry_order_id": order.get("orderId"),
            "stop_order_id": stop_order.get("orderId"),
            "tp_order_id": tp_order.get("orderId"),
            "last_trade_time": time.time(),
            "daily_trade_count": (
                int(STATE["daily_trade_count"]) + 1
            ),
            "breakeven_done": False,
            "last_be_check": 0.0,
        })

        save_state()

        send_telegram(
            f"🤖 *A+ v5 MICRO {side}*\n\n"
            f"Pair: `{SYMBOL}`\n"
            f"Score: `{setup['score']}`\n"
            f"Entry: `${avg_price:.2f}`\n"
            f"SL: `${actual_stop:.2f}`\n"
            f"TP: `${actual_target:.2f}`\n"
            f"RR: `{rr:.2f}`\n"
            f"RSI: `{setup['rsi']:.1f}` → `{setup['rsi_prev']:.1f}`\n"
            f"Vol: `{setup['vol_ratio']:.2f}x`\n"
            f"Reasons: {', '.join(setup['reasons'])}"
        )

        print(
            f"TRADE OPENED {side} "
            f"qty={qty} entry={avg_price} "
            f"sl={actual_stop} tp={actual_target}"
        )
        return True

    except Exception as exc:
        print(f"Trade execution failed: {exc}")
        send_telegram(
            f"⚠️ *Trade execution failed:* `{exc}`"
        )
        return False

# ------------------------ RESULT RECONCILE --------------------
def reconcile_position_and_result() -> None:
    current_side, qty, entry = get_position()

    if current_side:
        if STATE["position"] is None:
            STATE["position"] = current_side
            STATE["qty"] = qty
            STATE["entry_price"] = entry
            save_state()
        return

    if STATE["position"] is None:
        return

    old_side = STATE["position"]
    start_ms = int(STATE.get("entry_time", 0) or 0)
    net_pnl = realized_pnl_since(start_ms)

    if net_pnl < -1e-9:
        STATE["consecutive_losses"] = (
            int(STATE["consecutive_losses"]) + 1
        )
        outcome = "LOSS"

    elif net_pnl > 1e-9:
        STATE["consecutive_losses"] = 0
        outcome = "WIN"

    else:
        outcome = "FLAT"

    cancel_protection_orders()

    print(
        f"Position closed: {old_side} "
        f"net_pnl={net_pnl:.4f} "
        f"outcome={outcome}"
    )

    send_telegram(
        f"✅ *TRADE CLOSED*\n"
        f"Side: `{old_side}`\n"
        f"Net PnL: `{net_pnl:.4f} USDT`\n"
        f"Outcome: `{outcome}`\n"
        f"Consecutive losses: `{STATE['consecutive_losses']}`"
    )

    reset_position_state()
    save_state()

# ------------------------- MAIN LOOP --------------------------
def trading_loop() -> None:
    print("🤖 Ultra A+ v5 Micro-50 engine active")
    print(
        f"Testnet={TESTNET}, "
        f"Symbol={SYMBOL}, "
        f"MinScore={MIN_SCORE}, "
        f"Risk={RISK_PER_TRADE * 100:.2f}%, "
        f"MaxDaily={MAX_DAILY_TRADES}, "
        f"Leverage={LEVERAGE}x"
    )

    load_state()
    load_exchange_filters()
    configure_account()
    initialize_risk_windows()
    recover_position_once()

    send_telegram(
        f"🚀 *Ultra A+ v5 Micro-50 Active*\n"
        f"Mode: `{'TESTNET' if TESTNET else 'LIVE'}`\n"
        f"Pair: `{SYMBOL}`\n"
        f"Min Score: `{MIN_SCORE}`\n"
        f"Risk/Trade: `{RISK_PER_TRADE * 100:.2f}%`\n"
        f"Leverage: `{LEVERAGE}x`\n"
        f"Daily Max Trades: `{MAX_DAILY_TRADES}`\n"
        f"Daily Max Loss: `{MAX_DAILY_LOSS_PCT * 100:.1f}%`\n"
        f"Monthly Max Loss: `{MAX_MONTHLY_LOSS_PCT * 100:.1f}%`\n"
        f"Monthly Goal Alert: `${MONTHLY_PROFIT_TARGET_USDT:.2f}`"
    )

    last_closed_candle = None
    last_status_print = 0.0

    while True:
        try:
            initialize_risk_windows()

            df15 = add_indicators(
                get_klines(TIMEFRAME, CANDLES_15M)
            )

            row = closed(df15)
            candle_id = int(row["time"])

            current_price = float(df15["close"].iloc[-1])

            current_atr = (
                float(df15["atr"].iloc[-1])
                if not pd.isna(df15["atr"].iloc[-1])
                else 0.0
            )

            maybe_move_to_breakeven(
                current_price,
                current_atr,
            )

            if candle_id == last_closed_candle:
                time.sleep(LOOP_SECONDS)
                continue

            last_closed_candle = candle_id
            STATE["last_signal_candle"] = candle_id

            reconcile_position_and_result()

            if STATE["position"] is not None:
                time.sleep(LOOP_SECONDS)
                continue

            if not can_trade():
                if time.time() - last_status_print > 300:
                    print(
                        "No trade: "
                        f"daily={STATE['daily_trade_count']} "
                        f"losses={STATE['consecutive_losses']} "
                        f"safety={safety_window_ok()} "
                        f"cooldown={cooldown_ok()}"
                    )
                    last_status_print = time.time()

                time.sleep(LOOP_SECONDS)
                continue

            df1h = add_indicators(
                get_klines(TREND_1H, CANDLES_1H)
            )
            df4h = add_indicators(
                get_klines(TREND_4H, CANDLES_4H)
            )

            side, setup = build_signal(
                df15,
                df1h,
                df4h,
            )

            if side and setup:
                print(
                    f"A+ v5 setup: {side} "
                    f"score={setup['score']} "
                    f"RR={setup['rr']:.2f} "
                    f"RSI={setup['rsi']:.1f} "
                    f"Vol={setup['vol_ratio']:.2f}x"
                )

                open_trade(setup)

            else:
                if time.time() - last_status_print > 300:
                    print("No A+ v5 setup. Staying flat.")
                    last_status_print = time.time()

            time.sleep(LOOP_SECONDS)

        except Exception as exc:
            print(f"Main loop error: {exc}")
            send_telegram(
                f"⚠️ *Main loop error:* `{exc}`"
            )
            time.sleep(240)

# ---------------------------- MAIN -----------------------------
if __name__ == "__main__":
    threading.Thread(
        target=run_flask,
        daemon=True,
    ).start()

    trading_loop()
