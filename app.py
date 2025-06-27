import os
import traceback
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("true", "1", "yes")
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "25"))

if not MEXC_API_KEY or not MEXC_API_SECRET:
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET eksik")

def normalize_symbol(symbol, exchange):
    symbol = symbol.upper()
    exchange.load_markets()

    swap_symbols = [
        s for s in exchange.symbols
        if exchange.market(s).get('type') == 'swap'
    ]
    logger.info(f"[DEBUG] {len(swap_symbols)} swap sembol bulundu.")

    candidates = [
        symbol + "USDT",
        symbol + "USD",
        symbol + "_USDT",
        symbol + "/USDT:USDT",
        symbol + "/USDT:SWAP",
        symbol + "/USDT",
        symbol + "_USDT:USDT",
        symbol + "_USDT:SWAP"
    ]

    matches = [s for s in candidates if s in swap_symbols]
    logger.info(f"[MATCHES] Denenen semboller: {candidates}")
    if matches:
        logger.info(f"[SYMBOL] Kullanılan sembol: {matches[0]}")
        return matches[0]

    raise ValueError(f"[SYMBOL] Sembol bulunamadı: {candidates}")

def place_mexc_futures_order(symbol, side, quantity, price=None, leverage=DEFAULT_LEVERAGE):
    exchange = ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "enableRateLimit": True,
    })
    if USE_TESTNET:
        exchange.set_sandbox_mode(True)

    exchange.load_markets()
    normalized_symbol = normalize_symbol(symbol, exchange)

    try:
        exchange.set_leverage(leverage, normalized_symbol, {
            "openType": 1,
            "positionType": 1 if side.lower() == "long" else 2
        })

        order_type = 'market' if price is None else 'limit'
        side_value = 'buy' if side.lower() == 'long' else 'sell'

        order = exchange.create_order(
            symbol=normalized_symbol,
            type=order_type,
            side=side_value,
            amount=quantity,
            price=price,
            params={"type": "swap"}
        )

        logger.info(f"[CCXT ORDER] Emir başarıyla gönderildi: {order}")
        return order

    except Exception as e:
        logger.error(f"[CCXT ORDER] Emir gönderilirken hata oluştu: {e}")
        raise RuntimeError(f"Emir gönderilemedi: {str(e)}")

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def mexc_webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Geçersiz veya boş JSON"}), 400

        logger.info(f"[WEBHOOK] Gelen veri: {data}")

        symbol = data.get("symbol")
        side = data.get("side")
        entry_price = data.get("entry_price")

        if not symbol or not side:
            return jsonify({"error": "Eksik parametreler: symbol veya side"}), 400

        exchange = ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True,
        })
        if USE_TESTNET:
            exchange.set_sandbox_mode(True)

        exchange.load_markets()

        try:
            trade_symbol = normalize_symbol(symbol, exchange)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

        balance = exchange.fetch_balance({"type": "swap"})
        usdt_balance = balance['free'].get('USDT', 0)
        logger.info(f"[BAKIYE] USDT Vadeli Bakiye: {usdt_balance}")

        if usdt_balance <= 0:
            return jsonify({"status": "failed", "message": "Yeterli bakiye yok"}), 400

        ticker = exchange.fetch_ticker(trade_symbol)
        current_price = ticker['last']

        quantity = (usdt_balance * DEFAULT_LEVERAGE) / current_price
        MIN_ORDER_QUANTITY = 1.0
        if quantity < MIN_ORDER_QUANTITY:
            return jsonify({
                "status": "failed",
                "message": f"Minimum emir miktarının altında ({quantity:.6f} < {MIN_ORDER_QUANTITY})"
            }), 400

        quantity = float(f"{quantity:.6f}")

        result = place_mexc_futures_order(trade_symbol, side.lower(), quantity, price=None)
        return jsonify({"status": "success", "message": f"{side} emir gönderildi", "order": result}), 200

    except Exception as e:
        logger.error(f"[WEBHOOK] Hata: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("Sunucu başlıyor...")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
