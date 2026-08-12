import os, time, threading, requests, pandas as pd, ta
from flask import Flask
from binance.client import Client
from binance.exceptions import BinanceAPIException

app = Flask(__name__)

@app.route('/')
def home():
    return "Strict 100% 6-Condition AI Bot is Running!", 200

SYMBOL = "BTCUSDT"
TRADE_QUANTITY = 0.002

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

try:
    client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=True)
    client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'
except Exception as e:
    print(f"❌ API Setup Error: {e}")

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
    except:
        return True

def get_data():
    try:
        klines = client.futures_klines(symbol=SYMBOL, interval='5m', limit=100)
        df = pd.DataFrame(klines, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'ct', 'qav', 't', 'tbav', 'tbqv', 'i'])
        for col in ['open', 'high', 'low', 'close', 'vol']: 
            df[col] = df[col].astype(float)
        return df
    except:
        return None

# 🧠 ৬টির মধ্যে ৬টি শর্তই মিলতে হবে
def analyze_strict_6_conditions(df):
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
    vol_mean = df['vol'].rolling(20).mean().iloc[-1]
    
    # BUY-এর জন্য ৬টি শর্ত
    c1_buy = latest['close'] > latest['ema50']
    c2_buy = latest['close'] > latest['ema200']
    c3_buy = latest['rsi'] < 40
    c4_buy = latest['macd'] > 0 and prev['macd'] <= 0
    c5_buy = latest['close'] <= latest['bb_low']
    c6_buy = latest['vol'] > vol_mean

    # SELL-এর জন্য ৬টি শর্ত
    c1_sell = latest['close'] < latest['ema50']
    c2_sell = latest['close'] < latest['ema200']
    c3_sell = latest['rsi'] > 60
    c4_sell = latest['macd'] < 0 and prev['macd'] >= 0
    c5_sell = latest['close'] >= latest['bb_high']
    c6_sell = latest['vol'] > vol_mean

    side = None
    if c1_buy and c2_buy and c3_buy and c4_buy and c5_buy and c6_buy:
        side = "BUY"
    elif c1_sell and c2_sell and c3_sell and c4_sell and c5_sell and c6_sell:
        side = "SELL"

    return side, latest['close']

def execute_trade(side, price):
    if side == 'BUY':
        tp = round(price * 1.015, 1)
        sl = round(price * 0.992, 1)
        exit_side = 'SELL'
    else:
        tp = round(price * 0.985, 1)
        sl = round(price * 1.008, 1)
        exit_side = 'BUY'

    try:
        client.futures_create_order(symbol=SYMBOL, side=side, type='MARKET', quantity=TRADE_QUANTITY)
        client.futures_create_order(symbol=SYMBOL, side=exit_side, type='TAKE_PROFIT_MARKET', stopPrice=tp, closePosition=True)
        client.futures_create_order(symbol=SYMBOL, side=exit_side, type='STOP_MARKET', stopPrice=sl, closePosition=True)

        msg = f"🔥 *100% STRICT SIGNAL EXECUTED*\n\n" \
              f"🔹 *Action:* {side}\n" \
              f"💰 *Price:* ${price}\n" \
              f"🎯 *TP:* ${tp}\n" \
              f"🛑 *SL:* ${sl}\n\n" \
              f"✅ *All 6 Indicators Confirmed!*"
        send_telegram_msg(msg)
    except Exception as e:
        print(f"❌ Execution Error: {e}")

def trading_loop():
    print("🤖 Strict 6-Condition Trading Engine Active...")
    while True:
        try:
            if not check_active_position():
                df = get_data()
                if df is not None:
                    side, price = analyze_strict_6_conditions(df)
                    if side:
                        execute_trade(side, price)
                        time.sleep(300)
        except Exception as e:
            print(f"❌ Loop Error: {e}")
        time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=trading_loop)
    t.daemon = True
    t.start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
