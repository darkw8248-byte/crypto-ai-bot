import os
import time
import threading
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple

import pandas as pd
import requests
import ta
from flask import Flask
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ============================================================
# ULTRA A+ LOW-FREQUENCY BTCUSDT FUTURES BOT v3
# - Strict multi-timeframe confirmation: 4H -> 1H -> 15m
# - Low API usage: analyze only when a NEW 15m candle closes
# - Exchange-side STOP_MARKET + TAKE_PROFIT_MARKET protection
# - Risk-based position size instead of fixed BTC quantity
# - Optional break-even trailing, throttled to reduce API usage
# - Designed for small-memory Render deployment
# - TESTNET by default. Do NOT enable live trading until tested.
# ============================================================

# ------------------------- APP -------------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Ultra A+ Bot v3 is running"


def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


# ---------------------- SETTINGS ------------------------------
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
TIMEFRAME = "15m"
TREND_1H = "1h"
TREND_4H = "4h"

# Safety: keep testnet=True unless you deliberately change this.
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

API_KEY = os.getenv("BINANCE_TESTNET_KEY") if TESTNET else os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_TESTNET_SECRET") if TESTNET else os.getenv("BINANCE_SECRET_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Risk controls
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.005"))  # 0.5% of available USDT balance
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "3"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "45"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "90"))
MIN_RR = float(os.getenv("MIN_RR", "2.0"))
MAX_RR = float(os.getenv("MAX_RR", "3.5"))

# Exchange/order safety
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
USE_ISOLATED = os.getenv("USE_ISOLATED", "true").lower() == "true"

# Optional break-even. Disabled by default to minimize order-management calls.
ENABLE_BREAKEVEN = os.getenv("ENABLE_BREAKEVEN", "false").lower() == "true"
BREAKEVEN_TRIGGER_R = float(os.getenv("BREAKEVEN_TRIGGER_R", "1.2"))
BREAKEVEN_BUFFER_ATR = float(os.getenv("BREAKEVEN_BUFFER_ATR", "0.08"))
BREAKEVEN_CHECK_SECONDS = int(os.getenv("BREAKEVEN_CHECK_SECONDS", "300"))

# Loop timing. 60s means roughly 1 public market-data request/minute.
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "60"))

# Candle history. Keep it small for Render RAM usage.
CANDLES_15M = int(os.getenv("CANDLES_15M", "220"))
CANDLES_1H = int(os.getenv("CANDLES_1H", "220"))
CANDLES_4H = int(os.getenv("CANDLES_4H", "220"))

# --------------------------------------------------------------
if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing Binance API credentials in environment variables")

client = Client(API_KEY, API_SECRET, testnet=TESTNET, requests_params={"timeout": 12})

# Cached exchange filters - loaded once at startup.
SYMBOL_FILTERS = {
    "step_size": 0.001,
    "min_qty": 0.001,
    "tick_size": 0.10,
    "min_notional": 0.0,
}

# Local state: no repeated position endpoint polling.
STATE = {
    "last_signal_candle": None,
    "last_trade_time": 0.0,
    "daily_trade_count": 0,
    "daily_date": None,
    "consecutive_losses": 0,
    "position": None,  # BUY / SELL / None
    "entry_price": 0.0,
    "stop_price": 0.0,
    "target_price": 0.0,
    "atr_at_entry": 0.0,
    "qty": 0.0,
    "breakeven_done": False,
    "last_be_check": 0.0,
}


# ---------------------- UTILITIES -----------------------------
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


