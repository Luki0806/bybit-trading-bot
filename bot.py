import os
import time
import math
import json
import requests
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

# ====================== KONFIGURACJA ======================
try:
    # teraz importujemy też ALLOWED_SYMBOLS z config.py
    from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET, ALLOWED_SYMBOLS
except Exception:
    API_KEY = os.environ.get("API_KEY", "")
    API_SECRET = os.environ.get("API_SECRET", "")
    SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    TESTNET = os.environ.get("TESTNET", "true").lower() in ("1", "true", "yes")
    # fallback: można też podać przez ENV: ALLOWED_SYMBOLS="WIFUSDT,COAIUSDT"
    ALLOWED_SYMBOLS = [
        s.strip().upper()
        for s in os.environ.get("ALLOWED_SYMBOLS", "WIFUSDT,COAIUSDT").split(",")
        if s.strip()
    ]

PORT = int(os.environ.get("PORT", 5000))

# Tryby zachowania
RESPECT_MANUAL_SL = os.environ.get("RESPECT_MANUAL_SL", "true").lower() in ("1", "true", "yes")
AUTO_RESUME_ON_MANUAL_REMOVE = os.environ.get("AUTO_RESUME_ON_MANUAL_REMOVE", "true").lower() in ("1", "true", "yes")
IGNORE_NON_JSON = os.environ.get("IGNORE_NON_JSON", "true").lower() in ("1", "true", "yes")  # <— nowość

app = Flask(__name__)
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

# ====================== STAN BOTA ======================
processing = False
last_close_ts = 0.0

# Pamięć SL do wykrywania zmian manualnych
last_sl_value = None       # ostatni SL ustawiony przez bota (float lub None)
last_sl_set_ts = 0.0       # kiedy bot ostatnio ustawił SL (time.time())
manual_sl_locked = False   # gdy True: ignorujemy update_sl/clear_sl, aż do unlock/force

# ====================== POMOCNICZE ======================
def send_to_discord(message: str):
    if not DISCORD_WEBHOOK_URL:
        print(f"[Discord OFF] {message}")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"❌ Błąd wysyłania do Discord: {e}")

def parse_incoming_json():
    data = request.get_json(silent=True)
    if data is not None:
        return data
    raw = request.data.decode("utf-8") if request.data else ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def get_current_position(symbol: str):
    """Zwraca (size: float, side: 'Buy'|'Sell'|'None')"""
    try:
        result = session.get_positions(category="linear", symbol=symbol)
        items = (result or {}).get("result", {}).get("list", []) or []
        if not items:
            return 0.0, "None"
        position = items[0]
        size = float(position.get("size") or 0)
        side = position.get("side") or "None"
        return size, side
    except Exception as e:
        send_to_discord(f"❗ Błąd pobierania pozycji: {e}")
        return 0.0, "None"

def get_position_stop_loss(symbol: str):
    """Zwraca (current_sl: float|None, positionIdx: int)"""
    try:
        res = session.get_positions(category="linear", symbol=symbol)
        items = (res or {}).get("result", {}).get("list", []) or []
        if not items:
            return None, 0
        pos = items[0]
        sl_str = pos.get("stopLoss") or ""
        sl = float(sl_str) if sl_str not in ("", "0", 0, None) else None
        idx = int(pos.get("positionIdx", 0) or 0)
        return sl, idx
    except Exception:
        return None, 0

