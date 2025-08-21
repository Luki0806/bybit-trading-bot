import os
import time
import requests
import json
from flask import Flask, request
from pybit.unified_trading import HTTP
# Zaimportuj zmienne z pliku config
from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET, POSITION_PERCENT, LEVERAGE

app = Flask(__name__)
port = int(os.environ.get("PORT", 5000))

# Inicjalizacja sesji
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

# Zmienne globalne
processing = False
last_alert_time = 0
last_action = None
ALERT_COOLDOWN = 4  # sekundy


def send_to_discord(message):
    """Wysyła wiadomość na Discord."""
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
    except Exception as e:
        print(f"❌ Błąd wysyłania do Discord: {e}")


def set_leverage(symbol, leverage):
    """Ustawia dźwignię na Bybit."""
    try:
        session.set_leverage(
            category="contract",   # ważne: contract, nie linear
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage)
        )
        send_to_discord(f"⚙️ Ustawiono dźwignię {leverage}x dla {symbol}")
    except Exception as e:
        send_to_discord(f"❗ Błąd ustawiania dźwigni: {e}")


def get_current_position(symbol):
    """Pobiera aktualny rozmiar i kierunek pozycji."""
    try:
        result = session.get_positions(category="linear", symbol=symbol)
        pos = result["result"]["list"][0]
        return float(pos["size"]), pos["side"]
    except Exception as e:
        send_to_discord(f"❗ Błąd pobierania pozycji: {e}")
        return 0.0, "None"


def calculate_qty(symbol, portion):
    """Oblicza wielkość pozycji."""
    try:
        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        usdt = next(c for c in balance_data["result"]["list"][0]["coin"] if c["coin"] == "USDT")
        available = float(usdt.get("walletBalance", 0))
        price_info = next(i for i in session.get_tickers(category="linear")["result"]["list"] if i["symbol"] == symbol)
        qty = int((available * portion * LEVERAGE) / float(price_info["lastPrice"]))
        return qty
    except Exception as e:
        send_to_discord(f"❗ Błąd obliczania wielkości pozycji: {e}")
        return None


def retry_close_order(symbol, side, size, retries=5, delay=1):
    """Próbuje zamknąć pozycję z kilkoma próbami."""
    for i in range(retries):
        try:
            close_side = "Buy" if side == "Sell" else "Sell"
            session.place_order(
                category="linear",
                symbol=symbol,
                side=close_side,
                orderType="Market",
                qty=size,
                reduceOnly=True
            )
            send_to_discord(f"🔒 Zamknięto pozycję {side.upper()} ({size}) (próba {i+1})")
            return True
        except Exception as e:
            send_to_discord(f"❗ Błąd zamykania pozycji: {e} (próba {i+1}/{retries})")
            time.sleep(delay)
    send_to_discord(f"❌ Nie udało się zamknąć pozycji {side.upper()} ({size}) po {retries} próbach.")
    return False


@app.route("/", methods=["GET"])
def index():
    return "✅ Bot działa!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    global processing, last_alert_time, last_action
    now = time.time()

    if processing:
        send_to_discord("⏳ Alert zignorowany - wciąż przetwarzam poprzedni.")
        return "Still processing", 429

    if now - last_alert_time < ALERT_COOLDOWN:
        send_to_discord("⏳ Alert zignorowany - zbyt krótki odstęp czasu.")
        return "Cooldown", 429

    try:
        try:
            data = json.loads(request.data)
        except json.JSONDecodeError:
            send_to_discord("⚠️ Nieprawidłowy alert: dane nie są w formacie JSON")
            return "Invalid JSON", 400

        action = data.get("action", "").lower()
        if not action:
            send_to_discord("⚠️ Nieprawidłowy alert: brak pola 'action'")
            return "No action", 400

        if action == last_action:
            send_to_discord(f"⚠️ Zignorowano duplikat alertu: {action}")
            return "Duplicate alert", 429

        processing = True
        last_alert_time = now
        last_action = action

        size, side = get_current_position(SYMBOL)

        if action == "buy" and size == 0:
            qty = calculate_qty(SYMBOL, POSITION_PERCENT)
            if qty:
                session.place_order(category="linear", symbol=SYMBOL, side="Buy", orderType="Market", qty=qty)
                send_to_discord(f"📈 Otwarto pozycję BUY ({qty})")

        elif action == "sell" and size == 0:
            qty = calculate_qty(SYMBOL, POSITION_PERCENT)
            if qty:
                session.place_order(category="linear", symbol=SYMBOL, side="Sell", orderType="Market", qty=qty)
                send_to_discord(f"📉 Otwarto pozycję SELL ({qty})")

        elif action == "close_buy" and size > 0 and side == "Buy":
            retry_close_order(SYMBOL, "Buy", size)

        elif action == "close_sell" and size > 0 and side == "Sell":
            retry_close_order(SYMBOL, "Sell", size)

        else:
            send_to_discord(f"⚠️ Alert '{action}' zignorowany - nie pasuje do obecnej pozycji ({side}, size: {size})")

        return "OK", 200

    except Exception as e:
        send_to_discord(f"❗ Błąd w webhook: {e}")
        return "Error", 500

    finally:
        processing = False


if __name__ == "__main__":
    print("🚀 Bot uruchomiony...")
    # ustawiamy dźwignię przy starcie
    set_leverage(SYMBOL, LEVERAGE)
    app.run(host="0.0.0.0", port=port)
