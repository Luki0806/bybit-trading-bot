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
    from config import (
        API_KEY, API_SECRET, SYMBOL, DISCORD_WEBHOOK_URL,
        TESTNET, ALLOWED_SYMBOLS, POSITION_VALUE
    )
except Exception:
    API_KEY = os.environ.get("API_KEY", "")
    API_SECRET = os.environ.get("API_SECRET", "")
    SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    TESTNET = os.environ.get("TESTNET", "true").lower() in ("1","true","yes")
    ALLOWED_SYMBOLS = [s.strip() for s in os.environ.get("ALLOWED_SYMBOLS","WIFUSDT,COAIUSDT").split(",") if s.strip()]
    POSITION_VALUE = float(os.environ.get("POSITION_VALUE","1.0"))

ALLOWED_SET = {normalize_symbol(s) for s in (ALLOWED_SYMBOLS or [])}
PORT = int(os.environ.get("PORT", 5000))

app = Flask(__name__)
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

processing = False

# ====================== POMOCNICZE ======================
def send_to_discord(message: str):
    if not DISCORD_WEBHOOK_URL:
        print(f"[Discord OFF] {message}")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"❌ Discord error: {e}")

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
        res = session.get_positions(category="linear", symbol=symbol)
        items = (res or {}).get("result", {}).get("list", []) or []
        if not items:
            return 0.0, "None", 0.0
        p = items[0]
        return float(p.get("size") or 0), p.get("side") or "None", float(p.get("entryPrice") or 0)
    except Exception as e:
        send_to_discord(f"❗ get_current_position error: {e}")
        return 0.0, "None", 0.0

def get_instrument(symbol: str):
    """Zwraca dict z filtrami lot/qty/price; None jeśli symbol niedozwolony/nie istnieje."""
    try:
        info = session.get_instruments_info(category="linear", symbol=symbol)
        lst = (info or {}).get("result", {}).get("list", []) or []
        return lst[0] if lst else None
    except Exception as e:
        send_to_discord(f"❗ get_instrument error: {e}")
        return None

def get_last_price(symbol: str) -> float:
    try:
        t = session.get_tickers(category="linear", symbol=symbol)
        lst = (t or {}).get("result", {}).get("list", []) or []
        return float(lst[0].get("lastPrice")) if lst else 0.0
    except Exception:
        return 0.0

def quantize_qty(qty: float, lot_step: float, min_qty: float) -> float:
    if lot_step <= 0:
        return qty
    steps = math.floor(qty / lot_step)
    q = steps * lot_step
    if q < min_qty:
        return 0.0
    return float(f"{q:.10f}")

# ====================== USTAWIENIE SL/TP (bez pustych requestów) ======================
def set_tp_sl_safe(symbol, sl, tp):
    try:
        if not sl and not tp:
            return  # nic nie ustawiamy
        res = session.get_positions(category="linear", symbol=symbol)
        items = res["result"]["list"]
        if not items:
            return
        idx = int(items[0]["positionIdx"])
        payload = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": idx,
            "tpslMode": "Full",
            "slTriggerBy": "LastPrice",
            "tpTriggerBy": "LastPrice"
        }
        if sl:
            payload["stopLoss"] = str(sl)
        if tp:
            payload["takeProfit"] = str(tp)
        session.set_trading_stop(**payload)
        if sl:
            send_to_discord(f"🛡️ SL @ {sl}")
        if tp:
            send_to_discord(f"🎯 TP @ {tp}")
    except Exception as e:
        send_to_discord(f"❗ set_tp_sl_safe error: {e}")

# ====================== WYLICZANIE ILOŚCI (TYLKO PROCENT) ======================
def calculate_qty(symbol: str, percent: float):
    """
    Zwraca (qty, notional_usdt).
    - ZAWSZE procent dostępnego salda (UNIFIED / USDT)
    - percent: 1.0 = 100%, 0.25 = 25%, 0.5 = 50% itd.
      Jeśli ktoś poda >1, traktujemy jako % (np. 25 = 25%).
    """
    try:
        inst = get_instrument(symbol)
        if not inst:
            send_to_discord(f"🚫 Symbol {symbol} nie jest dostępny (not whitelisted / brak kontraktu linear).")
            return None, None

        lot = inst.get("lotSizeFilter", {}) or {}
        min_qty = float(lot.get("minOrderQty", 0) or 0)
        qty_step = float(lot.get("qtyStep", 0) or 0)

        last_price = get_last_price(symbol)
        if last_price <= 0:
            send_to_discord("❗ Brak ceny rynkowej.")
            return None, None

        # saldo
        wb = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
        usdt = next((c for c in wb if c.get("coin") == "USDT"), None)
        if not usdt:
            send_to_discord("❗ Brak USDT na rachunku UNIFIED.")
            return None, None
        available = float(usdt.get("availableBalance") or usdt.get("walletBalance") or 0.0)

        # normalizacja procentu
        v = float(percent)
        if v > 1.0:
            v = v / 100.0  # 25 => 25%
        v = max(0.0, min(1.0, v))

        target_notional = available * v
        raw_qty = target_notional / last_price

        # lotStep/minQty
        qty = quantize_qty(raw_qty, qty_step, min_qty)
        if qty <= 0:
            send_to_discord("❗ Ilość po zaokrągleniu < minQty. Zlecenie pominięte.")
            return None, None

        final_notional = qty * last_price
        send_to_discord(
            f"📊 Tryb: PERCENT ({v*100:.2f}%) → {qty} {symbol} ≈ {final_notional:.2f} USDT "
            f"(avail {available:.2f} USDT)"
        )

        return qty, final_notional

    except Exception as e:
        send_to_discord(f"❗ calculate_qty error: {e}")
        return None, None

