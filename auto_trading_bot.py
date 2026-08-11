import yfinance as yf
import pandas as pd
import numpy as np
import ta
import joblib
import time
import requests
import urllib3
from binance.client import Client

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 🔑 ১. টেলিগ্রাম ক্রেডেনশিয়াল
TELEGRAM_TOKEN = "8356755161:AAHtX19JNmHJ8FLFWKfWJoG2-0HNVTDoYCM"
CHAT_ID = "5430604708"

# 🔑 ২. বাইনান্স টেস্টনেট এপিআই (Testnet Keys)
BINANCE_API_KEY = "7jF0gZgl9CIn6kmuDtcCoMZmwtvxjpc79Geso0GCEMJsoBRGJcR9Rfgfr2IW80as"
BINANCE_SECRET_KEY = "uczEZhc7RpzGp7cIarmxbVyGlVUnrzNaBXsWqVaaoNos3shjDTSaHjGrQRTzHni7"

# বাইনান্স টেস্টনেট ক্লায়েন্ট সেটআপ
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"❌ টেলিগ্রাম এরর: {e}")

print("⏳ Multi-Timeframe AI মডেল ও ট্রেডিং ইঞ্জিন লোড হচ্ছে...")
model = joblib.load("btc_multi_model.pkl")
feature_names = joblib.load("multi_features.pkl")
print("✅ মডেল এবং Binance Testnet কানেক্টেড!\n")

def clean_cols(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def get_multi_tf_data():
    df_1h = yf.download(tickers='BTC-USD', period='1mo', interval='1h', progress=False)
    df_1h = clean_cols(df_1h)

    df_4h = df_1h.resample('4h').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    }).dropna()

    df_4h['EMA_200_4h'] = ta.trend.ema_indicator(df_4h['Close'], window=200)
    df_4h['Trend_4h'] = (df_4h['Close'] > df_4h['EMA_200_4h']).astype(int)

    df_1h['EMA_20'] = ta.trend.ema_indicator(df_1h['Close'], window=20)
    df_1h['EMA_50'] = ta.trend.ema_indicator(df_1h['Close'], window=50)
    df_1h['RSI'] = ta.momentum.rsi(df_1h['Close'], window=14)

    macd = ta.trend.MACD(df_1h['Close'])
    df_1h['MACD_Diff'] = macd.macd_diff()

    bollinger = ta.volatility.BollingerBands(df_1h['Close'], window=20, window_dev=2)
    df_1h['BB_High'] = bollinger.bollinger_hband()
    df_1h['BB_Low'] = bollinger.bollinger_lband()
    df_1h['ATR'] = ta.volatility.average_true_range(df_1h['High'], df_1h['Low'], df_1h['Close'], window=14)

    df = pd.merge_asof(df_1h.sort_index(), df_4h[['Trend_4h']].sort_index(), left_index=True, right_index=True)
    return df.iloc[-1]

def execute_binance_trade(side, quantity=0.01):
    try:
        # মার্কেট অর্ডারের মাধ্যমে ফিউচার্স ট্রেড ওপেন
        order = client.futures_create_order(
            symbol='BTCUSDT',
            side=side,
            type='MARKET',
            quantity=quantity
        )
        print(f"⚡ Binance Testnet-এ ট্রেড এক্সিকিউট হয়েছে: {side}")
        return True
    except Exception as e:
        print(f"❌ বাইনান্স ট্রেডিং এরর: {e}")
        return False

send_telegram_message("🤖 *FULL-AUTO AI TRADING BOT ACTIVE!*\nBinance Testnet + Telegram Alerts সক্রিয় করা হয়েছে।")

last_signal = None

while True:
    try:
        latest = get_multi_tf_data()
        input_data = pd.DataFrame([latest[feature_names].values], columns=feature_names)

        pred = model.predict(input_data)[0]
        probs = model.predict_proba(input_data)[0]
        confidence = round(max(probs) * 100, 2)

        price = round(latest['Close'], 2)
        atr = latest['ATR']

        if pred == 1:
            signal = "BUY"
            signal_str = "🟢 LONG (BUY)"
            stop_loss = round(price - (atr * 1.5), 2)
            take_profit = round(price + (atr * 3.0), 2)
        else:
            signal = "SELL"
            signal_str = "🔴 SHORT (SELL)"
            stop_loss = round(price + (atr * 1.5), 2)
            take_profit = round(price - (atr * 3.0), 2)

        if signal != last_signal:
            # ১. বাইনান্স টেস্টনেটে ট্রেড এক্সিকিউট করা
            trade_status = execute_binance_trade(side=signal, quantity=0.01)
            
            status_text = "✅ Order Placed on Binance Testnet" if trade_status else "⚠️ Trade Execution Failed"
# ২. টেলিগ্রামে কনফার্মেশন ও নোটিফিকেশন পাঠানো
            msg = f"🤖 *AUTO-TRADE SIGNAL EXECUTED*\n\n" \
                  f"📊 *Signal:* {signal_str}\n" \
                  f"🎯 *Confidence:* {confidence}%\n" \
                  f"💰 *Entry Price:* ${price}\n" \
                  f"🛡️ *Stop Loss:* ${stop_loss}\n" \
                  f"🚀 *Take Profit:* ${take_profit}\n" \
                  f"🏦 *Binance Execution:* {status_text}"

            send_telegram_message(msg)
            last_signal = signal

    except Exception as e:
        print(f"❌ এরর: {e}")

    time.sleep(60)