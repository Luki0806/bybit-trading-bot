import os
import time
import math
import json
import requests
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

# ====================== NARZĘDZIA / NORMALIZACJA ======================
def normalize_symbol(sym: str) -> str:
    """
    Normalizuje symbole z TradingView/Bybit:
    - usuwa sufiks '.P' (np. 'COAIUSDT.P' -> 'COAIUSDT')
    - obcina spacje i zamienia na UPPER
    """
    if not sym:
        return ""
    s = str(sym).strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    return s

# ====================== KONFIGURACJA ======================
try:
    from config import API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL, TESTNET, ALLOWED_SYMBOLS
except Exception:
    API_KEY = os.environ.get("API_KEY", "")
    API_SECRET = os.environ.get("API_SECRET", "")
    SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    TESTNET = os.environ.get("TESTNET", "true").lower() in ("1", "true", "yes")
    # Możesz też podać ENV: ALLOWED_SYMBOLS="WIFUSDT,COAIUSDT,COAIUSDT.P"
    ALLOWED_SYMBOLS = [
        s.strip() for s in os.environ.get("ALLOWED_SYMBOLS", "WIFUSDT,COAIUSDT").split(",") if s.strip()
    ]

# Zestaw dozwolonych symboli po normalizacji (.P usunięte)
ALLOWED_SET = {normalize_symbol(s) for s in (ALLOWED_SYMBOLS or [])}

PORT = int(os.environ.get("PORT", 5000))

# Tryby zachowania
RESPECT_MANUAL_SL = os.environ.get("RESPECT_MANUAL_SL", "true").lower() in ("1", "true", "yes")
RESPECT_MANUAL_TP = os.environ.get("RESPECT_MANUAL_TP", "true").lower() in ("1", "true", "yes")
AUTO_RESUME_ON_MANUAL_REMOVE = os.environ.get("AUTO_RESUME_ON_MANUAL_REMOVE", "true").lower() in ("1", "true", "yes")
IGNORE_NON_JSON = os.environ.get("IGNORE_NON_JSON", "true").lower() in ("1", "true", "yes")

app = Flask(__name__)
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

# ====================== STAN BOTA ======================
processing = False
last_close_ts = 0.0

last_sl_value = None
last_tp_value = None
last_sl_set_ts = 0.0
last_tp_set_ts = 0.0
manual_sl_locked = False
manual_tp_locked = False

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
    """Zwraca (size: float, side: 'Buy'|'Sell'|'None') dla podanego symbolu."""
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

def get_sl_tp(symbol: str):
    """Zwraca (current_sl: float|None, current_tp: float|None, positionIdx: int)."""
    try:
        res = session.get_positions(category="linear", symbol=symbol)
        items = (res or {}).get("result", {}).get("list", []) or []
        if not items:
            return None, None, 0
        pos = items[0]
        sl = float(pos.get("stopLoss") or 0) or None
        tp = float(pos.get("takeProfit") or 0) or None
        idx = int(pos.get("positionIdx", 0) or 0)
        return sl, tp, idx
    except Exception:
        return None, None, 0

def calculate_qty(symbol: str):
    """Proste wyliczenie ilości – 100% dostępnego USDT."""
    try:
        send_to_discord("📊 Obliczam wielkość nowej pozycji…")
        balance_data = session.get_wallet_balance(accountType="UNIFIED")
        coins = balance_data["result"]["list"][0]["coin"]
        usdt = next((c for c in coins if c.get("coin") == "USDT"), None)
        if not usdt:
            send_to_discord("❗ Brak monety USDT na koncie UNIFIED.")
            return None

        available_usdt = float(usdt.get("walletBalance", 0) or 0)
        trade_usdt = available_usdt * 1  # 100% — dostosuj wg ryzyka

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

def _isclose(a: float, b: float) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=0.0)
    except Exception:
        return str(a) == str(b)

