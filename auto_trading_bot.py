import os
import time
import threading
import requests
import pandas as pd
import ta
from flask import Flask
from binance.client import Client
from binance.exceptions import BinanceAPIException

# 🌐 ১. Render-কে ২৪ ঘণ্টা সচল রাখার জন্য Flask সার্ভার
app = Flask(__name__)

@app.route('/')
def home():
    return "Professional AI Trading Bot is LIVE 24/7!", 200

# 🔑 ২. এনভায়রনমেন্ট ভেরিয়েবল (API Keys)
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "BTCUSDT"
TRADE_QUANTITY = 0.002  # বিটকয়েনের লট সাইজ (আপনার ব্যালেন্স অনুযায়ী পরিবর্তন করতে পারেন)

# 🚀 ৩. বাইন্যান্স ক্লায়েন্ট সেটআপ (Testnet)
try:
    client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)
    client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'
except Exception as e:
    print(f"❌ Client Setup Error: {e}")

# 📲 ৪. টেলিগ্রাম নোটিফিকেশন সিস্টেম
def send_telegram_msg(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"❌ Telegram Error: {e}")

# 📊 ৫. মার্কেট ডাটা ফেচিং
def get_market_data():
    try:
        # সরাসরি বাইন্যান্স ফিউচার্স থেকে ডাটা নেওয়া
        klines = client.futures_klines(symbol=SYMBOL, interval='15m', limit=250)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'
        ])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Market Data Error: {e}")
        return None

# 🧠 ৬. প্রফেশনাল ট্রেডিং স্ট্র্যাটেজি (Indicators Setup)
def analyze_market_signals(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['macd'] = ta.trend.macd_diff(df['close'])
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    df['vol_sma'] = df['volume'].rolling(window=20).mean()
    
    df.dropna(inplace=True)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    current_price = latest['close']
    side_action = None

    # Buy / Long Condition
    if (current_price > latest['ema200']) and (latest['rsi'] < 45) and (latest['macd'] > 0 and prev['macd'] <= 0):
        side_action = "BUY"
    # Sell / Short Condition
    elif (current_price < latest['ema200']) and (latest['rsi'] > 55) and (latest['macd'] < 0 and prev['macd'] >= 0):
        side_action = "SELL"

    return side_action, current_price

# ⚙️ ৭. বাইন্যান্স এক্সিকিউশন ও TP/SL প্লেসমেন্ট
def execute_trade(side_action, current_price):
    try:
        # 🎯 TP এবং SL হিসাব (Risk to Reward Ratio 1:1.5)
        if side_action == "BUY":
            take_profit = round(current_price * 1.015, 2)
            stop_loss = round(current_price * 0.990, 2)
            exit_side = "SELL"
        else:
            take_profit = round(current_price * 0.985, 2)
            stop_loss = round(current_price * 1.010, 2)
            exit_side = "BUY"

        print(f"⏳ Executing {side_action} Order...")

        # ১. মেইন পজিশন ওপেন করা (Market Order)
        client.futures_create_order(
            symbol=SYMBOL,
            side=side_action,
            type='MARKET',
            quantity=TRADE_QUANTITY
        )

        # ২. টেক প্রফিট (TP) সেট করা
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type='TAKE_PROFIT_MARKET',
            stopPrice=take_profit,
            closePosition=True
        )

        # ৩. স্টপ লস (SL) সেট করা
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type='STOP_MARKET',
            stopPrice=stop_loss,
            closePosition=True
        )

        # ৪. সফলভাবে ট্রেড প্লেস হলে টেলিগ্রামে মেসেজ
        msg = f"🚀 *BINANCE ORDER EXECUTED*\n\n" \
              f"🔹 *Symbol:* {SYMBOL}\n" \
              f"🔹 *Type:* {side_action}\n" \
              f"🔹 *Entry Price:* ${current_price}\n" \
              f"🎯 *Take Profit:* ${take_profit}\n" \
              f"🛑 *Stop Loss:* ${stop_loss}\n\n" \
              f"✅ *Status:* Position & TP/SL Set Automatically!"
        send_telegram_msg(msg)
        print("✅ Trade and TP/SL Executed Successfully!")

    except BinanceAPIException as e:
        error_msg = f"❌ Binance API Error: {e}"
        print(error_msg)
        send_telegram_msg(error_msg)
    except Exception as e:
        print(f"❌ General Error during execution: {e}")

# 🔄 ৮. ব্যাকগ্রাউন্ড রানিং লুপ
def trading_loop():
    print("🤖 Professional Auto-Trading Engine Active...")
    while True:
        try:
            print("\n🔄 Checking Market Conditions...")
            df = get_market_data()
            if df is not None and len(df) > 0:
                side_action, current_price = analyze_market_signals(df)
                
                if side_action:
                    execute_trade(side_action, current_price)
                else:
                    print(f"⏸️ No trade setup at the moment. Current Price: ${current_price}")
        except Exception as e:
            print(f"❌ Main Loop Error: {e}")
        
        # পরবর্তী সিগন্যাল চেকের জন্য ১৫ মিনিট অপেক্ষা
        time.sleep(900)

# 🏁 ৯. অ্যাপ্লিকেশন স্টার্টআপ
if __name__ == "__main__":
    t = threading.Thread(target=trading_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
