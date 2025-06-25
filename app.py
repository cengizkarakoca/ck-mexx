# app.py

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

# .env’den değişkenleri yükle
load_dotenv()

# Logging yapılandırması
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Ortam değişkenleri
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("true", "1", "yes")
try:
    RISK_RATIO = float(os.getenv("RISK_RATIO", "1.0"))
except:
    RISK_RATIO = 1.0
try:
    DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "25"))
except:
    DEFAULT_LEVERAGE = 25

if not MEXC_API_KEY or not MEXC_API_SECRET:
    logger.error("MEXC_API_KEY veya MEXC_API_SECRET tanımlı değil.")
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET tanımlı değil.")

# Base URL: canlı veya testnet
if USE_TESTNET:
    # Dokümanınıza göre testnet URL’sini doğrulayın
    MEXC_REST_BASE = "https://contract.testnet.mexc.com"
    logger.info("Testnet modu: MEXC REST base testnet URL olarak ayarlandı.")
else:
    MEXC_REST_BASE = "https://contract.mexc.com"
    logger.info("Gerçek modda: MEXC REST base live URL olarak ayarlandı.")

def get_timestamp_ms():
    return str(int(time.time() * 1000))

def sign_request(params: dict) -> str:
    """
    HMAC SHA256 imzalama. Parametre dict’ini alfabetik sıraya göre birleştir ve imzala.
    MEXC API dokümanına göre form-encoded veya JSON body kullanacaksanız ona göre urlencode yapıp imzayı üretin.
    """
    ordered_items = sorted(params.items(), key=lambda x: x[0])
    # boş değerleri atarak birleştiriyoruz
    query = "&".join(f"{k}={v}" for k, v in ordered_items if v != "")
    signature = hmac.new(MEXC_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return signature

def place_mexc_futures_order(symbol: str, side: str, quantity: float, price: float=None, leverage: int=DEFAULT_LEVERAGE):
    """
    MEXC futures/swap emir açma (market veya limit) direkt REST çağrısıyla.
    - symbol: MEXC API’nin beklediği format, genelde "ETH_USDT" (CCXT market_id’den alınan string).
    - side: "Long" veya "Short"
    - quantity: pozisyon büyüklüğü (adet)
    - price: None ise market order, değilse limit order price
    - leverage: kaldıraç
    """
    path = "/api/v1/private/order/submit"
    url = MEXC_REST_BASE + path

    timestamp = get_timestamp_ms()
    client_oid = str(uuid.uuid4())
    side_lower = side.strip().lower()
    # Dokümanınıza göre side kodunu kontrol edin: örneğin 1=open long, 3=open short
    if side_lower == "long":
        side_param = 1
    else:
        side_param = 3
    open_type = 1  # isolated; dokümana göre değiştirilebilir
    position_type = 1 if side_lower == "long" else 2
    # Order type: market vs limit; dokümana göre kod(örneğin 5=market,1=limit)
    order_type = 5 if price is None else 1

    params = {
        "symbol": symbol,                      # örn "ETH_USDT"
        "price": str(price) if price is not None else "",
        "vol": str(quantity),
        "side": side_param,
        "openType": open_type,
        "positionType": position_type,
        "leverage": leverage,
        "externalOid": client_oid,
        "type": order_type,
        "timestamp": timestamp,
        "recvWindow": 5000,
    }
    params["sign"] = sign_request(params)

    # Header: dokümana göre tam adı kullanın
    # Örneğin:
    headers = {
        "Content-Type": "application/json",
        "X-MEXC-APIKEY": MEXC_API_KEY,
    }
    # Eğer doküman form-encoded gerektiriyorsa:
    # headers = {
    #     "Content-Type": "application/x-www-form-urlencoded",
    #     "X-MEXC-APIKEY": MEXC_API_KEY,
    # }

    # Retry & timeout mantığı
    retries = 3
    backoff = 1
    for attempt in range(1, retries+1):
        try:
            logger.info(f"MEXC REST order denemesi (attempt {attempt}): URL={url}, params={params}")
            # JSON body kullanıyorsanız:
            resp = requests.post(url, json=params, headers=headers, timeout=30)
            # Eğer form-encoded body denenecekse:
            # body = urlencode(params)
            # resp = requests.post(url, data=body, headers=headers, timeout=30)
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}: {resp.text}")
            data = resp.json()
            # Dokümana göre success kontrolü
            if not data.get("success", False) and data.get("code") != "0":
                raise Exception(f"MEXC order hata: {data}")
            return data.get("data", data)
        except requests.exceptions.Timeout as te:
            logger.warning(f"Timeout oldu MEXC REST order (attempt {attempt}): {te}")
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            else:
                raise
        except Exception as e:
            logger.warning(f"MEXC REST order hatası (attempt {attempt}): {e}")
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            else:
                raise

