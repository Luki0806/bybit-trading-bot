import os
import time
import requests
from flask import Flask, request
from pybit.unified_trading import HTTP
from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET

app = Flask(__name__)
port = int(os.environ.get("PORT", 5000))

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

processing = False
last_alert_time = 0
ALERT_COOLDOWN = 2  # sekundy


def send_to_discord(message):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
    except Exception as e:
        print(f"❌ Błąd wysyłania do Discord: {e}")


def get_current_position(symbol):
    try:
        result = session.get_positions(category="linear", symbol=symbol)
        pos = result["result"]["list"][0]
        return float(pos["size"]), pos["side"]
    except Exception as e:
        send_to_discord(f"❗ Błąd pobierania pozycji: {e}")
        return 0.0, "None"


def calculate_qty(symbol):
    try:
        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        usdt = next(c for c in balance_data["result"]["list"][0]["coin"] if c["coin"] == "USDT")
        available = float(usdt.get("walletBalance", 0))
        price_info = next(i for i in session.get_tickers(category="linear")["result"]["list"] if i["symbol"] == symbol)
        qty = int((available * 1) / float(price_info["lastPrice"]))
        return qty
    except Exception as e:
        send_to_discord(f"❗ Błąd obliczania wielkości pozycji: {e}")
        return None


@app.route("/", methods=["GET"])
def index():
    return "✅ Bot działa!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    global processing, last_alert_time

    if time.time() - last_alert_time < ALERT_COOLDOWN:
        return "Cooldown", 429

    if processing:
        return "Still processing", 429

    processing = True
    last_alert_time = time.time()

    try:
        data = request.get_json()
        action = data.get("action", "").lower()
        size, side = get_current_position(SYMBOL)

        if action == "buy" and size == 0:
            qty = calculate_qty(SYMBOL)
            session.place_order(category="linear", symbol=SYMBOL, side="Buy", orderType="Market", qty=qty)
            send_to_discord(f"📈 Otwarto pozycję BUY ({qty})")

        elif action == "sell" and size == 0:
            qty = calculate_qty(SYMBOL)
            session.place_order(category="linear", symbol=SYMBOL, side="Sell", orderType="Market", qty=qty)
            send_to_discord(f"📉 Otwarto pozycję SELL ({qty})")

        elif action == "close_buy" and size > 0 and side == "Buy":
            session.place_order(category="linear", symbol=SYMBOL, side="Sell", orderType="Market",
                                qty=size, reduceOnly=True)
            send_to_discord(f"🔒 Zamknięto pozycję BUY ({size})")

        elif action == "close_sell" and size > 0 and side == "Sell":
            session.place_order(category="linear", symbol=SYMBOL, side="Buy", orderType="Market",
                                qty=size, reduceOnly=True)
            send_to_discord(f"🔒 Zamknięto pozycję SELL ({size})")

        else:
            send_to_discord(f"⚠️ Ignoruję alert '{action.upper()}' — nieodpowiedni kontekst (pozycja: {side}, size: {size})")

        processing = False
        return "OK", 200

    except Exception as e:
        send_to_discord(f"❗ Błąd w webhook: {e}")
        processing = False
        return "Error", 500


if __name__ == "__main__":
    print("🚀 Bot uruchomiony...")
    app.run(host="0.0.0.0", port=port)