def round_step(value: float, step: float) -> float:
    """Round DOWN to Binance step size."""
    if step <= 0:
        return value
    value_d = Decimal(str(value))
    step_d = Decimal(str(step))
    return float((value_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d)


def round_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    value_d = Decimal(str(value))
    tick_d = Decimal(str(tick))
    return float((value_d / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d)


def today_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def reset_daily_counter_if_needed() -> None:
    key = today_key()
    if STATE["daily_date"] != key:
        STATE["daily_date"] = key
        STATE["daily_trade_count"] = 0
        STATE["consecutive_losses"] = 0


def cooldown_ok() -> bool:
    return (time.time() - STATE["last_trade_time"]) >= COOLDOWN_MINUTES * 60


# ---------------------- EXCHANGE SETUP ------------------------
def load_exchange_filters() -> None:
    info = client.futures_exchange_info()
    symbol_info = next((s for s in info["symbols"] if s["symbol"] == SYMBOL), None)
    if not symbol_info:
        raise RuntimeError(f"Symbol not found: {SYMBOL}")

    for f in symbol_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            SYMBOL_FILTERS["step_size"] = float(f["stepSize"])
            SYMBOL_FILTERS["min_qty"] = float(f["minQty"])
        elif f["filterType"] == "PRICE_FILTER":
            SYMBOL_FILTERS["tick_size"] = float(f["tickSize"])
        elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            SYMBOL_FILTERS["min_notional"] = float(f.get("notional", f.get("minNotional", 0.0)))

    print(f"Exchange filters loaded: {SYMBOL_FILTERS}")


def configure_account() -> None:
    try:
        client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        if USE_ISOLATED:
            try:
                client.futures_change_margin_type(symbol=SYMBOL, marginType="ISOLATED")
            except BinanceAPIException as exc:
                # Already isolated is harmless; continue.
                print(f"Margin type note: {exc}")
    except Exception as exc:
        print(f"Account configuration warning: {exc}")


def recover_position_once() -> None:
    """One exchange-state check at startup only. No polling loop."""
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        p = next((x for x in positions if x["symbol"] == SYMBOL and abs(float(x["positionAmt"])) > 0), None)
        if p:
            amt = float(p["positionAmt"])
            STATE["position"] = "BUY" if amt > 0 else "SELL"
            STATE["entry_price"] = float(p["entryPrice"])
            STATE["qty"] = abs(amt)
            print(f"Recovered position: {STATE['position']} qty={STATE['qty']} entry={STATE['entry_price']}")
            send_telegram(
                f"⚠️ *Existing Binance position detected*\n"
                f"Side: {STATE['position']}\nQty: {STATE['qty']}\nEntry: {STATE['entry_price']:.2f}\n"
                f"Bot will NOT open a second position."
            )
        else:
            STATE["position"] = None

        open_orders = client.futures_get_open_orders(symbol=SYMBOL)
        if open_orders:
            print(f"Existing open orders: {len(open_orders)}")
    except Exception as exc:
        print(f"Startup reconciliation warning: {exc}")


# ---------------------- MARKET DATA ----------------------------
KLINE_COLUMNS = [
    "time", "open", "high", "low", "close", "volume",
    "close_time", "qav", "num_trades", "taker_base_vol",
    "taker_quote_vol", "ignore"
]


def get_klines(interval: str, limit: int) -> pd.DataFrame:
    klines = client.futures_klines(symbol=SYMBOL, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=KLINE_COLUMNS)
    numeric = ["open", "high", "low", "close", "volume"]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_numeric(df["time"], errors="coerce").astype("int64")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # EMA trend
    df["ema20"] = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()

    # Momentum
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd = ta.trend.MACD(df["close"], window_fast=12, window_slow=26, window_sign=9)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()
    df["adx"] = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()

    # Volatility
    atr = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14)
    df["atr"] = atr.average_true_range()

    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()

    # Volume
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    # Rolling VWAP approximation using recent 48 bars.
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"]
    df["vwap48"] = pv.rolling(48).sum() / df["volume"].rolling(48).sum()

    # Structure levels.
    df["support48"] = df["low"].rolling(48).min()
    df["resistance48"] = df["high"].rolling(48).max()

    return df


def closed(df: pd.DataFrame) -> pd.Series:
    # Last candle may still be forming. Always analyze the previous closed candle.
    return df.iloc[-2]


# ---------------------- A+ STRATEGY ----------------------------
def candle_features(row: pd.Series, prev: pd.Series) -> dict:
    body = abs(float(row["close"]) - float(row["open"]))
    lower = min(float(row["open"]), float(row["close"])) - float(row["low"])
    upper = float(row["high"]) - max(float(row["open"]), float(row["close"]))

    bullish_engulf = (
        float(prev["close"]) < float(prev["open"])
        and float(row["close"]) > float(row["open"])
        and float(row["close"]) >= float(prev["open"])
        and float(row["open"]) <= float(prev["close"])
    )
    bearish_engulf = (
        float(prev["close"]) > float(prev["open"])
        and float(row["close"]) < float(row["open"])
        and float(row["close"]) <= float(prev["open"])
        and float(row["open"]) >= float(prev["close"])
    )

    bullish_rejection = body > 0 and lower >= 1.8 * body and float(row["close"]) > float(row["open"])
    bearish_rejection = body > 0 and upper >= 1.8 * body and float(row["close"]) < float(row["open"])

    return {
        "bullish": bullish_engulf or bullish_rejection,
        "bearish": bearish_engulf or bearish_rejection,
    }


def liquidity_sweep(row: pd.Series, prev: pd.Series, side: str) -> bool:
    if side == "BUY":
        return float(row["low"]) < float(prev["low"]) and float(row["close"]) > float(prev["low"])
    return float(row["high"]) > float(prev["high"]) and float(row["close"]) < float(prev["high"])


def build_signal(df15: pd.DataFrame, df1h: pd.DataFrame, df4h: pd.DataFrame) -> Tuple[Optional[str], Optional[dict]]:
    row = closed(df15)
    prev = df15.iloc[-3]
    h1 = closed(df1h)
    h4 = closed(df4h)

    values = [row["atr"], row["adx"], row["rsi"], h1["adx"], h4["adx"], row["vwap48"]]
    if any(pd.isna(v) for v in values):
        return None, None

    price = float(row["close"])
    atr = float(row["atr"])
    atr_pct = atr / price if price else 0
    vol_ratio = float(row["volume"] / row["vol_sma20"]) if row["vol_sma20"] else 0
    candle = candle_features(row, prev)

    # Market regime: avoid very weak/sideways 4H trend.
    h4_bull = float(h4["ema50"]) > float(h4["ema200"])
    h4_bear = float(h4["ema50"]) < float(h4["ema200"])
    h4_strong = float(h4["adx"]) >= 18

    # 1H alignment.
    h1_bull = float(h1["ema20"]) > float(h1["ema50"]) > float(h1["ema200"])
    h1_bear = float(h1["ema20"]) < float(h1["ema50"]) < float(h1["ema200"])
    h1_strong = float(h1["adx"]) >= 18

    # 15m trend/momentum.
    m15_bull = float(row["ema20"]) > float(row["ema50"])
    m15_bear = float(row["ema20"]) < float(row["ema50"])
    rsi = float(row["rsi"])
    macd_hist = float(row["macd_hist"])
    vwap = float(row["vwap48"])

    # Avoid extremely dead markets.
    if atr_pct < 0.0008 or atr_pct > 0.035:
        return None, None

    # BUY score
    buy = 0
    buy_reasons = []

    if h4_bull:
        buy += 20; buy_reasons.append("4H bullish trend")
    if h4_strong:
        buy += 5; buy_reasons.append("4H ADX strong")
    if h1_bull:
        buy += 15; buy_reasons.append("1H alignment")
    if h1_strong:
        buy += 5; buy_reasons.append("1H ADX strong")
    if m15_bull:
        buy += 5; buy_reasons.append("15m EMA alignment")
    if price > vwap:
        buy += 5; buy_reasons.append("above VWAP")
    if 38 <= rsi <= 55:
        buy += 5; buy_reasons.append("RSI pullback zone")
    if macd_hist > 0:
        buy += 5; buy_reasons.append("MACD positive")
    if vol_ratio >= 1.3:
        buy += 10; buy_reasons.append("volume expansion")
    if liquidity_sweep(row, prev, "BUY"):
        buy += 10; buy_reasons.append("bullish liquidity sweep")
    if candle["bullish"]:
        buy += 5; buy_reasons.append("bullish candle confirmation")

    # SELL score
    sell = 0
    sell_reasons = []

    if h4_bear:
        sell += 20; sell_reasons.append("4H bearish trend")
    if h4_strong:
        sell += 5; sell_reasons.append("4H ADX strong")
    if h1_bear:
        sell += 15; sell_reasons.append("1H alignment")
    if h1_strong:
        sell += 5; sell_reasons.append("1H ADX strong")
    if m15_bear:
        sell += 5; sell_reasons.append("15m EMA alignment")
    if price < vwap:
        sell += 5; sell_reasons.append("below VWAP")
    if 45 <= rsi <= 62:
        sell += 5; sell_reasons.append("RSI pullback zone")
    if macd_hist < 0:
        sell += 5; sell_reasons.append("MACD negative")
    if vol_ratio >= 1.3:
        sell += 10; sell_reasons.append("volume expansion")
    if liquidity_sweep(row, prev, "SELL"):
        sell += 10; sell_reasons.append("bearish liquidity sweep")
    if candle["bearish"]:
        sell += 5; sell_reasons.append("bearish candle confirmation")

    # Must have direction-specific structure and avoid tie signals.
    side = None
    score = 0
    reasons = []
    if buy >= MIN_SCORE and buy >= sell + 10:
        side, score, reasons = "BUY", buy, buy_reasons
    elif sell >= MIN_SCORE and sell >= buy + 10:
        side, score, reasons = "SELL", sell, sell_reasons
    else:
        return None, None

    # Stop placement based on ATR + recent structure.
    support = float(row["support48"])
    resistance = float(row["resistance48"])

    if side == "BUY":
        structural_sl = support - 0.25 * atr
        atr_sl = price - 1.6 * atr
        stop = min(structural_sl, atr_sl)
        risk_distance = price - stop
        room = resistance - price
    else:
        structural_sl = resistance + 0.25 * atr
        atr_sl = price + 1.6 * atr
        stop = max(structural_sl, atr_sl)
        risk_distance = stop - price
        room = price - support

    if risk_distance <= 0 or room <= 0:
        return None, None

    # RR depends on available structural room, capped to avoid unrealistic targets.
    structural_rr = room / risk_distance
    rr = min(MAX_RR, max(MIN_RR, structural_rr * 0.9))

    # Reject setup if nearby structure does not leave enough room for target.
    if structural_rr < MIN_RR:
        return None, None

    target = price + rr * risk_distance if side == "BUY" else price - rr * risk_distance

    # Final sanity checks.
    if side == "BUY" and not (stop < price < target):
        return None, None
    if side == "SELL" and not (stop > price > target):
        return None, None

    setup = {
        "side": side,
        "score": score,
        "price": price,
        "stop": stop,
        "target": target,
        "risk_distance": risk_distance,
        "rr": rr,
        "atr": atr,
        "vol_ratio": vol_ratio,
        "rsi": rsi,
        "reasons": reasons,
        "candle_time": int(row["time"]),
    }
    return side, setup


# --------------------- POSITION SIZING ------------------------
def get_available_usdt() -> float:
    balances = client.futures_account_balance()
    for item in balances:
        if item.get("asset") == "USDT":
            return max(0.0, float(item.get("availableBalance", 0.0)))
    return 0.0


def calculate_quantity(entry: float, stop: float) -> float:
    balance = get_available_usdt()
    risk_usdt = balance * RISK_PER_TRADE
    stop_distance = abs(entry - stop)

    if balance <= 0 or risk_usdt <= 0 or stop_distance <= 0:
        return 0.0

    # Quantity = money willing to lose / price distance.
    raw_qty = risk_usdt / stop_distance
    qty = round_step(raw_qty, SYMBOL_FILTERS["step_size"])

    if qty < SYMBOL_FILTERS["min_qty"]:
        return 0.0

    notional = qty * entry
    min_notional = SYMBOL_FILTERS.get("min_notional", 0.0)
    if min_notional and notional < min_notional:
        return 0.0

    return qty


# ---------------------- ORDER FUNCTIONS -----------------------
def place_protection(side: str, stop: float, target: float) -> Tuple[dict, dict]:
    close_side = "SELL" if side == "BUY" else "BUY"
    stop = round_tick(stop, SYMBOL_FILTERS["tick_size"])
    target = round_tick(target, SYMBOL_FILTERS["tick_size"])

    stop_order = client.futures_create_order(
        symbol=SYMBOL,
        side=close_side,
        type="STOP_MARKET",
        stopPrice=stop,
        closePosition=True,
        workingType="MARK_PRICE",
    )

    try:
        tp_order = client.futures_create_order(
            symbol=SYMBOL,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=target,
            closePosition=True,
            workingType="MARK_PRICE",
        )
    except Exception:
        # If TP failed after SL succeeded, keep SL and fail the trade state.
        try:
            client.futures_cancel_order(symbol=SYMBOL, orderId=stop_order.get("orderId"))
        except Exception:
            pass
        raise

    return stop_order, tp_order


def open_trade(setup: dict) -> bool:
    side = setup["side"]
    entry = setup["price"]
    stop = setup["stop"]
    target = setup["target"]

    qty = calculate_quantity(entry, stop)
    if qty <= 0:
        print("Trade rejected: calculated quantity is below exchange minimum or balance unavailable")
        return False

    try:
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=qty,
            newOrderRespType="RESULT",
        )

        # Use exchange fill price when available.
        avg_price = float(order.get("avgPrice") or order.get("price") or entry)
        if avg_price <= 0:
            avg_price = entry

        # Recalculate protections from actual fill to keep risk coherent.
        distance = abs(entry - stop)
        if side == "BUY":
            actual_stop = avg_price - distance
            actual_target = avg_price + setup["rr"] * distance
        else:
            actual_stop = avg_price + distance
            actual_target = avg_price - setup["rr"] * distance

        stop_order, tp_order = place_protection(side, actual_stop, actual_target)

        STATE.update({
            "position": side,
            "entry_price": avg_price,
            "stop_price": actual_stop,
            "target_price": actual_target,
            "atr_at_entry": setup["atr"],
            "qty": qty,
            "last_trade_time": time.time(),
            "daily_trade_count": STATE["daily_trade_count"] + 1,
            "breakeven_done": False,
        })

        send_telegram(
            f"🚨 *A+ {side} SIGNAL EXECUTED*\n\n"
            f"Pair: `{SYMBOL}`\n"
            f"Score: `{setup['score']}/100`\n"
            f"Qty: `{qty}`\n"
            f"Entry: `${avg_price:.2f}`\n"
            f"SL: `${actual_stop:.2f}`\n"
            f"TP: `${actual_target:.2f}`\n"
            f"RR: `{setup['rr']:.2f}`\n"
            f"RSI: `{setup['rsi']:.1f}`\n"
            f"Vol: `{setup['vol_ratio']:.2f}x`\n"
            f"Reasons: {', '.join(setup['reasons'])}"
        )

        print(f"TRADE OPENED {side} qty={qty} entry={avg_price} sl={actual_stop} tp={actual_target}")
        return True

    except Exception as exc:
        print(f"Trade execution failed: {exc}")
        send_telegram(f"⚠️ *Trade execution failed:* {exc}")
        return False