# Flask app
app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        logger.info(f"[WEBHOOK] Alındı: {data}")

        coin = data.get("symbol")
        side = data.get("side")
        entry_price = data.get("entry_price")
        if not all([coin, side, entry_price]):
            msg = "Eksik veri: symbol/side/entry_price"
            logger.warning(msg + f" | data: {data}")
            return jsonify({"error": msg}), 400
        try:
            entry_price = float(entry_price)
        except:
            msg = "entry_price float formatında değil"
            logger.warning(msg + f" | entry_price: {entry_price}")
            return jsonify({"error": msg}), 400

        # Örn "ETHUSDT.P" -> "ETHUSDT"
        coin_raw = coin.upper().split(".")[0]
        logger.info(f"Suffix temizlendikten sonra coin_raw: {coin_raw}")
        if not coin_raw.endswith("USDT"):
            msg = f"Symbol formatı anlaşılmadı: {coin}"
            logger.warning(msg)
            return jsonify({"error": msg}), 400
        base = coin_raw[:-4]
        quote = "USDT"

        # CCXT exchange örneği (balance ve precision/leverage için)
        exchange = ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True,
        })
        if USE_TESTNET:
            try:
                exchange.set_sandbox_mode(True)
                logger.info("CCXT testnet modu etkin.")
            except Exception as e:
                logger.warning(f"CCXT sandbox hatası: {e}")
        try:
            exchange.load_markets()
            logger.info("CCXT markets yüklendi.")
        except Exception as e:
            logger.warning(f"CCXT load_markets hatası: {e}")

        # Swap market bulma
        unified_symbol = None
        market_id = None
        for m, market in exchange.markets.items():
            if market.get("type") == "swap" and market.get("base") == base and market.get("quote") == quote:
                unified_symbol = m
                market_id = market.get("id")  # örn "ETH_USDT"
                logger.info(f"Found swap market via CCXT: unified_symbol={m}, market_id={market_id}")
                break
        if not market_id:
            msg = f"Swap market bulunamadı: {base}/{quote}"
            logger.warning(msg)
            return jsonify({"error": msg}), 400

        # Bakiye çekme
        try:
            if exchange.has.get("swap"):
                bal = exchange.fetch_balance({"type": "swap"})
                logger.info("fetch_balance({'type':'swap'}) kullanıldı.")
            else:
                bal = exchange.fetch_balance()
                logger.info("fetch_balance fallback kullanıldı.")
            usdt_bal = None
            if "free" in bal and "USDT" in bal["free"]:
                usdt_bal = float(bal["free"]["USDT"])
            else:
                info = bal.get("info", {})
                if isinstance(info, dict) and isinstance(info.get("data"), list):
                    for entry in info.get("data"):
                        if entry.get("currency") == "USDT":
                            val = entry.get("availableBalance") or entry.get("availableCash") or None
                            if val is not None:
                                usdt_bal = float(val)
                            break
            if usdt_bal is None or usdt_bal <= 0:
                msg = f"Yetersiz bakiye: {usdt_bal}"
                logger.warning(msg)
                return jsonify({"error": msg}), 400
        except Exception as e:
            msg = f"Bakiye alınamadı: {e}"
            logger.error(msg)
            return jsonify({"error": msg}), 500
        logger.info(f"USDT bakiyesi: {usdt_bal}")

        # Leverage ayarı (isteğe bağlı, CCXT ile)
        is_long = side.strip().lower() == "long"
        try:
            params_lever = {"openType": 1, "positionType": 1 if is_long else 2}
            exchange.set_leverage(DEFAULT_LEVERAGE, unified_symbol, params_lever)
            logger.info(f"Leverage ayarlandı CCXT ile: {DEFAULT_LEVERAGE}x")
        except Exception as e:
            logger.warning(f"Leverage ayarlanamadı CCXT ile: {e}")

        # Qty hesaplama
        qty = (usdt_bal * RISK_RATIO * DEFAULT_LEVERAGE) / entry_price
        if qty <= 0:
            msg = f"Qty hesaplama sıfır veya negatif: {qty}"
            logger.warning(msg)
            return jsonify({"error": msg}), 400
        try:
            qty = exchange.amount_to_precision(unified_symbol, qty)
        except:
            qty = round(qty, 3)
        logger.info(f"Pozisyon qty: {qty}")

        # Market order açma (REST)
        try:
            open_resp = place_mexc_futures_order(
                symbol=market_id,
                side=side,
                quantity=qty,
                price=None,
                leverage=DEFAULT_LEVERAGE,
            )
            logger.info(f"Pozisyon açıldı (REST): {open_resp}")
        except Exception as e:
            msg = f"Pozisyon açma REST hatası: {e}"
            logger.error(msg)
            return jsonify({"error": msg}), 500

        # TP emri (limit)
        tp_price = entry_price * (1.004 if is_long else 0.996)
        try:
            tp_price = exchange.price_to_precision(unified_symbol, tp_price)
        except:
            tp_price = round(tp_price, 2)
        logger.info(f"TP fiyatı: {tp_price}")

        tp_resp = None
        try:
            tp_resp = place_mexc_futures_order(
                symbol=market_id,
                side="Short" if is_long else "Long",
                quantity=qty,
                price=tp_price,
                leverage=DEFAULT_LEVERAGE,
            )
            logger.info(f"TP emri kondu (REST): {tp_resp}")
        except Exception as e:
            logger.error(f"TP emri REST hatası: {e}")

        return jsonify({
            "status": "success",
            "market_id": market_id,
            "qty": qty,
            "open_order": open_resp,
            "tp_order": tp_resp,
        }), 200

    except Exception as e:
        trace = traceback.format_exc()
        logger.exception("Webhook işlenirken beklenmedik hata")
        return jsonify({"error": str(e), "trace": trace}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Sunucu başlatılıyor, port={port}")
    app.run(host="0.0.0.0", port=port, debug=False)
