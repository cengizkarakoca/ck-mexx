import os
import time
import hmac
import hashlib
import uuid
import traceback
import logging
import requests
from urllib.parse import urlencode
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("true", "1", "yes")
RISK_RATIO = float(os.getenv("RISK_RATIO", "1.0"))
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "25"))

if not MEXC_API_KEY or not MEXC_API_SECRET:
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET eksik")

MEXC_REST_BASE = "https://contract.testnet.mexc.com" if USE_TESTNET else "https://contract.mexc.com"
server_time_delta_ms = 0  # sunucu ile istemci arası fark

def sync_time_with_exchange():
    global server_time_delta_ms
    try:
        resp = requests.get(MEXC_REST_BASE + "/api/v1/contract/ping", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data:
                server_time = int(data["data"])
                local_time = int(time.time() * 1000)
                server_time_delta_ms = server_time - local_time
                logger.info(f"[SENKRON] MEXC zaman farkı: {server_time_delta_ms} ms")
    except Exception as e:
        logger.warning(f"[SENKRON] Zaman senkronizasyonu başarısız: {e}")

def get_timestamp_ms():
    return str(int(time.time() * 1000) + server_time_delta_ms)

def sign_request(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v != "")
    return hmac.new(MEXC_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def place_mexc_futures_order(symbol, side, quantity, price=None, leverage=DEFAULT_LEVERAGE):
    path = "/api/v1/private/order/submit"
    url = MEXC_REST_BASE + path

    side_param = 1 if side.lower() == "long" else 3
    open_type = 1
    position_type = 1 if side.lower() == "long" else 2
    order_type = 5 if price is None else 1 # 5 for market order, 1 for limit order

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-MEXC-APIKEY": MEXC_API_KEY
    }

    for attempt in range(1, 4):
        try:
            timestamp = get_timestamp_ms()
            external_oid = str(uuid.uuid4())
            params = {
                "symbol": symbol,
                "price": str(price or ""), # Set price to empty string for market orders
                "vol": str(quantity),
                "side": side_param,
                "openType": open_type,
                "positionType": position_type,
                "leverage": leverage,
                "externalOid": external_oid,
                "type": order_type,
                "timestamp": timestamp,
                "recvWindow": 20000 # Increased from 5000ms to 20000ms (20 seconds)
            }
            params["sign"] = sign_request(params)
            body = urlencode(params)

            logger.info(f"[ORDER] Deneme {attempt}: {body}")
            response = requests.post(url, data=body, headers=headers, timeout=30) # Increased from 15s to 30s
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}: {response.text}")
            data = response.json()
            if not data.get("success", False):
                raise Exception(f"API Hata: {data}")
            return data
        except requests.exceptions.Timeout:
            logger.warning(f"[ORDER] Zaman aşımı oldu (attempt {attempt})")
        except Exception as e:
            logger.error(f"[ORDER] Hata: {e}")
        time.sleep(1)
    raise RuntimeError("3 denemede de pozisyon açılamadı")

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/balance", methods=["GET"])
def balance():
    try:
        sync_time_with_exchange()
        exchange = ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True
        })
        if USE_TESTNET:
            exchange.set_sandbox_mode(True)
        bal = exchange.fetch_balance({"type": "swap"})
        logger.info(f"[BALANCE] {bal}")
        return jsonify({
            "USDT_swap_balance": bal['free'].get('USDT', 0),
            "raw": bal
        }), 200
    except Exception as e:
        logger.error(f"[BALANCE] Hata: {e}")
        return jsonify({"error": str(e)}), 500

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

        trade_symbol = symbol + "USDT" # Assuming the symbol from TradingView is e.g., "XRP"

        # --- DİKKAT: Miktar Hesaplama Kısmı ---
        # Burası sizin risk yönetiminize göre ayarlanması gereken en kritik kısımdır.
        # Sabit bir USDT değeri üzerinden işlem yapmak yerine, bakiyenize veya riskinize göre hesaplama yapmalısınız.

        trade_amount_usd = 10 # Örnek: Sabit 10 USDT değerinde bir işlem

        # Eğer piyasa emri veriyorsak (price=None), quantity olarak doğrudan almak istediğimiz coin miktarını vermeliyiz.
        # Bu miktarı, o anki piyasa fiyatını çekerek trade_amount_usd'ye bölebiliriz.
        # Basitlik adına, şu an için entry_price'ı kullanarak yaklaşık bir hesaplama yapıyoruz.
        # CANLI İŞLEM İÇİN, buraya CCXT ile güncel piyasa fiyatını çeken bir mekanizma eklemeniz şiddetle tavsiye edilir.
        
        # Örneğin, aşağıdaki gibi güncel fiyatı çekebilirsiniz (ccxt import edilmişti):
        # try:
        #     exchange = ccxt.mexc({
        #         "apiKey": MEXC_API_KEY,
        #         "secret": MEXC_API_SECRET,
        #         "enableRateLimit": True
        #     })
        #     if USE_TESTNET:
        #         exchange.set_sandbox_mode(True)
        #     ticker = exchange.fetch_ticker(trade_symbol)
        #     current_price = ticker['last']
        #     quantity = trade_amount_usd / current_price
        # except Exception as e:
        #     logger.error(f"Fiyat çekme hatası: {e}")
        #     quantity = 20 # Hata durumunda varsayılan miktar
        
        # Mevcut kodda, giriş fiyatını kullanarak basit bir tahmin yapıyoruz:
        quantity = trade_amount_usd / float(entry_price) if entry_price and float(entry_price) > 0 else 20 # Eğer price yoksa veya 0 ise varsayılan miktar
        # ------------------------------------

        if side.lower() == "long":
            order_result = place_mexc_futures_order(trade_symbol, "long", quantity, price=None) # Piyasa emri için price=None
            logger.info(f"[ORDER] Long sipariş başarıyla gönderildi: {order_result}")
            return jsonify({"status": "success", "message": "Long sipariş gönderildi", "order": order_result}), 200
        elif side.lower() == "short":
            order_result = place_mexc_futures_order(trade_symbol, "short", quantity, price=None) # Piyasa emri için price=None
            logger.info(f"[ORDER] Short sipariş başarıyla gönderildi: {order_result}")
            return jsonify({"status": "success", "message": "Short sipariş gönderildi", "order": order_result}), 200
        else:
            return jsonify({"error": "Geçersiz side değeri"}), 400

    except Exception as e:
        logger.error(f"[WEBHOOK] İşlem hatası: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("Sunucu başlıyor...")
    sync_time_with_exchange()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
