# app.py
import os
import time
import hmac
import hashlib
import uuid
import traceback
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# .env dosyasından API_KEY, API_SECRET, vs. okunur
load_dotenv()

# Logging yapılandırma
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
    # Dokümanınıza göre testnet base URL’i kontrol edin:
    MEXC_REST_BASE = "https://contract.testnet.mexc.com"  # örnek; dokümanınıza göre değiştirin
    logger.info("Testnet modu: MEXC REST base set to testnet URL.")
else:
    MEXC_REST_BASE = "https://contract.mexc.com"
    logger.info("Gerçek modda: MEXC REST base set to live URL.")

# Zaman damgası ms
def get_timestamp_ms():
    return str(int(time.time() * 1000))

# İmzalama (HMAC SHA256). Parametre dict’i alfabetik sırayla birleştirip HMAC SHA256 ile sign üretilir.
def sign_request(params: dict) -> str:
    # Parametreleri alfabetik sıraya göre birleştir:
    ordered_items = sorted(params.items(), key=lambda x: x[0])
    query = "&".join(f"{k}={v}" for k, v in ordered_items if v != "")
    # HMAC SHA256
    secret = MEXC_API_SECRET.encode()
    signature = hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()
    return signature

# MEXC futures (swap) emir açma fonksiyonu
def place_mexc_futures_order(
    symbol: str,
    side: str,
    quantity: float,
    price: float = None,
    leverage: int = DEFAULT_LEVERAGE,
):
    """
    MEXC futures/swap emir açma (market veya limit) direkt REST.
    - symbol: MEXC API’nin beklediği format, genelde "ETH_USDT" gibi CCXT market_id’den alınan string.
    - side: "Long" veya "Short"
    - quantity: pozisyon büyüklüğü (adet). MEXC API’de vol parametresi.
    - price: None ise market order, değilse limit order price.
    - leverage: Kaldıraç
    """
    # Endpoint path: dokümanınıza göre: tek emir oluşturma endpoint’i
    path = "/api/v1/private/order/submit"
    url = MEXC_REST_BASE + path

    # Zaman damgası
    timestamp = get_timestamp_ms()

    # MEXC API parametreleri: dokümanınıza göre param adlarını kesinleştirin.
    # Aşağıda yaygın örnek parametre adlandırması kullanıldı:
    #   symbol: "ETH_USDT"
    #   price: limit price; market order için price="" veya omitted
    #   vol: quantity
    #   leverage: kaldıraç
    #   side: 1=open long, 3=open short  (doküman kontrolü: bazen 2/4 close vs. farklı)
    #   openType: 1=isolated (dokümanınıza göre)
    #   positionType: 1=long,2=short? Bazı API’lerde side zaten belirtiyor, burada dokümanınıza göre side parametre ile birlikte kullanın.
    #   externalOid: benzersiz ID (UUID)
    #   timestamp, recvWindow
    #   type/orderType: 5=market, 1=limit  (bazı MEXC sürümlerinde)
    client_oid = str(uuid.uuid4())

    # side_param dokümanınıza göre:
    # Örnek: 1=open long, 3=open short
    side_lower = side.strip().lower()
    if side_lower == "long":
        side_param = 1  # open long
    elif side_lower == "short":
        side_param = 3  # open short; dokümanın close paramlarıyla karışmamasına dikkat edin
    else:
        raise ValueError(f"Unknown side: {side}")

    # openType ve positionType: dokümanınıza göre
    open_type = 1       # örnek: 1=isolated
    position_type = 1 if side_lower == "long" else 2  # bazen API’da 1=long,2=short; doküman kontrol edin

    # type/orderType: market vs limit
    # Dokümanınıza göre market order type kodu genelde 5; limit 1.
    if price is None:
        order_type = 5  # market order
    else:
        order_type = 1  # limit order

    # Parametre dict
    params = {
        "symbol": symbol,                      # örn "ETH_USDT"
        "price": str(price) if price is not None else "",  # market için boş string
        "vol": str(quantity),                  # miktar
        "side": side_param,                    # int
        "openType": open_type,                 # int
        "positionType": position_type,         # int
        "leverage": leverage,                  # int
        "externalOid": client_oid,             # benzersiz
        "type": order_type,                    # int
        "timestamp": timestamp,
        "recvWindow": 5000,
    }
    # İmzayı ekle
    signature = sign_request(params)
    params["sign"] = signature

    # Header: dokümandaki isim
    headers = {
        "Content-Type": "application/json",
        "X-MEXC-APIKEY": MEXC_API_KEY,  # veya dokümanda “ApiKey”/“apiKey” geçiyorsa ona göre değiştirin
    }

    # İstek JSON body
    body = params

    logger.info(f"Sending MEXC futures order REST: URL={url}, body={body}")
    resp = requests.post(url, json=body, headers=headers, timeout=10)
    if resp.status_code != 200:
        raise Exception(f"MEXC order API HTTP {resp.status_code}: {resp.text}")
    data = resp.json()
    # Örnek response: {"success":true,"code":"0","data":{...}} veya {"success":false,...}
    # Dokümanınıza göre success alanı veya code=="0" kontrol edin
    if not data.get("success", False) and data.get("code") != "0":
        raise Exception(f"MEXC order hata: {data}")
    # Başarılı yanıt data kısmı
    return data.get("data", data)

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

        # Suffix temizle, örn "ETHUSDT.P" -> "ETHUSDT"
        coin_raw = coin.upper().split(".")[0]
        logger.info(f"Suffix temizlendikten sonra coin_raw: {coin_raw}")

        # Symbol parse: "ETHUSDT" -> base="ETH", quote="USDT"
        if coin_raw.endswith("USDT"):
            base = coin_raw[:-4]
            quote = "USDT"
        else:
            msg = f"Symbol formatı anlaşılmadı: {coin}"
            logger.warning(msg)
            return jsonify({"error": msg}), 400

        # CCXT ile market bilgisi yüklüyse market_id tespit edilebilir; eğer CCXT’yi artık sadece balance/leverage için kullanıyorsanız:
        # Burada CCXT fetch_balance ve set_leverage kullanmak istiyorsanız exchange.load_markets() öncesi CCXT exchange örneği oluşturmalısınız.
        # Aşağıda örnek CCXT balance çekme ve leverage ayar yapılıyor. Eğer CCXT’i bütünüyle bırakmak isterseniz, balance ve leverage REST ile de yapılabilir.
        import ccxt
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
                logger.warning(f"CCXT testnet set hatası: {e}")
        # Markets yükle (bir kez startup’ta yapılabilir, buraya koymak redeploy performansını etkiler ama örnek basitliği için ekleniyor)
        try:
            exchange.load_markets()
            logger.info("CCXT markets yüklendi.")
        except Exception as e:
            logger.warning(f"CCXT load_markets hatası: {e}")

        # Swap market_id bulma: CCXT market listesinde type='swap', base ve quote eşleşen
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

        # Balance çekme CCXT ile
        try:
            if exchange.has.get("swap"):
                bal = exchange.fetch_balance({"type": "swap"})
                logger.info("fetch_balance({'type':'swap'}) kullanıldı.")
            else:
                bal = exchange.fetch_balance()
                logger.info("fetch_balance fallback kullanıldı.")
            logger.info(f"Balance raw: {bal}")
            usdt_bal = None
            if "free" in bal and "USDT" in bal["free"]:
                usdt_bal = float(bal["free"]["USDT"])
            else:
                # info içindeki availableBalance
                info = bal.get("info", {})
                if isinstance(info, dict) and isinstance(info.get("data"), list):
                    for entry in info.get("data"):
                        if entry.get("currency") == "USDT":
                            val = entry.get("availableBalance") or entry.get("availableCash") or None
                            if val is not None:
                                usdt_bal = float(val)
                                logger.info(f"Balance info.data entry kullanıldı: available={usdt_bal}")
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

        # Leverage ayarı CCXT ile
        is_long = side.strip().lower() == "long"
        try:
            params = {"openType": 1, "positionType": 1 if is_long else 2}
            exchange.set_leverage(DEFAULT_LEVERAGE, unified_symbol, params)
            logger.info(f"Leverage ayarlandı CCXT ile: {DEFAULT_LEVERAGE}x")
        except Exception as e:
            logger.warning(f"Leverage ayarlanamadı CCXT ile: {e}")

        # Qty hesaplama
        qty = (usdt_bal * RISK_RATIO * DEFAULT_LEVERAGE) / entry_price
        if qty <= 0:
            msg = f"Qty hesaplama sıfır: {qty}"
            logger.warning(msg)
            return jsonify({"error": msg}), 400
        try:
            qty = exchange.amount_to_precision(unified_symbol, qty)
        except:
            qty = round(qty, 3)
        logger.info(f"Pozisyon qty: {qty}")

        # Market order açma (REST)
        # price=None -> market, değilse limit
        try:
            open_resp = place_mexc_futures_order(
                symbol=market_id,
                side=side,
                quantity=qty,
                price=None,  # market order
                leverage=DEFAULT_LEVERAGE,
            )
            logger.info(f"Pozisyon açıldı (REST): {open_resp}")
        except Exception as e:
            msg = f"Pozisyon açma REST hatası: {e}"
            logger.error(msg)
            return jsonify({"error": msg}), 500

        # TP emri: limit order
        tp_price = entry_price * (1.004 if is_long else 0.996)
        try:
            # CCXT price precision ile formatlayın
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
            logger.info(f"TP order kondu (REST): {tp_resp}")
        except Exception as e:
            # TP emri hata verse de pozisyon açık olabilir; loglayın
            logger.error(f"TP emri REST hatası: {e}")

        return jsonify(
            {
                "status": "success",
                "market_id": market_id,
                "qty": qty,
                "open_order": open_resp,
                "tp_order": tp_resp,
            }
        ), 200

    except Exception as e:
        trace = traceback.format_exc()
        logger.exception("Webhook işlenirken beklenmedik hata")
        return jsonify({"error": str(e), "trace": trace}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Sunucu başlatılıyor, port={port}")
    app.run(host="0.0.0.0", port=port, debug=False)
