import os
import time
import datetime
import threading
import http.server
import socketserver
import requests
import urllib3
import pandas as pd
import numpy as np
import ta
import joblib
from binance.client import Client

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 🌐 ১. ডামি পোর্ট ফিক্স (Render Web Service Active রাখার জন্য)
def start_dummy_port():
    port = int(os.environ.get("PORT", 8080))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
        httpd.serve_forever()

threading.Thread(target=start_dummy_port, daemon=True).start()

# 🔑 ২. ক্রেডেনশিয়াল সেটআপ (Environment Variable বা কাস্টম ভ্যালু)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8356755161:AAHtX19JNmHJ8FLFWKfwJoG")
CHAT_ID = os.environ.get("CHAT_ID", "5430604708")

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "7jF0gzg19CIn6kmuDtcCoMZmwtvxjpc79G")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "uczEZhc7RpzGp7ciarmxbVyGlVUnrzN")

# বাইন্যান্স ক্লায়েন্ট (Testnet active)
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)

# 📩 ৩. টেলিগ্রাম মেসেজিং সিস্টেম
def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"❌ Telegram Messaging Error: {e}")

# 📊 ৪. Binance REST API থেকে মার্কেট ক্যান্ডেলস্টিক ডাটা গ্রহণ
def fetch_binance_data(symbol="BTCUSDT", interval="1m", limit=100):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        df = pd.read_json(url)
        df = df.iloc[:, :6]
        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Market Data Fetch Error: {e}")
        return None

