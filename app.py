import os
import time
import hmac
import hashlib
import uuid
import requests
import logging
from threading import Thread
from urllib.parse import urlencode
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

API_KEY = os.getenv("MEXC_API_KEY")
API_SECRET = os.getenv("MEXC_API_SECRET")
BASE_URL = "https://contract.mexc.com"

def get_timestamp():
    return str(int(time.time() * 1000))

def sign(params: dict):
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v != "")
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def place_order(symbol, side, qty, leverage=20):
    url = f"{BASE_URL}/api/v1/private/order/submit"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-MEXC-APIKEY": API_KEY
    }

    order_type = 5  # market order
    side_param = 1 if side == "long" else 3

    params = {
        "symbol": symbol,
        "price": "",  # market order
        "vol": str(qty),
        "side": side_param,
        "openType": 1,
        "positionType": 1 if side == "long" else 2,
        "leverage": leverage,
        "externalOid": str(uuid.uuid4()),
        "type": order_type,
        "timestamp": get_timestamp(),
        "recvWindow": 30000
    }
    params["sign"] = sign(params)
    data = urlencode(params)

    try:
        resp = requests.post(url, headers=headers, data=data, timeout=20)
        logger.info(f"[ORDER] Yanıt: {resp.status_code} - {resp.text}")
    except requests.exceptions.Timeout:
        logger.warning("[ORDER] Timeout hatası")
    except Exception as e:
        logger.error(f"[ORDER] Hata: {e}")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    logger.info(f"[WEBHOOK] Veri geldi: {data}")

    symbol = data.get("symbol", "").upper() + "USDT"
    side = data.get("side", "").lower()
    qty = float(data.get("qty", 1))

    Thread(target=place_order, args=(symbol, side, qty)).start()
    return jsonify({"status": "ok", "message": "İşlem başlatıldı"}), 200

@app.route("/")
def index():
    return "✅ Bot çalışıyor"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
