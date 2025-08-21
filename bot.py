import os
import time
import requests
import json
from flask import Flask, request
from pybit.unified_trading import HTTP
# Zaimportuj zmienna POSITION_PERCENT z pliku config
from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET, POSITION_PERCENT

app = Flask(__name__)
port = int(os.environ.get("PORT", 5000))

# Inicjalizacja sesji z kluczami API
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

# Zmienne globalne do obslugi limitow i duplikatow alertow
processing = False
last_alert_time = 0
last_action = None
ALERT_COOLDOWN = 4  # sekundy

def send_to_discord(message):
    """Wysyla wiadomosc na kanal Discord."""
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
    except Exception as e:
        print(f"❌ Blad wysylania do Discord: {e}")

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
    """
    Oblicza wielkosc pozycji na podstawie salda portfela i ustalonego procentu.
    
    :param symbol: Symbol waluty (np. "WIFUSDT")
    :param portion: Procent salda do uzycia (np. 0.1 dla 10%)
    :return: Wielkosc pozycji w jednostkach lub None w przypadku bledu.
    """
    try:
        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        # Wyszukuje dostepne saldo USDT
        usdt = next(c for c in balance_data["result"]["list"][0]["coin"] if c["coin"] == "USDT")
        available = float(usdt.get("walletBalance", 0))
        # Pobiera aktualna cene symbolu
        price_info = next(i for i in session.get_tickers(category="linear")["result"]["list"] if i["symbol"] == symbol)
        
        # Oblicza wielkosc pozycji
        qty = int((available * portion) / float(price_info["lastPrice"]))
        return qty
    except Exception as e:
        send_to_discord(f"❗ Blad obliczania wielkosci pozycji: {e}")
        return None

@app.route("/", methods=["GET"])
def index():
    """Endpoint testowy dla sprawdzenia, czy bot dziala."""
    return "✅ Bot dziala!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    """Glowny endpoint do odbierania alertow z TradingView."""
    global processing, last_alert_time, last_action

    now = time.time()

    # Logika sprawdzajaca, czy bot nie przetwarza juz alertu
    if processing:
        send_to_discord("⏳ Alert zignorowany - wciaz przetwarzam poprzedni.")
        return "Still processing", 429

    # Logika sprawdzajaca odstep czasu miedzy alertami
    if now - last_alert_time < ALERT_COOLDOWN:
        send_to_discord("⏳ Alert zignorowany - zbyt krotki odstep czasu.")
        return "Cooldown", 429

    try:
        # Zmodyfikowana sekcja obslugi danych JSON
        # Po prostu probujemy zaladowac JSON z ciala zadania, niezaleznie od Content-Type
        try:
            data = json.loads(request.data)
        except json.JSONDecodeError:
            send_to_discord("⚠️ Nieprawidlowy alert: dane nie sa w formacie JSON")
            return "Invalid JSON", 400

        action = data.get("action", "").lower()
        if not action:
            send_to_discord("⚠️ Nieprawidlowy alert: brak pola 'action'")
            return "No action", 400

        # Logika sprawdzajaca duplikaty alertow
        if action == last_action:
            send_to_discord(f"⚠️ Zignorowano duplikat alertu: {action}")
            return "Duplicate alert", 429

        processing = True
        last_alert_time = now
        last_action = action

        size, side = get_current_position(SYMBOL)

        if action == "buy" and size == 0:
            # Uzyj POSITION_PERCENT z pliku config
            qty = calculate_qty(SYMBOL, POSITION_PERCENT)
            if qty:
                session.place_order(category="linear", symbol=SYMBOL, side="Buy", orderType="Market", qty=qty)
                send_to_discord(f"📈 Otwarto pozycje BUY ({qty})")

        elif action == "sell" and size == 0:
            # Uzyj POSITION_PERCENT z pliku config
            qty = calculate_qty(SYMBOL, POSITION_PERCENT)
            if qty:
                session.place_order(category="linear", symbol=SYMBOL, side="Sell", orderType="Market", qty=qty)
                send_to_discord(f"📉 Otwarto pozycje SELL ({qty})")

        elif action == "close_buy" and size > 0 and side == "Buy":
            session.place_order(category="linear", symbol=SYMBOL, side="Sell", orderType="Market",
                                qty=size, reduceOnly=True)
            send_to_discord(f"🔒 Zamknieto pozycje BUY ({size})")

        elif action == "close_sell" and size > 0 and side == "Sell":
            session.place_order(category="linear", symbol=SYMBOL, side="Buy", orderType="Market",
                                qty=size, reduceOnly=True)
            send_to_discord(f"🔒 Zamknieto pozycje SELL ({size})")

        else:
            send_to_discord(f"⚠️ Alert '{action}' zignorowany - nie pasuje do obecnej pozycji ({side}, size: {size})")

        return "OK", 200

    except Exception as e:
        send_to_discord(f"❗ Blad w webhook: {e}")
        return "Error", 500

    finally:
        processing = False


if __name__ == "__main__":
    print("🚀 Bot uruchomiony...")
    app.run(host="0.0.0.0", port=port)
