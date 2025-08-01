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

        if action not in ["buy", "sell", "close_buy", "close_sell"]:
            send_to_discord(f"⚠️ Nieprawidłowe polecenie: '{action}'. Dozwolone: buy, sell, close_buy, close_sell.")
            processing = False
            return "Invalid action", 400

        position_size, position_side = get_current_position(SYMBOL)

        # Zamknięcie tylko long
        if action == "close_buy" and position_size > 0 and position_side == "Buy":
            try:
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side="Sell",
                    orderType="Market",
                    qty=position_size,
                    reduceOnly=True,
                    timeInForce="GoodTillCancel"
                )
                send_to_discord(f"🔒 Zamknięto pozycję LONG ({position_size} {SYMBOL})")
            except Exception as e:
                send_to_discord(f"❗ Błąd przy zamykaniu LONG: {e}")

            processing = False
            return "Closed long", 200

        # Zamknięcie tylko short
        if action == "close_sell" and position_size > 0 and position_side == "Sell":
            try:
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side="Buy",
                    orderType="Market",
                    qty=position_size,
                    reduceOnly=True,
                    timeInForce="GoodTillCancel"
                )
                send_to_discord(f"🔒 Zamknięto pozycję SHORT ({position_size} {SYMBOL})")
            except Exception as e:
                send_to_discord(f"❗ Błąd przy zamykaniu SHORT: {e}")

            processing = False
            return "Closed short", 200

        # Otwórz pozycję BUY
        if action == "buy":
            if position_size > 0 and position_side == "Buy":
                send_to_discord("ℹ️ Pozycja LONG już otwarta — brak akcji.")
                processing = False
                return "Already long", 200

            # Zamknij short jeśli otwarty
            if position_size > 0 and position_side == "Sell":
                try:
                    session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Buy",
                        orderType="Market",
                        qty=position_size,
                        reduceOnly=True,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"🔄 Zamknięto SHORT przed otwarciem LONG ({position_size} {SYMBOL})")
                    time.sleep(1.5)
                except Exception as e:
                    send_to_discord(f"❗ Błąd zamykania SHORT: {e}")

            qty = calculate_qty(SYMBOL)
            if qty:
                try:
                    session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Buy",
                        orderType="Market",
                        qty=qty,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"📥 Otwarto pozycję LONG ({qty} {SYMBOL})")
                except Exception as e:
                    send_to_discord(f"❗ Błąd otwierania LONG: {e}")

            processing = False
            return "Long opened", 200

        # Otwórz pozycję SELL
        if action == "sell":
            if position_size > 0 and position_side == "Sell":
                send_to_discord("ℹ️ Pozycja SHORT już otwarta — brak akcji.")
                processing = False
                return "Already short", 200

            # Zamknij long jeśli otwarty
            if position_size > 0 and position_side == "Buy":
                try:
                    session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Sell",
                        orderType="Market",
                        qty=position_size,
                        reduceOnly=True,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"🔄 Zamknięto LONG przed otwarciem SHORT ({position_size} {SYMBOL})")
                    time.sleep(1.5)
                except Exception as e:
                    send_to_discord(f"❗ Błąd zamykania LONG: {e}")

            qty = calculate_qty(SYMBOL)
            if qty:
                try:
                    session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side="Sell",
                        orderType="Market",
                        qty=qty,
                        timeInForce="GoodTillCancel"
                    )
                    send_to_discord(f"📥 Otwarto pozycję SHORT ({qty} {SYMBOL})")
                except Exception as e:
                    send_to_discord(f"❗ Błąd otwierania SHORT: {e}")

            processing = False
            return "Short opened", 200

    except Exception as e:
        send_to_discord(f"❗ Błąd systemowy: {e}")
        processing = False
        return "Webhook error", 500
