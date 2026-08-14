import os
import time
import threading
import requests
import pandas as pd
import numpy as np
import ta
from binance.client import Client
from flask import Flask

# Render পোর্ট এরর এড়াতে Flask ওয়েব সার্ভার
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running live!"

def run_flask():
    # Render ডাইনামিক পোর্ট ব্যবহার করে, সেটিংস অটো-ম্যাচ করার জন্য
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# ট্রেডিং সেটিংস
SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"
binance_client = Client()

def send_telegram_msg(msg):
    try:
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"❌ Telegram Error: {e}")
        def get_4h_trend():
    try:
        klines = binance_client.futures_klines(symbol=SYMBOL, interval="4h", limit=100)
        df_4h = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
        df_4h['close'] = df_4h['close'].astype(float)
        
        ema_50 = ta.trend.EMAIndicator(df_4h['close'], window=50).ema_indicator().iloc[-2]
        ema_200 = ta.trend.EMAIndicator(df_4h['close'], window=200).ema_indicator().iloc[-2]
        
        if ema_50 > ema_200:
            return "BULLISH"
        elif ema_50 < ema_200:
            return "BEARISH"
        return "SIDEWAYS"
    except Exception as e:
        print(f"❌ 4H Trend Check Error: {e}")
        return "NEUTRAL"

def calculate_dynamic_rr(df):
    current_vol = df['volume'].iloc[-2]
    vol_sma = df['vol_sma'].iloc[-2]
    rsi = df['rsi'].iloc[-2]

    if current_vol > (vol_sma * 2.0) and (rsi > 65 or rsi < 35):
        return 5.0
    elif current_vol > (vol_sma * 1.3):
        return 3.0
    else:
        return 2.0

def get_data():
    try:
        klines = binance_client.futures_klines(symbol=SYMBOL, interval=TIMEFRAME, limit=100)
        df = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        macd = ta.trend.MACD(df['close'])
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        df['ema_50'] = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
        df['ema_200'] = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator()
        
        bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
        df['bb_high'] = bb.bollinger_hband()
        df['bb_low'] = bb.bollinger_lband()
        
        df['support'] = df['low'].rolling(window=20).min()
        df['resistance'] = df['high'].rolling(window=20).max()
        df['vol_sma'] = df['volume'].rolling(window=20).mean()
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        
        return df
    except Exception as e:
        print(f"❌ Data Fetch Error: {e}")
        return None

def analyze_hybrid_strategy(df):
    trend_4h = get_4h_trend()
    curr_close = df['close'].iloc[-2]
    prev_close = df['close'].iloc[-3]
    curr_open = df['open'].iloc[-2]
    prev_open = df['open'].iloc[-3]
    curr_high = df['high'].iloc[-2]
    curr_low = df['low'].iloc[-2]
    curr_vol = df['volume'].iloc[-2]
    
    rsi = df['rsi'].iloc[-2]
    macd = df['macd'].iloc[-2]
    macd_signal = df['macd_signal'].iloc[-2]
    ema_50 = df['ema_50'].iloc[-2]
    ema_200 = df['ema_200'].iloc[-2]
    bb_high = df['bb_high'].iloc[-2]
    bb_low = df['bb_low'].iloc[-2]
    
    support = df['support'].iloc[-2]
    resistance = df['resistance'].iloc[-2]
    vol_sma = df['vol_sma'].iloc[-2]
    current_atr = df['atr'].iloc[-2]

    buy_conditions = [
    rsi < 45,
    macd > macd_signal,
    ema_50 > ema_200,
    curr_close <= bb_low * 1.005,
    curr_close > prev_close,
    trend_4h == "BULLISH"
]

