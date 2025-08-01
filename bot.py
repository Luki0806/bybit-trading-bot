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
position_opened_by_alert = False  # nowa zmienna śledząca otwarcie

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
        send_to_discord("📊 Obliczam wielkość nowej pozycji...")
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
        send_to_discord(f"✅ Ilość do zlecenia: {qty} {symbol} przy cenie {last_price} USDT")
        return qty
    except Exception as e:
        send_to_discord(f"❗ Błąd podczas obliczania ilości: {e}")
        return None

@app.route("/", methods=["GET"])
def index():
    return "✅ Bot działa!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    global processing, last_alert_time, position_opened_by_alert

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
        print(f"🔔 Odebrano alert: {data}")

        action = data.get("action", "").lower()  # "entry" lub "exit"
        side = data.get("side", "").lower()      # "buy" lub "sell"

        if action not in ["entry", "exit"] or side not in ["buy", "sell"]:
            send_to_discord(f"⚠️ Nieprawidłowy alert: {data}")
            processing = False
            return "Invalid alert", 400

        position_size, position_side = get_current_position(SYMBOL)

        # === EXIT ===
        if action == "exit":
            if position_size > 0:
                expected_side = "Sell" if position_side == "Buy" else "Buy"
                if side == expected_side.lower():
                    try:
                        session.place_order(
                            category="linear",
                            symbol=SYMBOL,
                            side=expected_side,
                            orderType="Market",
                            qty=position_size,
                            reduceOnly=True,
                            timeInForce="GoodTillCancel"
                        )
                        send_to_discord(f"🔒 ZAMKNIĘTO pozycję {position_side.upper()} ({position_size} {SYMBOL})")
                        position_opened_by_alert = False
                    except Exception as e:
                        send_to_discord(f"❗ Błąd przy zamykaniu pozycji: {e}")
                else:
                    send_to_discord(f"⚠️ Alert EXIT ({side}) nie pasuje do pozycji {position_side}.")
            else:
                send_to_discord("ℹ️ Brak pozycji do zamknięcia.")
            processing = False
            return "Handled exit", 200

        # === ENTRY ===
        if action == "entry":
            if position_size > 0:
                send_to_discord("⚠️ Pozycja już otwarta. Pomijam wejście.")
                processing = False
                return "Already in position", 200

            if position_opened_by_alert:
                send_to_discord("⚠️ Poprzednia pozycja nie została zamknięta alertem. Pomijam wejście.")
                processing = False
                return "Waiting for exit", 200

            qty = calculate_qty(SYMBOL)
            if qty is None or qty == 0:
                send_to_discord("⚠️ Ilość do wejścia zbyt mała.")
                processing = False
                return "Invalid qty", 400

            try:
                order_side = "Buy" if side == "buy" else "Sell"
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side=order_side,
                    orderType="Market",
                    qty=qty,
                    timeInForce="GoodTillCancel"
                )
                send_to_discord(f"📥 OTWARTO pozycję {order_side.upper()} ({qty} {SYMBOL})")
                position_opened_by_alert = True
            except Exception as e:
                send_to_discord(f"❗ Błąd przy otwieraniu pozycji: {e}")

        processing = False
        return "OK", 200

    except Exception as e:
        send_to_discord(f"❗ Błąd systemowy: {e}")
        processing = False
        return "Webhook error", 500

if __name__ == "__main__":
    print("🚀 Bot uruchomiony...")
    app.run(host="0.0.0.0", port=port)
