import os
import requests
import pandas as pd
import numpy as np
import joblib
import ta
from binance.client import Client

# 🔑 ১. পরিবেশ পরিবর্তনশীল (Environment Variables) থেকে API Key নেওয়া
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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

# 📊 ৩. বাইন্যান্স থেকে মার্কেট ডাটা আনা
def get_market_data():
    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": SYMBOL, "interval": "15m", "limit": 100}
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

# 🧠 ৪. ইন্ডিকেটর ও ফিচার ক্যালকুলেশন
def prepare_features(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['macd'] = ta.trend.macd_diff(df['close'])
    df['sma'] = ta.trend.sma_indicator(df['close'], window=20)
    df['ema'] = ta.trend.ema_indicator(df['close'], window=20)
    df.dropna(inplace=True)
    return df

# 🚀 মূল প্রসেস শুরু
def main():
    print("🤖 AI Trading Bot Started...")
    df = get_market_data()
    if df is None or len(df) == 0:
        return

    df = prepare_features(df)
    latest = df.iloc[-1]
    current_price = latest['close']

    # AI Model লোড করা
    model_path = "trading_model.pkl"
    if not os.path.exists(model_path):
        model_path = "btc_multi_model.pkl"

    if os.path.exists(model_path):
        model = joblib.load(model_path)
        # প্রেডিকশন
        features = [[latest['close'], latest['rsi'], latest['macd'], latest['sma'], latest['ema']]]
        try:
            pred = model.predict(features)[0]
        except:
            pred = 1 if latest['rsi'] < 40 else (0 if latest['rsi'] > 60 else -1)
    else:
        pred = 1 if latest['rsi'] < 40 else (0 if latest['rsi'] > 60 else -1)

    # সিগন্যাল নির্ধারণ
    if pred == 1:
        side_action = "BUY"
        take_profit = round(current_price * 1.015, 2)
        stop_loss = round(current_price * 0.992, 2)
    elif pred == 0:
        side_action = "SELL"
        take_profit = round(current_price * 0.985, 2) # SHORT এর জন্য কম দাম
        stop_loss = round(current_price * 1.008, 2)   # SHORT এর জন্য বেশি দাম
    else:
        print("⏸️ No Trade Signal.")
        return

    # 🏦 বাইন্যান্স টেস্টনেটে ট্রেড এক্সিকিউশন
    execution_status = "⚠️ Order Skipped"
    try:
        client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)
        
        # মূল পজিশন নেওয়া
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side_action,
            type='MARKET',
            quantity=0.002
        )
        
        exit_side = "SELL" if side_action == "BUY" else "BUY"
        
        # LONG এবং SHORT-এর জন্য TP/SL নির্ধারণ
        if side_action == "BUY":
            tp_price = take_profit
            sl_price = stop_loss
        else:
            tp_price = take_profit  # SHORT-এর জন্য TP দাম কম
            sl_price = stop_loss    # SHORT-এর জন্য SL দাম বেশি
# ১. টেক প্রফিট অর্ডার
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type='TAKE_PROFIT_MARKET',
            stopPrice=tp_price,
            closePosition=True
        )

        # ২. স্টপ লস অর্ডার
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type='STOP_MARKET',
            stopPrice=sl_price,
            closePosition=True
        )

        execution_status = "✅ Order Placed with Correct TP & SL"
    except Exception as order_err:
        execution_status = f"❌ Order Failed ({str(order_err)[:40]})"

    # 📩 টেলিগ্রাম মেসেজ পাঠানো
    msg = f"🤖 *AI Signal Update*\n\n" \
          f"🔹 *Symbol:* {SYMBOL}\n" \
          f"🔹 *Action:* {side_action}\n" \
          f"🔹 *Price:* ${current_price}\n" \
          f"🎯 *TP:* ${take_profit}\n" \
          f"🛑 *SL:* ${stop_loss}\n\n" \
          f"Status: {execution_status}"
    
    send_telegram_msg(msg)
    print("Done!")

if name == "main":
    main()