sell_conditions = [
    rsi > 55,
    macd < macd_signal,
    ema_50 < ema_200,
    curr_close >= bb_high * 0.995,
    curr_close < prev_close,
    trend_4h == "BEARISH"
]
    body = abs(curr_close - curr_open)
    lower_shadow = min(curr_close, curr_open) - curr_low
    upper_shadow = curr_high - max(curr_close, curr_open)
    
    is_bullish_pattern = (prev_close < prev_open and curr_close > prev_open) or (lower_shadow > 2 * body)
    is_bearish_pattern = (prev_close > prev_open and curr_close < prev_open) or (upper_shadow > 2 * body)
    
    near_support = abs(curr_low - support) / support < 0.003 or curr_close > resistance
    near_resistance = abs(curr_high - resistance) / resistance < 0.003 or curr_close < support

    good_volume = curr_vol > (vol_sma * 1.2)

    rr_multiplier = calculate_dynamic_rr(df)

    if sum(buy_conditions) >= 4 and (is_bullish_pattern or near_support) and good_volume:
        return "BUY", curr_close, current_atr, rr_multiplier

    if sum(sell_conditions) >= 4 and (is_bearish_pattern or near_resistance) and good_volume:
        return "SELL", curr_close, current_atr, rr_multiplier

    return None, None, None, None

def trading_loop():
    print("🤖 Ultra-Pro Trading Engine Active...")
    send_telegram_msg("🚀 *Ultra-Pro Bot Active!*\n✅ Indicators & Price Action\n✅ Volume Filter\n✅ Dynamic ATR\n✅ Trailing SL\n✅ Server Port Fixed")
    
    active_position = None
    target_tp = 0
    current_sl = 0

    while True:
        try:
            df = get_data()
            if df is not None:
                curr_price = df['close'].iloc[-1]
                current_atr = df['atr'].iloc[-1]

                if active_position == "BUY":
                    new_sl = curr_price - (1.5 * current_atr)
                    if new_sl > current_sl:
                        current_sl = new_sl

                    if curr_price >= target_tp:
                        send_telegram_msg(f"✅ *TP HIT!* BUY Trade Closed at ${curr_price:.2f} 🎯")
                        active_position = None
                    elif curr_price <= current_sl:
                        send_telegram_msg(f"🛑 *SL HIT!* BUY Trade Closed at ${curr_price:.2f}")
                        active_position = None

                elif active_position == "SELL":
                    new_sl = curr_price + (1.5 * current_atr)
                    if current_sl == 0 or new_sl < current_sl:
                        current_sl = new_sl

                    if curr_price <= target_tp:
                        send_telegram_msg(f"✅ *TP HIT!* SELL Trade Closed at ${curr_price:.2f} 🎯")
                        active_position = None
                    elif curr_price >= current_sl:
                        send_telegram_msg(f"🛑 *SL HIT!* SELL Trade Closed at ${curr_price:.2f}")
                        active_position = None

                if not active_position:
    side, price, atr_val, rr_mult = analyze_hybrid_strategy(df)
    if side:
        active_position = side

        if side == "BUY":
            current_sl = price - (1.5 * atr_val)
            target_tp = price + (rr_mult * atr_val)
        else:
            current_sl = price + (1.5 * atr_val)
            target_tp = price - (rr_mult * atr_val)
                        
                        msg = f"🚨 *PRO {side} SIGNAL DETECTED!*\n\n" \
                              f"💎 *Pair:* {SYMBOL}\n" \
                              f"💰 *Entry:* ${price:.2f}\n" \
                              f"🎯 *Dynamic TP:* ${target_tp:.2f}\n" \
                              f"🛑 *Dynamic SL:* ${current_sl:.2f}\n" \
                              f"📊 *ATR Value:* {atr_val:.2f}\n\n" \
                              f"⚡ *High Volume Setup Confirmed!*"
                        
                        send_telegram_msg(msg)
                        time.sleep(300)

            time.sleep(60)

        except Exception as e:
            print(f"❌ Execution Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    # ব্যাকগ্রাউন্ডে ফ্ল্যাস্ক ওয়েবসাইট চালু হবে যাতে Render পোর্ট এরর না দেয়
    threading.Thread(target=run_flask, daemon=True).start()
    # মেইন ট্রেডিং লুপ চালু
    trading_loop()
