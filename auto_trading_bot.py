import os, time, threading, requests, pandas as pd, ta
from flask import Flask
from binance.client import Client
from binance.exceptions import BinanceAPIException

app = Flask(__name__)

@app.route('/')
def home():
    return "6-Factor AI Trading Bot is Running Correctly!", 200

# CONFIGURATION
SYMBOL = "BTCUSDT"
TRADE_QUANTITY = 0.002
MIN_SCORE = 75.0  # 🎯 ৭৫% স্কোর না পাওয়া পর্যন্ত ট্রেড নেবে না!

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

try:
    client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)
    client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'
except Exception as e:
    print(f"❌ Client Error: {e}")

def send_telegram_msg(message):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"❌ Telegram Error: {e}")

def check_active_position():
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        for pos in positions:
            if float(pos['positionAmt']) != 0:
                return True
        return False
    except Exception as e:
        print(f"❌ Position Check Error: {e}")
        return True

def get_data():
    try:
        klines = client.futures_klines(symbol=SYMBOL, interval='5m', limit=100)
        df = pd.DataFrame(klines, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'ct', 'qav', 't', 'tbav', 'tbqv', 'i'])
        for col in ['open', 'high', 'low', 'close', 'vol']: 
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Data Fetch Error: {e}")
        return None

# 🧠 ৬টি প্রফেশনাল শর্ত যাচাইকরণ
def calculate_score(df):
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd_diff()
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    bb = ta.volatility.BollingerBands(df['close'])
    df['bb_high'] = bb.bollinger_hband()
    df['bb_low'] = bb.bollinger_lband()
    
    df.dropna(inplace=True)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    bullish_score = 0
    bearish_score = 0
    
    # ১. Trend EMA50 (১৫ পয়েন্ট)
    if latest['close'] > latest['ema50']: bullish_score += 15
    else: bearish_score += 15

    # ২. Trend EMA200 (১৫ পয়েন্ট)
    if latest['close'] > latest['ema200']: bullish_score += 15
    else: bearish_score += 15

    # ৩. Momentum RSI (২০ পয়েন্ট)
    if latest['rsi'] < 42: bullish_score += 20
    elif latest['rsi'] > 58: bearish_score += 20

    # ৪. MACD Signal (২০ পয়েন্ট)
    if latest['macd'] > 0 and prev['macd'] <= 0: bullish_score += 20
    elif latest['macd'] < 0 and prev['macd'] >= 0: bearish_score += 20

    # ৫. Bollinger Band (১৫ পয়েন্ট)
    if latest['close'] <= latest['bb_low']: bullish_score += 15
    elif latest['close'] >= latest['bb_high']: bearish_score += 15

    # ৬. Volume Support (১৫ পয়েন্ট)
    vol_mean = df['vol'].rolling(20).mean().iloc[-1]
    if latest['vol'] > vol_mean:
        bullish_score += 15
        bearish_score += 15

    side_action = None
    final_score = 0

    if bullish_score >= MIN_SCORE:
        side_action = "BUY"
        final_score = bullish_score
    elif bearish_score >= MIN_SCORE:
        side_action = "SELL"
        final_score = bearish_score

    print(f"📊 Market Price: ${latest['close']} | Bullish: {bullish_score}% | Bearish: {bearish_score}%")
    return side_action, final_score, latest['close']

def execute_trade(side, price, score):
    if side == 'BUY':
        tp = round(price * 1.015, 1) # ১.৫% টেক প্রফিট
        sl = round(price * 0.992, 1) # ০.৮% স্টপ লস
        exit_side = 'SELL'
    else:
        tp = round(price * 0.985, 1)
        sl = round(price * 1.008, 1)
        exit_side = 'BUY'

    try:
        print(f"⚡ Executing High-Confidence {side} Order on Binance...")
        
        # ১. মেইন অর্ডার
        client.futures_create_order(symbol=SYMBOL, side=side, type='MARKET', quantity=TRADE_QUANTITY)
        
        # ২. টেক প্রফিট
        client.futures_create_order(symbol=SYMBOL, side=exit_side, type='TAKE_PROFIT_MARKET', stopPrice=tp, closePosition=True)
        
        # ৩. স্টপ লস
        client.futures_create_order(symbol=SYMBOL, side=exit_side, type='STOP_MARKET', stopPrice=sl, closePosition=True)

        msg = f"🚀 *HIGH CONFIDENCE TRADE EXECUTED*\n\n" \
              f"🔹 *Signal:* {side}\n" \
              f"🎯 *Score:* {score}/100\n" \
              f"💰 *Entry Price:* ${price}\n" \
              f"🚀 *Take Profit:* ${tp}\n" \
              f"🛑 *Stop Loss:* ${sl}\n\n" \
              f"✅ *Binance Status:* Order & TP/SL Placed!"
        send_telegram_msg(msg)
        print("✅ Trade and TP/SL Orders Placed Successfully!")

    except BinanceAPIException as e:
        error_msg = f"❌ *Binance Execution Error:* `{e.message}`"
        print(error_msg)
        send_telegram_msg(error_msg)
    except Exception as e:
        print(f"❌ Order Error: {e}")

def trading_loop():
    print("🤖 Fixed 6-Factor AI Trading Bot Running...")
    while True:
        try:
            if check_active_position():
                print("⏳ Trade active on Binance. Waiting for TP/SL to hit...")
            else:
                df = get_data()
                if df is not None:
                    side_action, score, price = calculate_score(df)
                    
                    # 🔴 মূল ফিক্স: কেবল স্কোর ৭৫ বা তার বেশি হলে এবং সাইড থাকলে ট্রেড নেবে!
                    if side_action and score >= MIN_SCORE:
                        execute_trade(side_action, price, score)
                        time.sleep(300) # ট্রেড নেওয়ার পর ৫ মিনিট পজ
                    else:
                        print(f"⏸️ Waiting for strong signal (Score below {MIN_SCORE}%).")
        except Exception as e:
            print(f"❌ Loop Error: {e}")
        
        time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=trading_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