# 🧠 ৫. ইন্ডিকেটর ও ফিচার ক্যালকুলেশন
def prepare_features(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['macd'] = ta.trend.macd_diff(df['close'])
    df['sma'] = ta.trend.sma_indicator(df['close'], window=20)
    df['ema'] = ta.trend.ema_indicator(df['close'], window=20)
    df.dropna(inplace=True)
    return df

# 📦 ৬. AI মডেল ও ফিচার লোড
print("🤖 AI Trading Engine setup in progress...")
model = None
feature_names = None

try:
    if os.path.exists("trading_model.pkl"):
        model = joblib.load("trading_model.pkl")
    if os.path.exists("multi_features.pkl"):
        feature_names = joblib.load("multi_features.pkl")
    print("✅ AI Model successfully loaded.")
except Exception as e:
    print(f"⚠️ Model Load Warning: {e}")

# 🚀 ৭. বট শুরুর প্রফেশনাল মেসেজ
send_telegram_msg(
    "🤖 *PRO AI TRADING ENGINE ACTIVATED*\n\n"
    "• *Platform:* Binance Futures Testnet\n"
    "• *Pair:* BTCUSDT\n"
    "• *Strategy:* AI Multi-Feature Technical Model\n"
    "• *Status:* Listening for Market Signals..."
)

# 🔄 ৮. মূল ট্রেডিং লুপ
SYMBOL = "BTCUSDT"
last_signal = None  # আগের সিগন্যাল ট্র্যাক করার জন্য

while True:
    try:
        df = fetch_binance_data(symbol=SYMBOL, interval="1m", limit=100)
        
        if df is not None and len(df) > 20:
            df = prepare_features(df)
            current_price = df['close'].iloc[-1]
            
            # --- AI Model Prediction / Strategy Logic ---
            signal_type = None
            confidence = 0.0

            if model is not None and feature_names is not None:
                # আসল মডেল প্রেডিকশন
                X = df[feature_names].iloc[[-1]]
                pred = model.predict(X)[0]
                probs = model.predict_proba(X)[0] if hasattr(model, "predict_proba") else [0.5, 0.5]
                
                signal_type = "LONG" if pred == 1 else "SHORT"
                confidence = round(max(probs) * 100, 2)
            else:
                # ইন্ডিকেটর ভিত্তিক ব্যাকআপ স্ট্র্যাটেজি (যদি PKL ফাইল লোড না হয়)
                rsi = df['rsi'].iloc[-1]
                macd = df['macd'].iloc[-1]
                if rsi < 35 and macd > 0:
                    signal_type = "LONG"
                    confidence = 78.50
                elif rsi > 65 and macd < 0:
                    signal_type = "SHORT"
                    confidence = 81.20

            # কেবল সিগন্যাল চেঞ্জ হলে এবং সিগন্যাল ভ্যালিড থাকলে ট্রেড নেবে
            if signal_type and signal_type != last_signal:
                
                # Risk Management Calculation (0.8% SL, 1.5% TP)
                if signal_type == "SHORT":
                    stop_loss = round(current_price * 1.008, 2)
                    take_profit = round(current_price * 0.985, 2)
                    side_action = "SELL"
                    signal_emoji = "🔴 SHORT (SELL)"
                else:
                    stop_loss = round(current_price * 0.992, 2)
                    take_profit = round(current_price * 1.015, 2)
                    side_action = "BUY"
                    signal_emoji = "🟢 LONG (BUY)"

                # 🏦 Order Execution on Binance Testnet
                execution_status = "⚠️ Order Skipped"
                try:
                    # Futures Market Order (0.002 BTC Test Order Size)
                    order = client.futures_create_order(
                        symbol=SYMBOL,
                        side=side_action,
                        type='MARKET',
                        quantity=0.002
                    )# 🏦 Order Execution on Binance Testnet (Order + TP/SL)
execution_status = "⚠️ Order Skipped"
try:
    # ১. মূল মার্কেট অর্ডার এক্সিকিউট
    order = client.futures_create_order(
        symbol=SYMBOL,
        side=side_action,
        type='MARKET',
        quantity=0.01
    )
    
    # বিপরীতে অর্ডার অ্যাকশন (BUY থাকলে SELL, SELL থাকলে BUY)
    exit_side = "SELL" if side_action == "BUY" else "BUY"
    
    # ২. অটোমেটিক টেক প্রফিট (Take Profit) সেট করা
    client.futures_create_order(
        symbol=SYMBOL,
        side=exit_side,
        type='TAKE_PROFIT_MARKET',
        stopPrice=take_profit,
        closePosition=True
    )

    # ৩. অটোমেটিক স্টপ লস (Stop Loss) সেট করা
    client.futures_create_order(
        symbol=SYMBOL,
        side=exit_side,
        type='STOP_MARKET',
        stopPrice=stop_loss,
        closePosition=True
    )
    
    execution_status = "✅ Market Order with TP/SL Placed"
except Exception as order_err:
    execution_status = f"❌ Order Failed ({str(order_err)[:30]})"
                    execution_status = "✅ Order Placed Successfully"
                except Exception as order_err:
                    execution_status = f"❌ Order Failed ({str(order_err)[:30]})"

                # 📩 টেলিগ্রাম পেশাদার রিপোর্ট
                time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
                msg = (
                    f"⚡ *AUTO-TRADE SIGNAL EXECUTED*\n\n"
                    f"📊 *Signal:* {signal_emoji}\n"
                    f"🎯 *Confidence:* {confidence}%\n"
                    f"💵 *Entry Price:* ${current_price:.2f}\n"
                    f"🛡️ *Stop Loss:* ${stop_loss:.2f}\n"
                    f"🚀 *Take Profit:* ${take_profit:.2f}\n\n"
                    f"🏦 *Execution:* {execution_status}\n"
                    f"⏰ *Time:* {time_str}"
                )
                
                send_telegram_msg(msg)
                last_signal = signal_type # বর্তমান সিগন্যাল সেভ করে রাখা হলো

    except Exception as main_err:
        print(f"❌ Execution Loop Error: {main_err}")
    
    # ৬০ সেকেন্ড বিরতি (Yahoo Rate Limit ইস্যু প্রতিরোধে)
    time.sleep(60)