# ====================== SL / TP ======================
def set_tp_sl_safe(symbol: str, side: str, sl_price: float | None, tp_price: float | None):
    """
    Ustawia/kasuje TP i SL dla danej pozycji.
    - respektuje manualne zmiany jeśli włączone (nie nadpisuje tej samej wartości),
    - usuwa parametry gdy przyjdzie None,
    - korzysta z LastPrice jako trigger.
    """
    global last_sl_value, last_tp_value, last_sl_set_ts, last_tp_set_ts
    try:
        cur_sl, cur_tp, idx = get_sl_tp(symbol)
        size, _ = get_current_position(symbol)
        if size <= 0:
            return {"skipped": "no position"}

        payload = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": idx,
            "tpslMode": "Full",
            "slTriggerBy": "LastPrice",
            "tpTriggerBy": "LastPrice",
        }

        # STOP LOSS
        if sl_price and sl_price > 0:
            if (not RESPECT_MANUAL_SL) or (cur_sl is None) or (not _isclose(cur_sl, sl_price)):
                payload["stopLoss"] = str(sl_price)
                session.set_trading_stop(**payload)
                last_sl_value = float(sl_price)
                last_sl_set_ts = time.time()
                send_to_discord(f"🛡️ Ustawiam SL @ {sl_price} dla {symbol}")
        elif cur_sl is not None and sl_price is None:
            payload["stopLoss"] = "0"
            session.set_trading_stop(**payload)
            last_sl_value = None
            send_to_discord(f"🧹 Kasuję SL dla {symbol}")

        # TAKE PROFIT
        if tp_price and tp_price > 0:
            if (not RESPECT_MANUAL_TP) or (cur_tp is None) or (not _isclose(cur_tp, tp_price)):
                payload["takeProfit"] = str(tp_price)
                session.set_trading_stop(**payload)
                last_tp_value = float(tp_price)
                last_tp_set_ts = time.time()
                send_to_discord(f"🎯 Ustawiam TP @ {tp_price} dla {symbol}")
        elif cur_tp is not None and tp_price is None:
            payload["takeProfit"] = "0"
            session.set_trading_stop(**payload)
            last_tp_value = None
            send_to_discord(f"🧹 Kasuję TP dla {symbol}")

        return {"ok": True}
    except Exception as e:
        send_to_discord(f"❗ Błąd set_tp_sl_safe: {e}")
        return {"error": str(e)}

# ====================== ROUTES ======================
@app.get("/")
def index():
    return "✅ Bot działa!", 200