# -------------------- OPTIONAL BREAK-EVEN ---------------------
def maybe_move_to_breakeven(current_price: float, current_atr: float) -> None:
    if not ENABLE_BREAKEVEN or not STATE["position"] or STATE["breakeven_done"]:
        return
    if (time.time() - STATE["last_be_check"]) < BREAKEVEN_CHECK_SECONDS:
        return

    STATE["last_be_check"] = time.time()
    side = STATE["position"]
    entry = STATE["entry_price"]
    initial_risk = abs(entry - STATE["stop_price"])
    if initial_risk <= 0:
        return

    if side == "BUY":
        trigger = entry + BREAKEVEN_TRIGGER_R * initial_risk
        new_stop = entry + BREAKEVEN_BUFFER_ATR * current_atr
        should_move = current_price >= trigger and new_stop > STATE["stop_price"]
    else:
        trigger = entry - BREAKEVEN_TRIGGER_R * initial_risk
        new_stop = entry - BREAKEVEN_BUFFER_ATR * current_atr
        should_move = current_price <= trigger and new_stop < STATE["stop_price"]

    if not should_move:
        return

    # Find existing stop orders once, cancel the stop and place a new exchange-side stop.
    try:
        open_orders = client.futures_get_open_orders(symbol=SYMBOL)
        stop_orders = [
            o for o in open_orders
            if o.get("type") in ("STOP_MARKET", "STOP")
            and o.get("reduceOnly") in (True, "true", None)
        ]

        for o in stop_orders:
            try:
                client.futures_cancel_order(symbol=SYMBOL, orderId=o["orderId"])
            except Exception:
                pass

        close_side = "SELL" if side == "BUY" else "BUY"
        new_stop = round_tick(new_stop, SYMBOL_FILTERS["tick_size"])
        client.futures_create_order(
            symbol=SYMBOL,
            side=close_side,
            type="STOP_MARKET",
            stopPrice=new_stop,
            closePosition=True,
            workingType="MARK_PRICE",
        )
        STATE["stop_price"] = new_stop
        STATE["breakeven_done"] = True
        send_telegram(f"🔒 *Break-even moved* {side}\nNew SL: `${new_stop:.2f}`")
    except Exception as exc:
        print(f"Break-even error: {exc}")


