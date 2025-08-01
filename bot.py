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
        print(f"🔔 Odebrano alert: {data}")
        action = data.get("action", "").lower()

        if action not in ["buy", "sell"]:
            send_to_discord(f"⚠️ Nieprawidłowe polecenie: '{action}'. Dozwolone: 'buy' lub 'sell'.")
            processing = False
            return "Invalid action", 400

        position_size, position_side = get_current_position(SYMBOL)

        # Sprawdzenie czy pozycja jest już w odpowiednim kierunku
        if position_size > 0 and (
            (action == "buy" and position_side == "Buy") or
            (action == "sell" and position_side == "Sell")
        ):
            send_to_discord(f"ℹ️ Pozycja już otwarta w kierunku {position_side.upper()} — brak akcji.")
            processing = False
            return "Position already open", 200

        # Zamknięcie pozycji jeśli otwarta i niezgodna
        if position_size > 0.0001:
            close_side = "Sell" if position_side == "Buy" else "Buy"
            try:
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side=close_side,
                    orderType="Market",
                    qty=position_size,
                    reduceOnly=True,
                    timeInForce="GoodTillCancel"
                )
                send_to_discord(f"🔒 Zamknięto pozycję {position_side.upper()} ({position_size} {SYMBOL})")
                time.sleep(1.5)
            except Exception as e:
                send_to_discord(f"❗ Błąd przy zamykaniu pozycji: {e}")

        # Otwarcie nowej pozycji jeśli brak pozycji
        position_size, _ = get_current_position(SYMBOL)
        if position_size < 0.0001:
            qty = calculate_qty(SYMBOL)
            if qty is None or qty == 0:
                send_to_discord("⚠️ Zbyt mała ilość do otwarcia pozycji. Anuluję.")
                processing = False
                return "Invalid qty", 400

            try:
                side = "Buy" if action == "buy" else "Sell"
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side=side,
                    orderType="Market",
                    qty=qty,
                    timeInForce="GoodTillCancel"
                )
                send_to_discord(f"📥 Otwarto pozycję {side.upper()} ({qty} {SYMBOL})")
            except Exception as e:
                send_to_discord(f"❗ Błąd przy składaniu zlecenia: {e}")

        processing = False
        return "OK", 200

    except Exception as e:
        send_to_discord(f"❗ Błąd systemowy: {e}")
        processing = False
        return "Webhook error", 500

if __name__ == "__main__":
    print("🚀 Bot uruchomiony...")
    app.run(host="0.0.0.0", port=port)