# ====================== FLASK ROUTES ======================
@app.get("/")
def index():
    return "✅ Bot działa!", 200

@app.post("/webhook")
def webhook():
    global processing
    if processing:
        return "Processing in progress", 429
    processing = True
    try:
        data = parse_incoming_json()
        if not isinstance(data, dict):
            processing = False
            return ("", 204)

        action = str(data.get("action","")).lower()
        symbol = normalize_symbol(data.get("symbol", SYMBOL))
        if symbol not in ALLOWED_SET:
            send_to_discord(f"🚫 Niedozwolony symbol: {symbol}")
            processing = False
            return jsonify(error="symbol not allowed"), 400

        sl = float(data.get("sl") or 0) or None
        tp = float(data.get("tp") or 0) or None

        # ZAWSZE TRYB PROCENTOWY Z CONFIGU
        percent = POSITION_VALUE

        size, side, entry = get_current_position(symbol)

        # ===== CLOSE =====
        if action == "close":
            if size <= 0:
                processing = False
                return ("", 204)
            last = get_last_price(symbol)
            notional = size * last
            pnl_pct = 0.0
            if entry > 0:
                pnl_pct = ((last - entry)/entry*100) if side == "Buy" else ((entry - last)/entry*100)
            close_side = "Sell" if side == "Buy" else "Buy"
            try:
                session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=close_side,
                    orderType="Market",
                    qty=size,
                    reduceOnly=True,
                    timeInForce="GoodTillCancel"
                )
            except Exception as e:
                send_to_discord(f"❗ CLOSE error: {e}")
            sign = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < 0 else "⚪"
            send_to_discord(
                f"🧯 CLOSE: {side.upper()} {size} {symbol} ≈ {notional:.2f} USDT ({sign}{pnl_pct:.2f}%)"
            )
            processing = False
            return jsonify(ok=True), 200

        # ===== BUY / SELL =====
        if action in ("buy","sell"):
            # zamknij odwrotną
            if size > 0:
                close_side = "Sell" if side == "Buy" else "Buy"
                try:
                    session.place_order(
                        category="linear",
                        symbol=symbol,
                        side=close_side,
                        orderType="Market",
                        qty=size,
                        reduceOnly=True,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"🔒 Zamknięto {side.upper()} {size} {symbol}")
                    time.sleep(1.0)
                except Exception as e:
                    send_to_discord(f"❗ Close-prev error: {e}")

            # wylicz ilość (TYLKO PROCENT)
            qty, notional = calculate_qty(symbol, percent)
            if not qty:
                processing = False
                return "Invalid qty", 400

            new_side = "Buy" if action == "buy" else "Sell"

            # pre-check instrumentu (whitelist / status)
            inst = get_instrument(symbol)
            if not inst:
                processing = False
                return jsonify(error="symbol not tradable"), 400

            try:
                session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=new_side,
                    orderType="Market",
                    qty=qty,
                    timeInForce="GoodTillCancel"
                )
            except Exception as e:
                send_to_discord(f"❗ place_order error: {e}")
                processing = False
                return "Order error", 400

            msg = f"📥 Otwarto {new_side.upper()} ({qty} {symbol}) ≈ {notional:.2f} USDT (PERCENT {percent})"
            send_to_discord(msg)

            # ustaw TP/SL jeśli są
            set_tp_sl_safe(symbol, sl, tp)

            processing = False
            return jsonify(ok=True), 200

        processing = False
        return jsonify(ok=True), 200

    except Exception as e:
        send_to_discord(f"❗ System error: {e}")
        processing = False
        return "Webhook error", 500

if __name__ == "__main__":
    print("🚀 Bot uruchomiony…")
    print(f"✅ Dozwolone pary: {', '.join(sorted(ALLOWED_SET))}")
    print(f"📈 Tryb: PERCENT, wartość: {POSITION_VALUE}")
    app.run(host="0.0.0.0", port=PORT)