def calculate_qty(symbol: str):
    """Proste wyliczenie ilości (100% USDT / lastPrice)."""
    try:
        send_to_discord("📊 Obliczam wielkość nowej pozycji…")
        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        coins = balance_data["result"]["list"][0]["coin"]
        usdt = next((c for c in coins if c.get("coin") == "USDT"), None)
        if not usdt:
            send_to_discord("❗ Brak monety USDT na koncie UNIFIED.")
            return None

        available_usdt = float(usdt.get("walletBalance", 0) or 0)
        trade_usdt = available_usdt * 1  # 100% — zmień wedle ryzyka

        tickers_data = session.get_tickers(category="linear")
        price_info = next((it for it in tickers_data["result"]["list"] if it.get("symbol") == symbol), None)
        if not price_info:
            send_to_discord(f"❗ Symbol {symbol} nie znaleziony.")
            return None

        last_price = float(price_info.get("lastPrice") or 0)
        if last_price <= 0:
            send_to_discord("❗ Nieprawidłowa cena rynkowa.")
            return None

        qty = int(trade_usdt / last_price)
        if qty < 1:
            send_to_discord("❗ Wyliczona ilość < 1, nie złożę zlecenia.")
            return None

        send_to_discord(f"✅ Ilość do zlecenia: {qty} {symbol} przy cenie {last_price} USDT")
        return qty
    except Exception as e:
        send_to_discord(f"❗ Błąd podczas obliczania ilości: {e}")
        return None

# ---------- SL / TRADING-STOP ----------
def _isclose_num(a: float, b: float) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=0.0)
    except Exception:
        return str(a) == str(b)

def set_stop_loss_safe(symbol: str, side: str, sl_price: float | None):
    """Ustawia/kasuje SL z obsługą błędu 34040."""
    global last_sl_value, last_sl_set_ts
    try:
        current_sl, idx_from_pos = get_position_stop_loss(symbol)
        size, _ = get_current_position(symbol)
        if size <= 0:
            return {"skipped": "no position"}

        payload = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": idx_from_pos,
            "slTriggerBy": "LastPrice",
            "tpslMode": "Full",
        }

        if sl_price and sl_price > 0:
            if current_sl is not None and _isclose_num(current_sl, sl_price):
                return {"skipped": "not modified (same value)"}
            payload["stopLoss"] = str(sl_price)
            session.set_trading_stop(**payload)
            last_sl_value = float(sl_price)
            last_sl_set_ts = time.time()
            send_to_discord(f"🛡️ Ustawiam SL {side.upper()} @ {sl_price} na {symbol}")
            return {"ok": True}
        else:
            if current_sl is None:
                return {"skipped": "not modified (already cleared)"}
            payload["stopLoss"] = "0"
            session.set_trading_stop(**payload)
            last_sl_value = None
            last_sl_set_ts = time.time()
            send_to_discord(f"🧹 Kasuję SL dla {side.upper()} na {symbol}")
            return {"ok": True}

    except Exception as e:
        send_to_discord(f"❗ Błąd set_trading_stop: {e}")
        return {"error": str(e)}

# ====================== ROUTES ======================
@app.get("/")
def index():
    return "✅ Bot działa!", 200

