import os
import time
import hmac
import hashlib
import uuid
import traceback
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Ortam değişkenleri
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("true", "1", "yes")
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "25"))

if not MEXC_API_KEY or not MEXC_API_SECRET:
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET eksik")

MEXC_REST_BASE = "https://contract.testnet.mexc.com" if USE_TESTNET else "https://contract.mexc.com"
server_time_delta_ms = 0

def sync_time_with_exchange():
    global server_time_delta_ms
    try:
        resp = ccxt.mexc().public_get_contract_ping()
        if resp:
            server_time = int(resp["data"])
            local_time = int(time.time() * 1000)
            server_time_delta_ms = server_time - local_time
            logger.info(f"[SENKRON] MEXC zaman farkı: {server_time_delta_ms} ms")
    except Exception as e:
        logger.warning(f"[SENKRON] Zaman senkronizasyonu başarısız: {e}")

def get_timestamp_ms():
    return str(int(time.time() * 1000) + server_time_delta_ms)

def place_mexc_futures_order(symbol, side, quantity, price=None, leverage=DEFAULT_LEVERAGE):
    """MEXC vadeli işlemler piyasasında emir gönderir (CCXT ile)."""
    exchange = ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "enableRateLimit": True
    })
    if USE_TESTNET:
        exchange.set_sandbox_mode(True)

    try:
        exchange.set_leverage(leverage, symbol)
        order_type = 'market' if price is None else 'limit'
        side_value = 'buy' if side.lower() == 'long' else 'sell'

        order = exchange.create_order(
            symbol=symbol,
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
        data = request.get_json()
        logger.info(f"[WEBHOOK] Gelen veri: {data}")

        symbol = data.get("symbol")
        side = data.get("side")
        entry_price = data.get("entry_price")

        if not symbol or not side:
            return jsonify({"error": "Eksik parametreler: symbol veya side"}), 400

        trade_symbol = symbol.upper() + "USDT"

        exchange = ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True
        })
        if USE_TESTNET:
            exchange.set_sandbox_mode(True)

        sync_time_with_exchange()

        usdt_balance = 0
        try:
            bal = exchange.fetch_balance({"type": "swap"})
            usdt_balance = bal['free'].get('USDT', 0)
            logger.info(f"[BAKIYE] USDT Vadeli Bakiye: {usdt_balance}")
        except Exception as e:
            logger.error(f"[BAKIYE] Bakiye hatası: {e}")
            return jsonify({"error": "Bakiye çekilemedi: " + str(e)}), 500

        if usdt_balance <= 0:
            return jsonify({"status": "failed", "message": "Yeterli bakiye yok"}), 400

        current_price = 0
        try:
            ticker = exchange.fetch_ticker(trade_symbol)
            current_price = ticker['last']
        except Exception as e:
            logger.error(f"[FİYAT] Fiyat çekilemedi: {e}")
            return jsonify({"error": f"Fiyat alınamadı: {e}"}), 500

        quantity = (usdt_balance * DEFAULT_LEVERAGE) / current_price
        MIN_ORDER_QUANTITY = 1.0
        if quantity < MIN_ORDER_QUANTITY:
            return jsonify({"status": "failed", "message": f"Minimum emir miktarının altında ({quantity} < {MIN_ORDER_QUANTITY})"}), 400

        quantity = float(f"{quantity:.6f}")

        sync_time_with_exchange()

        if side.lower() == "long":
            result = place_mexc_futures_order(trade_symbol, "long", quantity, price=None)
            return jsonify({"status": "success", "message": "Long emir gönderildi", "order": result}), 200
        elif side.lower() == "short":
            result = place_mexc_futures_order(trade_symbol, "short", quantity, price=None)
            return jsonify({"status": "success", "message": "Short emir gönderildi", "order": result}), 200
        else:
            return jsonify({"error": "Geçersiz taraf (side)"}), 400

    except Exception as e:
        logger.error(f"[WEBHOOK] Hata: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("Sunucu başlıyor...")
    sync_time_with_exchange()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
