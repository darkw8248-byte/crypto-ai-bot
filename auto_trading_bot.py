import os, time, threading, requests, pandas as pd, ta
from flask import Flask
from binance.client import Client
from binance.exceptions import BinanceAPIException

app = Flask(__name__)

@app.route('/')
def home():
    return "6-Factor Trading Engine with Telegram Alerts is LIVE!", 200

# CONFIGURATION
SYMBOL = "BTCUSDT"
TRADE_QUANTITY = 0.002
MIN_SCORE = 75  # প্রফেশনাল থ্রেশহোল্ড ৭৫%

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)
client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

# 📩 টেলিগ্রাম মেসেজ পাঠানোর ফাংশন
def send_telegram_msg(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"❌ Telegram Error: {e}")

def get_data():
    try:
        klines = client.futures_klines(symbol=SYMBOL, interval='5m', limit=100)
        df = pd.DataFrame(klines, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'ct', 'qav', 't', 'tbav', 'tbqv', 'i'])
        for col in ['open', 'high', 'low', 'close', 'vol']: df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Market Data Error: {e}")
        return None

def calculate_score(df):
    # indicators
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd_diff()
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    bb = ta.volatility.BollingerBands(df['close'])
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
    
    latest = df.iloc[-1]
    score = 0
    
    # ৬টি প্রফেশনাল শর্ত
    if latest['close'] > latest['ema50']: score += 15 # ট্রেন্ড
    if latest['close'] > latest['ema200']: score += 15 # ট্রেন্ড
    if latest['rsi'] < 45: score += 20 # RSI
    if latest['macd'] > 0: score += 20 # MACD
    if latest['close'] < latest['bb_low']: score += 15 # Bollinger
    if latest['vol'] > df['vol'].rolling(20).mean().iloc[-1]: score += 15 # Volume
    
    return score, latest['close'], latest['atr']

def execute_trade(side, price, atr, score):
    sl = round((price * 0.985) if side == 'BUY' else (price * 1.015), 1)
    tp = round((price * 1.020) if side == 'BUY' else (price * 0.980), 1)
    
    try:
        # ১. পজিশন ওপেন
        client.futures_create_order(symbol=SYMBOL, side=side, type='MARKET', quantity=TRADE_QUANTITY)
        
        # ২. টেলিগ্রামে অ্যালার্ট নোটিফিকেশন পাঠানো
        msg = f"🚀 *HIGH CONFIDENCE SIGNAL DETECTED*\n\n" \
              f"🔹 *Symbol:* {SYMBOL}\n" \
              f"🔹 *Action:* {side}\n" \
              f"📊 *Confidence Score:* {score}/100\n" \
              f"💰 *Entry Price:* ${price}\n" \
              f"🎯 *Take Profit:* ${tp}\n" \
              f"🛑 *Stop Loss:* ${sl}\n\n" \
              f"✅ *Binance Order:* Placed Successfully!"
        
        send_telegram_msg(msg)
        print(f"✅ Trade Executed & Telegram Alert Sent: {side} at {price}")
    except BinanceAPIException as e:
        error_msg = f"❌ *Binance Order Failed! Order Error:* `{e.message}`"
        send_telegram_msg(error_msg)
        print(error_msg)
    except Exception as e:
        print(f"❌ Order Error: {e}")

def trading_loop():
    print("🤖 6-Factor Signal & Trading Bot Active...")
    while True:
        try:
            df = get_data()
            if df is not None:
                score, price, atr = calculate_score(df)
                print(f"📊 Current Score: {score}/100 | Price: ${price}")
                if score >= MIN_SCORE:
                    execute_trade('BUY', price, atr, score)
                    time.sleep(300) # ট্রেড নেওয়ার পর ৫ মিনিট বিরতি
        except Exception as e:
            print(f"❌ Loop Error: {e}")
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