@app.post("/webhook")
def webhook():
    global processing, last_close_ts, manual_sl_locked, manual_tp_locked

    if processing:
        return "Processing in progress", 429

    processing = True
    try:
        data = parse_incoming_json()
        if not isinstance(data, dict):
            if IGNORE_NON_JSON:
                processing = False
                return ("", 204)
            processing = False
            return "Ignored non-JSON", 204

        action = str(data.get("action", "")).lower().strip()
        # Normalizacja symbolu (usuwa .P i upper-case)
        symbol_raw = str(data.get("symbol", SYMBOL)).strip() or SYMBOL
        symbol = normalize_symbol(symbol_raw)

        # Walidacja dozwolonych symboli po normalizacji
        if symbol not in ALLOWED_SET:
            send_to_discord(f"🚫 Niedozwolony symbol: {symbol_raw} (po normalizacji: {symbol}). "
                            f"Dozwolone: {', '.join(sorted(ALLOWED_SET))}")
            processing = False
            return jsonify(error="symbol not allowed"), 400

        # Parsowanie SL/TP (opcjonalne)
        sl_val = data.get("sl")
        tp_val = data.get("tp")
        try:
            sl_price = float(sl_val) if sl_val not in ("", None) else None
        except Exception:
            sl_price = None
        try:
            tp_price = float(tp_val) if tp_val not in ("", None) else None
        except Exception:
            tp_price = None

        allowed = ("buy", "sell", "close",
                   "update_sl", "update_tp",
                   "clear_sl", "clear_tp",
                   "unlock_sl", "unlock_tp",
                   "force_update_sl", "force_update_tp")
        if action not in allowed:
            processing = False
            return ("", 204)

        # Bieżąca pozycja
        size, side = get_current_position(symbol)

        # ===== UNLOCKS =====
        if action == "unlock_sl":
            manual_sl_locked = False
            send_to_discord("🔓 Odblokowano SL (UNLOCK).")
            processing = False
            return jsonify(ok=True), 200
        if action == "unlock_tp":
            manual_tp_locked = False
            send_to_discord("🔓 Odblokowano TP (UNLOCK).")
            processing = False
            return jsonify(ok=True), 200

        # ===== FORCE UPDATE =====
        if action == "force_update_sl":
            if size > 0:
                set_tp_sl_safe(symbol, side, sl_price, None)
            processing = False
            return jsonify(ok=True), 200
        if action == "force_update_tp":
            if size > 0:
                set_tp_sl_safe(symbol, side, None, tp_price)
            processing = False
            return jsonify(ok=True), 200

        # ===== UPDATE/CLEAR =====
        if action == "update_sl":
            if size > 0:
                set_tp_sl_safe(symbol, side, sl_price, None)
            processing = False
            return jsonify(ok=True), 200
        if action == "update_tp":
            if size > 0:
                set_tp_sl_safe(symbol, side, None, tp_price)
            processing = False
            return jsonify(ok=True), 200
        if action == "clear_sl":
            if size > 0:
                set_tp_sl_safe(symbol, side, None, None if tp_val is None else (float(tp_val) if tp_val != "" else None))
            processing = False
            return jsonify(ok=True), 200
        if action == "clear_tp":
            if size > 0:
                set_tp_sl_safe(symbol, side, None if sl_val is None else (float(sl_val) if sl_val != "" else None), None)
            processing = False
            return jsonify(ok=True), 200

        # ===== CLOSE =====
        if action == "close":
            now = time.time()
            if now - last_close_ts < 1.0:
                processing = False
                return jsonify(ok=True), 200
            last_close_ts = now
            if size <= 0:
                processing = False
                return ("", 204)
            close_side = "Sell" if side == "Buy" else "Buy"
            session.place_order(category="linear", symbol=symbol, side=close_side,
                                orderType="Market", qty=size, reduceOnly=True,
                                timeInForce="GoodTillCancel")
            send_to_discord(f"🧯 CLOSE: zamknięto pozycję {side.upper()} ({size} {symbol})")
            set_tp_sl_safe(symbol, side, None, None)
            manual_sl_locked = False
            manual_tp_locked = False
            processing = False
            return jsonify(ok=True), 200

        # ===== BUY / SELL =====
        if action in ("buy", "sell"):
            # Jeśli przeciwna pozycja otwarta — zamknij
            if size > 0 and side in ("Buy", "Sell"):
                close_side = "Sell" if side == "Buy" else "Buy"
                session.place_order(category="linear", symbol=symbol, side=close_side,
                                    orderType="Market", qty=size,
                                    reduceOnly=True, timeInForce="GoodTillCancel")
                send_to_discord(f"🔒 Zamknięto pozycję {side.upper()} ({size} {symbol})")
                time.sleep(1.2)

            # Otwórz nową
            qty = calculate_qty(symbol)
            if not qty:
                processing = False
                return "Invalid qty", 400

            side_new = "Buy" if action == "buy" else "Sell"
            session.place_order(category="linear", symbol=symbol, side=side_new,
                                orderType="Market", qty=qty,
                                timeInForce="GoodTillCancel")
            send_to_discord(f"📥 Otwarto pozycję {side_new.upper()} ({qty} {symbol})")

            # Ustaw TP/SL z alertu wejściowego (jeśli podane)
            set_tp_sl_safe(symbol, side_new, sl_price, tp_price)

            processing = False
            return jsonify(ok=True), 200

        processing = False
        return jsonify(ok=True), 200

    except Exception as e:
        send_to_discord(f"❗ Błąd systemowy: {e}")
        processing = False
        return "Webhook error", 500

if __name__ == "__main__":
    print("🚀 Bot uruchomiony…")
    print(f"✅ Dozwolone pary: {', '.join(sorted(ALLOWED_SET))}")
    app.run(host="0.0.0.0", port=PORT)