@app.post("/webhook")
def webhook():
    global processing, last_close_ts, manual_sl_locked, last_sl_value, last_sl_set_ts

    if processing:
        return "Processing in progress", 429

    processing = True
    try:
        data = parse_incoming_json()
        if not isinstance(data, dict):
            if IGNORE_NON_JSON:
                processing = False
                return ("", 204)
        if not isinstance(data, dict):
            processing = False
            return "Ignored non-JSON", 204

        action = str(data.get("action", "")).lower().strip()
        symbol = str(data.get("symbol", SYMBOL)).upper().strip() or SYMBOL

        # ✅ Walidacja symbolu
        if symbol not in ALLOWED_SYMBOLS:
            send_to_discord(f"🚫 Odrzucono nieautoryzowany symbol: {symbol}. Dozwolone: {', '.join(ALLOWED_SYMBOLS)}")
            processing = False
            return jsonify(error="symbol not allowed"), 400

        sl_val = data.get("sl")
        try:
            sl_price = float(sl_val) if sl_val not in ("", None) else None
        except Exception:
            sl_price = None

        allowed = ("buy", "sell", "update_sl", "clear_sl", "close", "unlock_sl", "force_update_sl")
        if action not in allowed:
            processing = False
            return ("", 204)

        # ===== UNLOCK SL =====
        if action == "unlock_sl":
            manual_sl_locked = False
            send_to_discord("🔓 Odblokowano ręczny SL (UNLOCK).")
            processing = False
            return jsonify(ok=True), 200

        # ===== FORCE SL =====
        if action == "force_update_sl":
            size, side = get_current_position(symbol)
            if size > 0:
                set_stop_loss_safe(symbol, side, sl_price)
                manual_sl_locked = False
                send_to_discord("⚠️ FORCE: zaktualizowano SL mimo locka.")
            else:
                send_to_discord("ℹ️ FORCE: brak pozycji — pomijam.")
            processing = False
            return jsonify(ok=True), 200

        # ===== Aktualizacja/kasowanie SL =====
        if action in ("update_sl", "clear_sl"):
            size, side = get_current_position(symbol)
            if size <= 0:
                last_sl_value = None
                manual_sl_locked = False
                processing = False
                return ("", 204)
            target_sl = None if action == "clear_sl" else sl_price
            set_stop_loss_safe(symbol, side, target_sl)
            processing = False
            return jsonify(ok=True), 200

        # ===== Zamknięcie pozycji =====
        if action == "close":
            now = time.time()
            if now - last_close_ts < 1.0:
                processing = False
                return jsonify(ok=True), 200
            last_close_ts = now
            size, side = get_current_position(symbol)
            if size <= 0:
                processing = False
                return ("", 204)
            close_side = "Sell" if side == "Buy" else "Buy"
            session.place_order(category="linear", symbol=symbol, side=close_side,
                                orderType="Market", qty=size, reduceOnly=True,
                                timeInForce="GoodTillCancel")
            send_to_discord(f"🧯 CLOSE: zamknięto pozycję {side.upper()} ({size} {symbol})")
            set_stop_loss_safe(symbol, side, None)
            manual_sl_locked = False
            last_sl_value = None
            processing = False
            return jsonify(ok=True), 200

        # ===== BUY / SELL =====
        position_size, position_side = get_current_position(symbol)

        if position_size > 0 and (
            (action == "buy" and position_side == "Buy")
            or (action == "sell" and position_side == "Sell")
        ):
            if sl_price is not None:
                set_stop_loss_safe(symbol, position_side, sl_price)
            processing = False
            return jsonify(ok=True), 200

        # Jeśli odwrotna pozycja — zamknij
        if position_size > 0.0001 and position_side in ("Buy", "Sell"):
            close_side = "Sell" if position_side == "Buy" else "Buy"
            session.place_order(category="linear", symbol=symbol, side=close_side,
                                orderType="Market", qty=position_size,
                                reduceOnly=True, timeInForce="GoodTillCancel")
            send_to_discord(f"🔒 Zamknięto pozycję {position_side.upper()} ({position_size} {symbol})")
            time.sleep(1.2)

        # Nowa pozycja
        position_size, _ = get_current_position(symbol)
        if position_size < 0.0001:
            manual_sl_locked = False
            last_sl_value = None
            qty = calculate_qty(symbol)
            if not qty:
                processing = False
                return "Invalid qty", 400
            side = "Buy" if action == "buy" else "Sell"
            session.place_order(category="linear", symbol=symbol, side=side,
                                orderType="Market", qty=qty,
                                timeInForce="GoodTillCancel")
            send_to_discord(f"📥 Otwarto pozycję {side.upper()} ({qty} {symbol})")
            if sl_price is not None:
                set_stop_loss_safe(symbol, side, sl_price)
        processing = False
        return jsonify(ok=True), 200

    except Exception as e:
        send_to_discord(f"❗ Błąd systemowy: {e}")
        processing = False
        return "Webhook error", 500

if __name__ == "__main__":
    print("🚀 Bot uruchomiony…")
    print(f"✅ Dozwolone pary: {', '.join(ALLOWED_SYMBOLS)}")
    app.run(host="0.0.0.0", port=PORT)
