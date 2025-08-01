import os
import time
import requests
from flask import Flask, request
from pybit.unified_trading import HTTP
from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET

app = Flask(__name__)
port = int(os.environ.get("PORT", 5000))

session = HTTP(
    api_key=API_KEY,
    api_secret=API_SECRET,
    testnet=TESTNET
)

processing = False
last_alert_time = 0
ALERT_COOLDOWN = 3  # sekundy

def send_to_discord(message):
    try:
        payload = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"❌ Błąd wysyłania do Discord: {e}")

def get_current_position(symbol):
    try:
        result = session.get_positions(category="linear", symbol=symbol)
        position = result["result"]["list"][0]
        size = float(position["size"])
        side = position["side"]
        return size, side
    except Exception as e:
        send_to_discord(f"❗ Błąd pobierania pozycji: {e}")
        return 0.0, "None"

def calculate_qty(symbol):
    try:
        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        balance_info = balance_data["result"]["list"][0]["coin"]
        usdt = next(c for c in balance_info if c["coin"] == "USDT")
        available_usdt = float(usdt.get("walletBalance", 0))
        trade_usdt = available_usdt * 1

        tickers_data = session.get_tickers(category="linear")
        price_info = next((item for item in tickers_data["result"]["list"] if item["symbol"] == symbol), None)
        if not price_info:
            send_to_discord(f"❗ Symbol {symbol} nie znaleziony.")
            return None

        last_price = float(price_info["lastPrice"])
        qty = int(trade_usdt / last_price)
        return qty
    except Exception as e:
        send_to_discord(f"❗ Błąd podczas obliczania ilości: {e}")
        return None

@app.route("/", methods=["GET"])
def index():
    return "✅ Bot działa!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    global processing, last_alert_time

    current_time = time.time()
    if current_time - last_alert_time < ALERT_COOLDOWN:
        send_to_discord("⏳ Alert zignorowany — zbyt krótki odstęp czasu.")
        return "Too soon", 429

    if processing:
        send_to_discord("⏳ Poprzedni alert nadal przetwarzany. Pomijam ten.")
        return "Processing in progress", 429

    processing = True
    last_alert_time = current_time

    try:
        data = request.get_json()
        action = data.get("action", "").lower()
        send_to_discord(f"🔔 Odebrano alert: {data}")

        valid_actions = ["buy", "sell", "close_buy", "close_sell"]
        if action not in valid_actions:
            send_to_discord(f"⚠️ Nieprawidłowe polecenie: {data}")
            processing = False
            return "Invalid action", 400

        position_size, position_side = get_current_position(SYMBOL)

        if action == "close_buy" and position_size > 0 and position_side == "Buy":
            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side="Sell",
                orderType="Market",
                qty=position_size,
                reduceOnly=True,
                timeInForce="GoodTillCancel"
            )
            send_to_discord("🔒 Zamknięto pozycję LONG")
            processing = False
            return "Closed long", 200

        if action == "close_sell" and position_size > 0 and position_side == "Sell":
            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side="Buy",
                orderType="Market",
                qty=position_size,
                reduceOnly=True,
                timeInForce="GoodTillCancel"
            )
            send_to_discord("🔒 Zamknięto pozycję SHORT")
            processing = False
            return "Closed short", 200

        if action == "buy":
            if position_size > 0:
                send_to_discord("⚠️ Pozycja już otwarta — LONG lub SHORT. Ignoruję sygnał BUY.")
                processing = False
                return "Position already open", 200

            qty = calculate_qty(SYMBOL)
            if qty:
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side="Buy",
                    orderType="Market",
                    qty=qty,
                    timeInForce="GoodTillCancel"
                )
                send_to_discord(f"📥 Otwarta pozycja LONG ({qty} {SYMBOL})")

        if action == "sell":
            if position_size > 0:
                send_to_discord("⚠️ Pozycja już otwarta — LONG lub SHORT. Ignoruję sygnał SELL.")
                processing = False
                return "Position already open", 200

            qty = calculate_qty(SYMBOL)
            if qty:
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side="Sell",
                    orderType="Market",
                    qty=qty,
                    timeInForce="GoodTillCancel"
                )
                send_to_discord(f"📥 Otwarta pozycja SHORT ({qty} {SYMBOL})")

        processing = False
        return "OK", 200

    except Exception as e:
        send_to_discord(f"❗ Błąd systemowy: {e}")
        processing = False
        return "Webhook error", 500

if __name__ == "__main__":
    print("🚀 Bot uruchomiony...")
    app.run(host="0.0.0.0", port=port)