# ----------------------- CONTROL LOGIC ------------------------
def can_trade() -> bool:
    reset_daily_counter_if_needed()
    if STATE["position"] is not None:
        return False
    if STATE["daily_trade_count"] >= MAX_DAILY_TRADES:
        return False
    if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        return False
    if not cooldown_ok():
        return False
    return True


def trading_loop() -> None:
    print("🤖 Ultra A+ Low-Frequency Engine active")
    print(f"Testnet={TESTNET}, Symbol={SYMBOL}, MinScore={MIN_SCORE}, Risk={RISK_PER_TRADE * 100:.2f}%")

    load_exchange_filters()
    configure_account()
    recover_position_once()

    send_telegram(
        f"🚀 *Ultra A+ Bot v3 Active*\n"
        f"Mode: `{'TESTNET' if TESTNET else 'LIVE'}`\n"
        f"Pair: `{SYMBOL}`\n"
        f"Min Score: `{MIN_SCORE}`\n"
        f"Risk/Trade: `{RISK_PER_TRADE * 100:.2f}%`\n"
        f"Max Daily Trades: `{MAX_DAILY_TRADES}`"
    )

    last_closed_candle = None
    last_status_print = 0.0

    while True:
        try:
            reset_daily_counter_if_needed()

            # One 15m request per loop. Only do full multi-timeframe analysis
            # when the closed 15m candle changes.
            df15 = add_indicators(get_klines(TIMEFRAME, CANDLES_15M))
            row = closed(df15)
            candle_id = int(row["time"])

            current_price = float(df15["close"].iloc[-1])
            current_atr = float(df15["atr"].iloc[-1]) if not pd.isna(df15["atr"].iloc[-1]) else 0.0

            # Optional BE management happens only every few minutes.
            maybe_move_to_breakeven(current_price, current_atr)

            if candle_id == last_closed_candle:
                time.sleep(LOOP_SECONDS)
                continue

            last_closed_candle = candle_id
            STATE["last_signal_candle"] = candle_id

            # If a local position exists, do not create another one.
            if STATE["position"] is not None:
                print(f"Position already active: {STATE['position']}")
                time.sleep(LOOP_SECONDS)
                continue

            if not can_trade():
                print(
                    f"No trade: daily={STATE['daily_trade_count']} "
                    f"losses={STATE['consecutive_losses']} cooldown_ok={cooldown_ok()}"
                )
                time.sleep(LOOP_SECONDS)
                continue

            # Only on a fresh 15m close: +2 market-data calls.
            df1h = add_indicators(get_klines(TREND_1H, CANDLES_1H))
            df4h = add_indicators(get_klines(TREND_4H, CANDLES_4H))

            side, setup = build_signal(df15, df1h, df4h)
            if side and setup:
                print(
                    f"A+ setup: {side} score={setup['score']} "
                    f"RR={setup['rr']:.2f} RSI={setup['rsi']:.1f} Vol={setup['vol_ratio']:.2f}x"
                )
                open_trade(setup)
            else:
                if time.time() - last_status_print > 300:
                    print("No A+ setup. Staying flat.")
                    last_status_print = time.time()

            time.sleep(LOOP_SECONDS)

        except Exception as exc:
            print(f"Main loop error: {exc}")
            time.sleep(90)


# -------------------------- MAIN ------------------------------
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    trading_loop()
