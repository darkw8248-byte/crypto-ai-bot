import os
import time
import requests
import pandas as pd
import numpy as np
import ta
from binance.client import Client

# ট্রেডিং সেটিংস
SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"

def send_telegram_msg(msg):
    """টেলিগ্রামে মেসেজ পাঠানোর ফাংশন"""
    try:
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

def get_data():
    """মার্কেট ডেটা, ইন্ডিকেটর, ATR এবং ভলিউম ক্যালকুলেশন"""
    try:
        client = Client()
        klines = client.futures_klines(symbol=SYMBOL, interval=TIMEFRAME, limit=100)
        df = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        # ১. বেসিক ইন্ডিকেটর
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        macd = ta.trend.MACD(df['close'])
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        df['ema_50'] = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
        df['ema_200'] = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator()
        
        bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2)
        df['bb_high'] = bb.bollinger_hband()
        df['bb_low'] = bb.bollinger_lband()
        
        # ২. সাপোর্ট, রেজিস্ট্যান্স এবং ভলিউম ফিল্টার
        df['support'] = df['low'].rolling(window=20).min()
        df['resistance'] = df['high'].rolling(window=20).max()
        df['vol_sma'] = df['volume'].rolling(window=20).mean() # এভারেজ ভলিউম
        
        # ৩. ATR (ডাইনামিক স্টপ-লসের জন্য)
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        
        return df
    except Exception as e:
        print(f"❌ Data Fetch Error: {e}")
        return None

def analyze_hybrid_strategy(df):
    """প্রাইস অ্যাকশন + ইন্ডিকেটর + ভলিউম ফিল্টার লজিক"""
    curr_close = df['close'].iloc[-1]
    prev_close = df['close'].iloc[-2]
    curr_open = df['open'].iloc[-1]
    prev_open = df['open'].iloc[-2]
    curr_high = df['high'].iloc[-1]
    curr_low = df['low'].iloc[-1]
    curr_vol = df['volume'].iloc[-1]
    
    rsi = df['rsi'].iloc[-1]
    macd = df['macd'].iloc[-1]
    macd_signal = df['macd_signal'].iloc[-1]
    ema_50 = df['ema_50'].iloc[-1]
    ema_200 = df['ema_200'].iloc[-1]
    bb_high = df['bb_high'].iloc[-1]
    bb_low = df['bb_low'].iloc[-1]
    
    support = df['support'].iloc[-2]
    resistance = df['resistance'].iloc[-2]
    vol_sma = df['vol_sma'].iloc[-2]
    current_atr = df['atr'].iloc[-1]

    # কড়া ৬টি শর্ত
    buy_conditions = [
        rsi < 45,
        macd > macd_signal,
        curr_close > ema_50,
        ema_50 > ema_200,
        curr_close <= bb_low * 1.005,
        curr_close > prev_close
    ]
    
    sell_conditions = [
        rsi > 55,
        macd < macd_signal,
        curr_close < ema_50,
        ema_50 < ema_200,
        curr_close >= bb_high * 0.995,
        curr_close < prev_close
    ]

    # ক্যান্ডেলস্টিক প্যাটার্ন ও লেভেল লজিক
    body = abs(curr_close - curr_open)
    lower_shadow = min(curr_close, curr_open) - curr_low
    upper_shadow = curr_high - max(curr_close, curr_open)
    
    is_bullish_pattern = (prev_close < prev_open and curr_close > prev_open) or (lower_shadow > 2 * body)
    is_bearish_pattern = (prev_close > prev_open and curr_close < prev_open) or (upper_shadow > 2 * body)
    
    near_support = abs(curr_low - support) / support < 0.003 or curr_close > resistance
    near_resistance = abs(curr_high - resistance) / resistance < 0.003 or curr_close < support

    # ভলিউম স্পাইক (স্বাভাবিকের চেয়ে ২০% বেশি ভলিউম থাকতে হবে)
    good_volume = curr_vol > (vol_sma * 1.2)

    # যেকোনো ৪টি ইন্ডিকেটর শর্ত + লেভেল/প্যাটার্ন + ভালো ভলিউম
    if sum(buy_conditions) >= 4 and (is_bullish_pattern or near_support) and good_volume:
        return "BUY", curr_close, current_atr
        
    if sum(sell_conditions) >= 4 and (is_bearish_pattern or near_resistance) and good_volume:
        return "SELL", curr_close, current_atr

    return None, None, None

def trading_loop():
    """স্মার্ট ট্রেডিং ইঞ্জিন: ATR, Trailing SL এবং Auto Position Close সহ"""
    print("🤖 Ultra-Pro Trading Engine Active...")
    send_telegram_msg("🚀 *Ultra-Pro Bot Active!*\n✅ Indicators & Price Action\n✅ Volume Filter\n✅ Dynamic ATR (TP/SL)\n✅ Trailing SL Enabled")
    
    active_position = None
    target_tp = 0
    current_sl = 0

    while True:
        try:
            df = get_data()
            if df is not None:
                curr_price = df['close'].iloc[-1]
                current_atr = df['atr'].iloc[-1]

                # --- রানিং ট্রেড ম্যানেজমেন্ট (Trailing SL & Close Logic) ---
                if active_position == "BUY":
                    # ATR ভিত্তিক ট্রেইলিং স্টপ-লস (১.৫ গুন ATR)
                    new_sl = curr_price - (1.5 * current_atr)
                    if new_sl > current_sl:
                        current_sl = new_sl
                        print(f"🔒 BUY Trailing SL Moved Up: ${current_sl:.2f}")

                    # প্রফিট বা লস হিট করলে ট্রেড ক্লোজ
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
                        print(f"🔒 SELL Trailing SL Moved Down: ${current_sl:.2f}")

                    if curr_price <= target_tp:
                        send_telegram_msg(f"✅ *TP HIT!* SELL Trade Closed at ${curr_price:.2f} 🎯")
                        active_position = None
                    elif curr_price >= current_sl:
                        send_telegram_msg(f"🛑 *SL HIT!* SELL Trade Closed at ${curr_price:.2f}")
                        active_position = None

                # --- নতুন ট্রেড খোঁজা (যদি কোনো ট্রেড রানিং না থাকে) ---
                if not active_position:
                    side, price, atr_val = analyze_hybrid_strategy(df)
                    if side:
                        active_position = side
                        
                        # ATR ব্যবহার করে ১:২ Risk-Reward রেশিও
                        if side == "BUY":
                            current_sl = price - (1.5 * atr_val)
                            target_tp = price + (2.5 * atr_val)
                        else:
                            current_sl = price + (1.5 * atr_val)
                            target_tp = price - (2.5 * atr_val)
                        
                        msg = f"🚨 *PRO {side} SIGNAL DETECTED!*\n\n" \
                              f"💎 *Pair:* {SYMBOL}\n" \
                              f"💰 *Entry:* ${price:.2f}\n" \
                              f"🎯 *Dynamic TP:* ${target_tp:.2f}\n" \
                              f"🛑 *Dynamic SL:* ${current_sl:.2f}\n" \
                              f"📊 *ATR Value:* {atr_val:.2f}\n\n" \
                              f"⚡ *High Volume Setup Confirmed!*"
                        
                        send_telegram_msg(msg)
                        time.sleep(300) # বারবার সিগন্যাল ঠেকানোর জন্য ৫ মিনিটের ব্রেক

            # API লিমিট সুরক্ষিত রাখতে ৬০ সেকেন্ড অপেক্ষা
            time.sleep(60)

        except Exception as e:
            print(f"❌ Execution Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    trading_loop()
