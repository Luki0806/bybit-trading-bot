import os
from flask import Flask, request
from pybit.unified_trading import HTTP
import requests
import time
from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET

# Tworzymy instancję aplikacji Flask
app = Flask(__name__)

# Upewnij się, że używasz poprawnego portu z Render
port = int(os.environ.get("PORT", 5000))

session = HTTP(
    api_key=API_KEY,
    api_secret=API_SECRET,
    testnet=TESTNET
)

def send_to_discord(message):
    """Funkcja wysyłająca wiadomość na Discord."""
    try:
        payload = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"❌ Błąd wysyłania do Discord: {e}")

def get_current_position(symbol):
    """Funkcja sprawdzająca, czy istnieje otwarta pozycja."""
    try:
        result = session.get_positions(category="linear", symbol=symbol)
        position = result["result"]["list"][0]
        size = float(position["size"])
        side = position["side"]
        print(f"🔄 Pozycja: {side} o rozmiarze {size}")
        
        # Sprawdzamy, czy pozycja jest wystarczająco duża, aby ją zamknąć (min. 0.0001)
        if size < 0.0001:
            send_to_discord(f"⚠️ Pozycja {side} jest zbyt mała, aby ją zamknąć.")
            return 0.0, "None"
        
        return size, side
    except Exception as e:
        send_to_discord(f"⚠️ Błąd pobierania pozycji: {e}")
        return 0.0, "None"

def calculate_qty(symbol):
    """Funkcja do obliczania ilości do zlecenia na podstawie salda."""
    try:
        send_to_discord("🔍 Rozpoczynam obliczanie ilości...")

        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        balance_info = balance_data["result"]["list"][0]["coin"]
        usdt = next(c for c in balance_info if c["coin"] == "USDT")
        available_usdt = float(usdt.get("walletBalance", 0))
        trade_usdt = available_usdt * 0.5  # Używamy 50% dostępnego USDT

        tickers_data = session.get_tickers(category="linear")
        price_info = next((item for item in tickers_data["result"]["list"] if item["symbol"] == symbol), None)

        if not price_info:
            send_to_discord(f"⚠️ Symbol {symbol} nie został znaleziony.")
            return None

        last_price = float(price_info["lastPrice"])
        qty = int(trade_usdt / last_price)

        send_to_discord(f"✅ Obliczona ilość: {qty} {symbol} przy cenie {last_price} USDT")
        return qty
    except Exception as e:
        send_to_discord(f"⚠️ Błąd obliczania ilości: {e}")
        return None

def round_to_precision(value, precision=4):
    """Funkcja do zaokrąglania wartości do określonej liczby miejsc po przecinku (domyślnie 4)."""
    return round(value, precision)

@app.route("/", methods=["GET"])
def index():
    return "✅ Bot działa!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    """Obsługuje przychodzący webhook z TradingView."""
    try:
        data = request.get_json()
        print(f"🔔 Otrzymano webhook: {data}")  # Logowanie otrzymanych danych
        action = data.get("action", "").lower()

        if action not in ["buy", "sell"]:
            send_to_discord("⚠️ Nieprawidłowe polecenie. Użyj 'buy' lub 'sell'.")
            return "Invalid action", 400

        # Sprawdzanie, czy pozycja jest otwarta
        position_size, position_side = get_current_position(SYMBOL)

        # Jeśli pozycja jest już otwarta w odpowiednim kierunku
        if position_size > 0 and position_side == "Buy" and action == "buy":
            send_to_discord("⚠️ Pozycja już otwarta w odpowiednim kierunku (BUY), nie składam nowego zlecenia.")
            return "Pozycja już otwarta w odpowiednim kierunku", 200
        
        if position_size > 0 and position_side == "Sell" and action == "sell":
            send_to_discord("⚠️ Pozycja już otwarta w odpowiednim kierunku (SELL), nie składam nowego zlecenia.")
            return "Pozycja już otwarta w odpowiednim kierunku", 200

        # Jeśli pozycja jest otwarta w przeciwnym kierunku, zamknij ją
        if position_size > 0 and (
            (position_side == "Buy" and action == "sell") or
            (position_side == "Sell" and action == "buy")
        ):
            # Zamykamy poprzednią pozycję
            close_side = "Sell" if position_side == "Buy" else "Buy"
            close_order = session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=close_side,
                orderType="Market",
                qty=position_size,
                reduceOnly=True,
                timeInForce="GoodTillCancel"
            )
            send_to_discord(f"🔒 Zamknięcie pozycji {position_side.upper()} ({position_size} {SYMBOL})")

            # Po zamknięciu poprzedniej pozycji składamy nowe zlecenie
            qty = calculate_qty(SYMBOL)
            if qty is not None and qty > 0:
                if action == "buy":
                    new_order = session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Buy",
                        orderType="Market",
                        qty=qty,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"✅ Zlecenie BUY złożone: {qty} {SYMBOL}")
                elif action == "sell":
                    new_order = session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Sell",
                        orderType="Market",
                        qty=qty,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"✅ Zlecenie SELL złożone: {qty} {SYMBOL}")
        else:
            # Jeśli pozycja nie jest otwarta, składamy nowe zlecenie
            qty = calculate_qty(SYMBOL)
            if qty is not None and qty > 0:
                if action == "buy":
                    new_order = session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Buy",
                        orderType="Market",
                        qty=qty,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"✅ Zlecenie BUY złożone: {qty} {SYMBOL}")
                elif action == "sell":
                    new_order = session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Sell",
                        orderType="Market",
                        qty=qty,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"✅ Zlecenie SELL złożone: {qty} {SYMBOL}")
        return "OK", 200

    except Exception as e:
        send_to_discord(f"❌ Błąd składania zlecenia: {e}")
        print(f"❌ Błąd: {e}")  # Logowanie błędu
        return "Order error", 500

if __name__ == "__main__":
    print("Bot uruchomiony...")  # Logowanie rozpoczęcia działania bota
    app.run(host="0.0.0.0", port=port)
