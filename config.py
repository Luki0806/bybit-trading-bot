# ======================
# 🔑 USTAWIENIA API BYBIT
# ======================
API_KEY = "dSxOzQaRzGVTZ6SiL0"
API_SECRET = "qSWZc6ca2IeOjoQQDz82EZQCvMAePkIpPQvV"

# ======================
# ⚙️ PARAMETRY BOTA
# ======================
SYMBOL = "WIFUSDT"  # Domyślny symbol
ALLOWED_SYMBOLS = ["WIFUSDT", "COAIUSDT", "ZECUSDT","ZKUSDT","NEARUSDT","STRKUSDT","TRUMPUSDT"]  # Lista dozwolonych symboli
TESTNET = False  # False = konto realne, True = testnet

# ======================
# 💬 POWIADOMIENIA
# ======================
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1396430612960772126/eO-1bSIssVJqkScx0gZmvWeVgiV6PjDYlx8--cH9KkgvKX878WQc5aKJuan8eGYEidYT"

# ======================
# 💰 DOMYŚLNY TRYB HANDLU
# ======================
# Bot używa tych wartości tylko wtedy,
# jeśli strategia NIE przekaże "mode" i "value" w webhooku.
POSITION_MODE = "PERCENT"   # "PERCENT" lub "SIZE"
POSITION_VALUE = 1.0        # 1.0 = 100% kapitału lub np. 100 = 100 sztuk w trybie SIZE

LEVERAGE = 1           # dźwignia dla kontraktów linear
AUTOSCALE_QTY = True   # automatycznie zmniejsz ilość, gdy brakuje marginu
SAFETY_MARGIN = 0.95   # nie używaj 100% dostępnego marginu
