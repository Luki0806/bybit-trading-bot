import os
import time
import requests
import json
from flask import Flask, request
from pybit.unified_trading import HTTP
# Zmienna POSITION_PERCENT oraz inne dane z config
from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET, POSITION_PERCENT

# Dodajemy własną konfigurację dźwigni
LEVERAGE = 2  # <-- Tutaj ustawiasz swoją dźwignię np. x2

app = Flask(__name__)
port = int(os.environ.get("PORT", 5000))

# Inicjalizacja sesji z kluczami API
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

# === FUNKCJE POMOCNICZE ===

def send_to_discord(message):
    """Wysyla wiadomosc na kanal Discord."""
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
    except Exception as e:
        print(f"❌ Blad wysylania do Discord: {e}")


def set_leverage(symbol, leverage):
    """Ustawia dźwignię tylko, jeśli jest dozwolona dla danego symbolu."""
    try:
        # Pobierz info o instrumencie
        info = session.get_instruments_info(category="linear", symbol=symbol)
        data = info["result"]["list"][0]

        min_leverage = int(data.get("leverageFilter", {}).get("minLeverage", 1))
        max_leverage = int(data.get("leverageFilter", {}).get("maxLeverage", 100))

        if min_leverage <= leverage <= max_leverage:
            session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
            send_to_discord(f"⚙️ Ustawiono dźwignię {leverage}x dla {symbol}")
        else:
            send_to_discord(f"⚠️ Wybrana dźwignia {leverage}x poza zakresem ({min_leverage}–{max_leverage}). Pozostaje domyślna.")

    except Exception as e:
        send_to_discord(f"❗ Błąd ustawiania dźwigni: {e}")


def get_current_position(symbol):
    """Pobiera aktualny rozmiar i kierunek pozycji."""
    try:
        result = session.get_positions(category="linear", symbol=symbol)
        pos = result["result"]["list"][0]
        return float(pos["size"]), pos["side"]
    except Exception as e:
        send_to_discord(f"❗ Blad pobierania pozycji: {e}")
        return 0.0, "None"


def calculate_qty(symbol, portion):
    """Oblicza wielkość pozycji na podstawie salda portfela i procentu."""
    try:
        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        usdt = next(c for c in balance_data["result"]["list"][0]["coin"] if c["coin"] == "USDT")
        available = float(usdt.get("walletBalance", 0))
        price_info = next(i for i in session.get_tickers(category="linear")["result"]["list"] if i["symbol"] == symbol)
        qty = int((available * portion * LEVERAGE) / float(price_info["lastPrice"]))  # uwzględniamy dźwignię
        return qty
    except Exception as e:
        send_to_discord(f"❗ Blad obliczania wielkosci pozycji: {e}")
        return None


def retry_close_order(symbol, side, size, retries=5, delay=1):
    """Ponawia próbę zamknięcia pozycji w razie błędu."""
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
            send_to_discord(f"🔒 Zamknieto pozycje {side.upper()} ({size}) (proba {i+1})")
            return True
        except Exception as e:
            send_to_discord(f"❗ Blad zamykania pozycji: {e} (proba {i+1}/{retries})")
            time.sleep(delay)
    send_to_discord(f"❌ Nie udalo sie zamknac pozycji {side.upper()} ({size}) po {retries} probach.")
    return False


# === API ENDPOINTY ===

@app.route("/", methods=["GET"])
def index():
    return "✅ Bot dziala!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    global processing, last_alert_time, last_action
    now = time.time()

    if processing:
        return "Still processing", 429
    if now - last_alert_time < ALERT_COOLDOWN:
        return "Cooldown", 429

    try:
        data = json.loads(request.data)
        action = data.get("action", "").lower()
        if not action:
            return "No action", 400

        processing = True
        last_alert_time = now
        last_action = action

        size, side = get_current_position(SYMBOL)

        if action == "buy" and size == 0:
            qty = calculate_qty(SYMBOL, POSITION_PERCENT)
            if qty:
                session.place_order(category="linear", symbol=SYMBOL, side="Buy", orderType="Market", qty=qty)
                send_to_discord(f"📈 Otwarto pozycje BUY ({qty})")

        elif action == "sell" and size == 0:
            qty = calculate_qty(SYMBOL, POSITION_PERCENT)
            if qty:
                session.place_order(category="linear", symbol=SYMBOL, side="Sell", orderType="Market", qty=qty)
                send_to_discord(f"📉 Otwarto pozycje SELL ({qty})")

        elif action == "close_buy" and size > 0 and side == "Buy":
            retry_close_order(SYMBOL, "Buy", size)

        elif action == "close_sell" and size > 0 and side == "Sell":
            retry_close_order(SYMBOL, "Sell", size)

        return "OK", 200

    except Exception as e:
        send_to_discord(f"❗ Blad w webhook: {e}")
        return "Error", 500

    finally:
        processing = False


if __name__ == "__main__":
    print("🚀 Bot uruchomiony...")
    # Najpierw ustawiamy dźwignię
    set_leverage(SYMB_
