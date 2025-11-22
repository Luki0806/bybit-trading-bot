import os
import time
import math
import json
import requests
import threading
from queue import Queue
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP
from collections import defaultdict

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
    TESTNET = os.environ.get("TESTNET", "true").lower() in ("1", "true", "yes")
    ALLOWED_SYMBOLS = [
        s.strip() for s in os.environ.get("ALLOWED_SYMBOLS", "WIFUSDT,COAIUSDT").split(",")
        if s.strip()
    ]
    POSITION_VALUE = float(os.environ.get("POSITION_VALUE", "1.0"))

ALLOWED_SET = {normalize_symbol(s) for s in (ALLOWED_SYMBOLS or [])}
PORT = int(os.environ.get("PORT", 5000))

# 🔒 ANTY-FLIP / ANTY-DUPLIKAT – KONFIG
MIN_SECONDS_BETWEEN_SAME_ACTION = float(os.environ.get("MIN_SECONDS_BETWEEN_SAME_ACTION", "0.5"))
MIN_HOLD_SECONDS_AFTER_OPEN = float(os.environ.get("MIN_HOLD_SECONDS_AFTER_OPEN", "3.0"))

app = Flask(__name__)
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, testnet=TESTNET)

# ====================== KOLEJKA ZDARZEŃ ======================
event_queue: Queue = Queue()

# ====================== STAN WEWNĘTRZNY (ANTY-FLIP) ======================
# kiedy ostatnio OTWORZYLIŚMY pozycję na danym symbolu
last_open_time = {}              # symbol -> timestamp
# ostatnia akcja tego samego typu na symbolu (do anty-duplikatu)
last_action_time = {}            # (symbol, action) -> timestamp

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
        if sl is None and tp is None:
            return

        res = session.get_positions(category="linear", symbol=symbol)
        items = (res or {}).get("result", {}).get("list", []) or []
        if not items:
            return

        idx = int(items[0].get("positionIdx", 0))
        payload = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": idx,
            "tpslMode": "Full",
            "slTriggerBy": "LastPrice",
            "tpTriggerBy": "LastPrice"
        }

        if sl is not None:
            payload["stopLoss"] = str(sl)
        if tp is not None:
            payload["takeProfit"] = str(tp)

        if "stopLoss" in payload or "takeProfit" in payload:
            session.set_trading_stop(**payload)

        if sl is not None:
            send_to_discord(f"🛡️ SL @ {sl}")
        if tp is not None:
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

        wb = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
        usdt = next((c for c in wb if c.get("coin") == "USDT"), None)
        if not usdt:
            send_to_discord("❗ Brak USDT na rachunku UNIFIED.")
            return None, None
        available = float(usdt.get("availableBalance") or usdt.get("walletBalance") or 0.0)

        v = float(percent)
        if v > 1.0:
            v = v / 100.0
        v = max(0.0, min(1.0, v))

        target_notional = available * v
        raw_qty = target_notional / last_price

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

# ====================== GŁÓWNA LOGIKA PRZETWARZANIA ALERTU ======================
def process_event(data: dict):
    if not isinstance(data, dict):
        return

    action = str(data.get("action", "")).lower()
    symbol = normalize_symbol(data.get("symbol", SYMBOL))

    if symbol not in ALLOWED_SET:
        send_to_discord(f"🚫 Niedozwolony symbol: {symbol}")
        return

    sl = data.get("sl", None)
    tp = data.get("tp", None)

    try:
        sl = float(sl) if sl is not None else None
    except Exception:
        sl = None

    try:
        tp = float(tp) if tp is not None else None
    except Exception:
        tp = None

    # 🔒 ANTY-DUPLIKAT TEJ SAMEJ AKCJI W KRÓTKIM CZASIE
    now = time.time()
    key = (symbol, action)
    last_ts = last_action_time.get(key)
    if last_ts is not None and now - last_ts < MIN_SECONDS_BETWEEN_SAME_ACTION:
        send_to_discord(
            f"⏱️ Zignorowano duplikat akcji {action.upper()} dla {symbol} "
            f"({now - last_ts:.2f}s od poprzedniej)."
        )
        return
    last_action_time[key] = now

    percent = POSITION_VALUE
    size, side, entry = get_current_position(symbol)

    # ===== CLOSE =====
    if action == "close":
        # 🔒 ANTY-FLIP: nie zamykaj pozycji natychmiast po otwarciu
        open_ts = last_open_time.get(symbol)
        if open_ts is not None and now - open_ts < MIN_HOLD_SECONDS_AFTER_OPEN:
            send_to_discord(
                f"⏱️ CLOSE dla {symbol} odrzucony ({now - open_ts:.2f}s po OTWARCIU) – anty-flip."
            )
            return

        if size <= 0:
            return

        last = get_last_price(symbol)
        notional = size * last
        pnl_pct = 0.0
        if entry > 0:
            if side == "Buy":
                pnl_pct = (last - entry) / entry * 100
            else:
                pnl_pct = (entry - last) / entry * 100

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
        return

    # ===== BUY / SELL =====
    if action in ("buy", "sell"):
        # 🔒 JEŚLI JUŻ JESTEŚMY W TEJ SAMEJ POZYCJI – NIE RÓB NIC WIĘCEJ
        if size > 0 and (
            (side == "Buy" and action == "buy") or
            (side == "Sell" and action == "sell")
        ):
            send_to_discord(
                f"ℹ️ Już w pozycji {side.upper()} na {symbol} – "
                f"pomijam nowe OTWARCIE, ewentualnie aktualizuję TP/SL."
            )
            set_tp_sl_safe(symbol, sl, tp)
            return

        # zamknij odwrotną pozycję (jeśli jest)
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
            return

        new_side = "Buy" if action == "buy" else "Sell"

        inst = get_instrument(symbol)
        if not inst:
            send_to_discord("🚫 Symbol nie jest tradowalny (brak instrumentu).")
            return

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
            return

        msg = (
            f"📥 Otwarto {new_side.upper()} ({qty} {symbol}) ≈ {notional:.2f} USDT "
            f"(PERCENT {percent})"
        )
        send_to_discord(msg)

        # zapamiętujemy czas OTWARCIA – do anty-flip CLOSE
        last_open_time[symbol] = time.time()

        # ustaw TP/SL jeśli są
        set_tp_sl_safe(symbol, sl, tp)
        return

    # inne akcje ignorujemy (na razie)
    send_to_discord(f"ℹ️ Nieznana akcja: {action}")

# ====================== WORKER KOLEJKI ======================
def worker():
    while True:
        event = event_queue.get()
        try:
            process_event(event)
        except Exception as e:
            send_to_discord(f"❗ Worker error: {e}")
        finally:
            event_queue.task_done()

threading.Thread(target=worker, daemon=True).start()

# ====================== FLASK ROUTES ======================
@app.get("/")
def index():
    return "✅ Bot działa!", 200

@app.post("/webhook")
def webhook():
    data = parse_incoming_json()
    if not isinstance(data, dict):
        return ("", 204)

    event_queue.put(data)
    return jsonify(ok=True), 200

if __name__ == "__main__":
    print("🚀 Bot uruchomiony…")
    print(f"✅ Dozwolone pary: {', '.join(sorted(ALLOWED_SET))}")
    print(f"📈 Tryb: PERCENT, wartość: {POSITION_VALUE}")
    print(f"⏱️ Anti-dup: {MIN_SECONDS_BETWEEN_SAME_ACTION}s, Anti-flip CLOSE: {MIN_HOLD_SECONDS_AFTER_OPEN}s")
    app.run(host="0.0.0.0", port=PORT)
