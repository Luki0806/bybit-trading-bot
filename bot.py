import os
import time
import math
import json
import requests
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

# ====================== NARZĘDZIA / NORMALIZACJA ======================

def normalize_symbol(sym: str) -> str:
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
ALLOWED_SYMBOLS = [
s.strip() for s in os.environ.get("ALLOWED_SYMBOLS", "WIFUSDT,COAIUSDT").split(",") if s.strip()
]

ALLOWED_SET = {normalize_symbol(s) for s in (ALLOWED_SYMBOLS or [])}
PORT = int(os.environ.get("PORT", 5000))

RESPECT_MANUAL_SL = os.environ.get("RESPECT_MANUAL_SL", "true").lower() in ("1", "true", "yes")
RESPECT_MANUAL_TP = os.environ.get("RESPECT_MANUAL_TP", "true").lower() in ("1", "true", "yes")
AUTO_RESUME_ON_MANUAL_REMOVE = os.environ.get("AUTO_RESUME_ON_MANUAL_REMOVE", "true").lower() in ("1", "true", "yes")
IGNORE_NON_JSON = os.environ.get("IGNORE_NON_JSON", "true").lower() in ("1", "true", "yes")

app = Flask(**name**)
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
try:
send_to_discord("📊 Obliczam wielkość nowej pozycji…")
balance_data = session.get_wallet_balance(accountType="UNIFIED")
coins = balance_data["result"]["list"][0]["coin"]
usdt = next((c for c in coins if c.get("coin") == "USDT"), None)
if not usdt:
send_to_discord("❗ Brak monety USDT na koncie UNIFIED.")
return None

```
    available_usdt = float(usdt.get("walletBalance", 0) or 0)
    trade_usdt = available_usdt * 1
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
```

def _isclose(a: float, b: float) -> bool:
try:
return math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=0.0)
except Exception:
return str(a) == str(b)

# ====================== SL / TP ======================

def set_tp_sl_safe(symbol: str, side: str, sl_price=None, tp_price=None,
clear_sl: bool = False, clear_tp: bool = False):
global last_sl_value, last_tp_value, last_sl_set_ts, last_tp_set_ts
try:
cur_sl, cur_tp, idx = get_sl_tp(symbol)
size, _ = get_current_position(symbol)
if size <= 0:
return {"skipped": "no position"}

```
    want_sl, want_tp = None, None

    if clear_sl:
        want_sl = "0"
    elif sl_price is not None and sl_price > 0:
        if (not RESPECT_MANUAL_SL) or (cur_sl is None) or (not _isclose(cur_sl, sl_price)):
            want_sl = str(sl_price)

    if clear_tp:
        want_tp = "0"
    elif tp_price is not None and tp_price > 0:
        if (not RESPECT_MANUAL_TP) or (cur_tp is None) or (not _isclose(cur_tp, tp_price)):
            want_tp = str(tp_price)

    if want_sl is not None and want_tp is None and cur_tp is not None and not clear_tp:
        want_tp = str(cur_tp)
    if want_tp is not None and want_sl is None and cur_sl is not None and not clear_sl:
        want_sl = str(cur_sl)

    if want_sl is None and want_tp is None:
        return {"ok": True, "skipped": "no changes"}

    payload = {
        "category": "linear",
        "symbol": symbol,
        "positionIdx": idx,
        "tpslMode": "Full",
        "slTriggerBy": "LastPrice",
        "tpTriggerBy": "LastPrice",
    }
    if want_sl is not None:
        payload["stopLoss"] = want_sl
    if want_tp is not None:
        payload["takeProfit"] = want_tp

    session.set_trading_stop(**payload)

    if want_sl == "0":
        last_sl_value = None
        send_to_discord(f"🧹 Kasuję SL dla {symbol}")
    elif isinstance(want_sl, str):
        last_sl_value = float(want_sl)
        last_sl_set_ts = time.time()
        send_to_discord(f"🛡️ Ustawiam SL @ {want_sl} dla {symbol}")

    if want_tp == "0":
        last_tp_value = None
        send_to_discord(f"🧹 Kasuję TP dla {symbol}")
    elif isinstance(want_tp, str):
        last_tp_value = float(want_tp)
        last_tp_set_ts = time.time()
        send_to_discord(f"🎯 Ustawiam TP @ {want_tp} dla {symbol}")

    return {"ok": True}
except Exception as e:
    send_to_discord(f"❗ Błąd set_tp_sl_safe: {e}")
    return {"error": str(e)}
```

# ====================== ROUTES ======================

@app.get("/")
def index():
return "✅ Bot działa!", 200

@app.post("/webhook")
def webhook():
global processing, last_close_ts, manual_sl_locked, manual_tp_locked
if processing:
return "Processing in progress", 429

```
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
    reason = str(data.get("reason", "")).lower().strip()
    symbol_raw = str(data.get("symbol", SYMBOL)).strip() or SYMBOL
    symbol = normalize_symbol(symbol_raw)

    if symbol not in ALLOWED_SET:
        send_to_discord(f"🚫 Niedozwolony symbol: {symbol_raw} (po normalizacji: {symbol}).")
        processing = False
        return jsonify(error="symbol not allowed"), 400

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

    size, side = get_current_position(symbol)

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

        if reason == "atr_low":
            send_to_discord(f"🧊 Zamknięcie z powodu niskiego ATR (ATR spadł poniżej progu) — pozycja {side.upper()} ({size} {symbol})")
        else:
            send_to_discord(f"🔒 Zamknięto pozycję {side.upper()} ({size} {symbol})")

        set_tp_sl_safe(symbol, side, None, None, clear_sl=True, clear_tp=True)
        manual_sl_locked = False
        manual_tp_locked = False
        processing = False
        return jsonify(ok=True), 200

    # ===== BUY / SELL =====
    if action in ("buy", "sell"):
        if size > 0 and side in ("Buy", "Sell"):
            close_side = "Sell" if side == "Buy" else "Buy"
            session.place_order(category="linear", symbol=symbol, side=close_side,
                                orderType="Market", qty=size,
                                reduceOnly=True, timeInForce="GoodTillCancel")
            send_to_discord(f"🔒 Zamknięto pozycję {side.upper()} ({size} {symbol})")
            time.sleep(1.2)

        qty = calculate_qty(symbol)
        if not qty:
            processing = False
            return "Invalid qty", 400

        side_new = "Buy" if action == "buy" else "Sell"
        session.place_order(category="linear", symbol=symbol, side=side_new,
                            orderType="Market", qty=qty,
                            timeInForce="GoodTillCancel")
        send_to_discord(f"📥 Otwarto pozycję {side_new.upper()} ({qty} {symbol})")
        set_tp_sl_safe(symbol, side_new, sl_price, tp_price)
        processing = False
        return jsonify(ok=True), 200

    processing = False
    return jsonify(ok=True), 200

except Exception as e:
    send_to_discord(f"❗ Błąd systemowy: {e}")
    processing = False
    return "Webhook error", 500
```

if **name** == "**main**":
print("🚀 Bot uruchomiony…")
print(f"✅ Dozwolone pary: {', '.join(sorted(ALLOWED_SET))}")
app.run(host="0.0.0.0", port=PORT)
