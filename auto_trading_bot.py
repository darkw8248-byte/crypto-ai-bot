import os
import requests
import pandas as pd
import numpy as np
import ta
from binance.client import Client

# 🔑 ১. পরিবেশ পরিবর্তনশীল (Environment Variables)
BINANCE_API_KEY = os.environ.get("7jF0gZgl9CIn6kmuDtcCoMZmwtvxjpc79Geso0GCEMJsoBRGJcR9Rfgfr2IW80as")
BINANCE_SECRET_KEY = os.environ.get("uczEZhc7RpzGp7cIarmxbVyGlVUnrzNaBXsWqVaaoNos3shjDTSaHjGrQRTzHni7")
TELEGRAM_BOT_TOKEN = os.environ.get("8356755161:AAHtX19JNmHJ8FLFWKfWJoG2-0HNVTDoYCM")
TELEGRAM_CHAT_ID = os.environ.get("5430604708")

SYMBOL = "BTCUSDT"

# 📲 ২. টেলিগ্রাম নোটিফিকেশন ফাংশন
def send_telegram_msg(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"❌ Telegram Error: {e}")

# 📊 ৩. বাইন্যান্স থেকে মার্কেট ডাটা ফেচ করা (১৫ মিনিটের ক্যান্ডেল)
def get_market_data():
    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": SYMBOL, "interval": "15m", "limit": 250}
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        
        df = pd.DataFrame(data, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'
        ])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Market Data Fetch Error: {e}")
        return None

# 🧠 ৪. এডভান্সড কনফ্লুয়েন্স ফিল্টার (Multi-Indicator Analysis)
def analyze_market_signals(df):
    # ইন্ডিকেটর হিসাব
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['macd'] = ta.trend.macd_diff(df['close'])
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    df['vol_sma'] = df['volume'].rolling(window=20).mean()
    
    df.dropna(inplace=True)
    
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    current_price = latest['close']
    rsi = latest['rsi']
    macd = latest['macd']
    prev_macd = prev['macd']
    ema200 = latest['ema200']
    volume = latest['volume']
    vol_sma = latest['vol_sma']

    side_action = None

    # 🟢 BUY (LONG) ট্রেডের শক্ত কনফার্মেশন:
    # ১. দাম EMA200 এর উপরে (আপট্রেন্ড)
    # ২. RSI ৪০ এর নিচে বা কাছাকাছি (ডিপ বাই)
    # ৩. MACD বুুলিশ ক্রসওভার (নিচে থেকে ওপরে)
    # ৪. ভলিউম ২০ দিনের গড়ের চেয়ে বেশি
    if (current_price > ema200) and (rsi < 45) and (macd > 0 and prev_macd <= 0) and (volume > vol_sma):
        side_action = "BUY"

    # 🔴 SELL (SHORT) ট্রেডের শক্ত কনফার্মেশন:
    # ১. দাম EMA200 এর নিচে (ডাউনট্রেন্ড)
    # ২. RSI ৫৮ এর ওপরে (ওভারবট)
    # ৩. MACD বেয়ারিশ ক্রসওভার (ওপর থেকে নিচে)
    # ৪. ভলিউম ২০ দিনের গড়ের চেয়ে বেশি
    elif (current_price < ema200) and (rsi > 55) and (macd < 0 and prev_macd >= 0) and (volume > vol_sma):
        side_action = "SELL"

    return side_action, current_price

# 🚀 ৫. মূল এক্সিকিউশন
def main():
    print("🤖 Professional High-Precision AI Bot Running...")
    df = get_market_data()
    if df is None or len(df) == 0:
        return

    side_action, current_price = analyze_market_signals(df)

    if side_action is None:
        print("⏸️ No High-Probability Signal Found. Waiting for perfect market setup.")
        return

    # ১:১.৫ Risk to Reward Calculation
    if side_action == "BUY":
        take_profit = round(current_price * 1.015, 2) # ১.৫% লাভ
        stop_loss = round(current_price * 0.990, 2)   # ১.০% লস
    else: # SELL (SHORT)
        take_profit = round(current_price * 0.985, 2) # শর্টে কম দামে লাভ
        stop_loss = round(current_price * 1.010, 2)   # শর্টে বেশি দামে লস

    # 🏦 বাইন্যান্স টেস্টনেট অর্ডার এক্সিকিউশন
    execution_status = "⚠️ Order Skipped"
    try:
        client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)
        
        # পজিশন সাইজ (0.002 BTC)
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side_action,
            type='MARKET',
            quantity=0.002
        )
        
        exit_side = "SELL" if side_action == "BUY" else "BUY"

        # ১. Take Profit Order
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type='TAKE_PROFIT_MARKET',
            stopPrice=take_profit,
            closePosition=True
        )

        # ২. Stop Loss Order
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type='STOP_MARKET',
            stopPrice=stop_loss,
            closePosition=True
        )

        execution_status = "✅ Trade Placed with Perfect TP & SL"
    except Exception as order_err:
        execution_status = f"❌ Order Error: {str(order_err)[:40]}"

    # 📩 টেলিগ্রাম বার্তা পাঠানো
    msg = f"🚀 *HIGH ACCURACY TRADE SIGNAL*\n\n" \
          f"🔹 *Symbol:* {SYMBOL}\n" \
          f"🔹 *Action:* {side_action}\n" \
          f"🔹 *Entry Price:* ${current_price}\n" \
          f"🎯 *Take Profit:* ${take_profit}\n" \
          f"🛑 *Stop Loss:* ${stop_loss}\n\n" \
          f"Status: {execution_status}"
    
    send_telegram_msg(msg)
    print("Execution Completed Successfully.")

if __name__ == "__main__":
    main()
