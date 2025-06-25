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
import ccxt # ccxt kütüphanesini import ediyoruz

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Çevre değişkenleri
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("true", "1", "yes")
RISK_RATIO = float(os.getenv("RISK_RATIO", "1.0")) # Şu an kullanılmıyor ama kalsın
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "25")) # Varsayılan kaldıraç

if not MEXC_API_KEY or not MEXC_API_SECRET:
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET eksik")

# MEXC API temel URL'si (testnet veya ana ağa göre değişir)
MEXC_REST_BASE = "https://contract.testnet.mexc.com" if USE_TESTNET else "https://contract.mexc.com"
server_time_delta_ms = 0  # MEXC sunucusu ile istemci arası zaman farkı

def sync_time_with_exchange():
    """MEXC sunucu zamanını senkronize eder."""
    global server_time_delta_ms
    try:
        # MEXC'nin ping endpoint'ine istek göndererek sunucu zamanını alırız.
        # Zaman aşımı 10 saniye olarak ayarlandı.
        resp = requests.get(MEXC_REST_BASE + "/api/v1/contract/ping", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data:
                server_time = int(data["data"])
                local_time = int(time.time() * 1000)
                server_time_delta_ms = server_time - local_time
                logger.info(f"[SENKRON] MEXC zaman farkı: {server_time_delta_ms} ms")
        else:
            logger.warning(f"[SENKRON] Ping isteği başarısız oldu: HTTP {resp.status_code}: {resp.text}")
    except requests.exceptions.Timeout:
        logger.warning(f"[SENKRON] Zaman senkronizasyonu zaman aşımı yaşadı.")
    except Exception as e:
        logger.warning(f"[SENKRON] Zaman senkronizasyonu başarısız: {e}")

def get_timestamp_ms():
    """Senkronize edilmiş zaman damgasını milisaniye cinsinden döndürür."""
    return str(int(time.time() * 1000) + server_time_delta_ms)

def sign_request(params: dict) -> str:
    """MEXC API isteğini imzalar."""
    # Parametreleri alfabetik sıraya göre sıralar ve birleştirir.
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v != "")
    # HMAC SHA256 kullanarak isteği imzalar.
    return hmac.new(MEXC_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def place_mexc_futures_order(symbol, side, quantity, price=None, leverage=DEFAULT_LEVERAGE):
    """MEXC vadeli işlemler piyasasında emir gönderir."""
    path = "/api/v1/private/order/submit"
    url = MEXC_REST_BASE + path

    # Emir tarafı (long: 1, short: 3)
    side_param = 1 if side.lower() == "long" else 3
    open_type = 1 # Emir açılış tipi (1: açılış, 2: kapanış)
    position_type = 1 if side.lower() == "long" else 2 # Pozisyon tipi (long: 1, short: 2)
    order_type = 5 if price is None else 1 # Emir tipi (5: piyasa emri, 1: limit emri)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-MEXC-APIKEY": MEXC_API_KEY
    }

    # 3 deneme yaparak emir göndermeyi dener
    for attempt in range(1, 4):
        try:
            timestamp = get_timestamp_ms()
            external_oid = str(uuid.uuid4()) # Benzersiz harici emir ID'si
            params = {
                "symbol": symbol,
                "price": str(price or ""), # Piyasa emri için boş string
                "vol": str(quantity),
                "side": side_param,
                "openType": open_type,
                "positionType": position_type,
                "leverage": leverage,
                "externalOid": external_oid,
                "type": order_type,
                "timestamp": timestamp,
                "recvWindow": 30000 # Sunucu tarafında isteğin geçerli kalacağı süre (30 saniye)
            }
            params["sign"] = sign_request(params) # İsteği imzala
            body = urlencode(params) # Parametreleri URL formatına dönüştür

            logger.info(f"[ORDER] Deneme {attempt}: {body}")
            # MEXC API'ye POST isteği gönderir. Zaman aşımı 45 saniyeye çıkarıldı.
            response = requests.post(url, data=body, headers=headers, timeout=45)
            if response.status_code != 200:
                # HTTP hatası durumunda istisna fırlatır
                raise Exception(f"HTTP {response.status_code}: {response.text}")
            data = response.json()
            if not data.get("success", False):
                # API tarafından dönen hata durumunda istisna fırlatır
                raise Exception(f"API Hata: {data}")
            return data # Başarılı yanıtı döndürür
        except requests.exceptions.Timeout:
            logger.warning(f"[ORDER] Zaman aşımı oldu (attempt {attempt})")
        except Exception as e:
            logger.error(f"[ORDER] Hata: {e}")
        time.sleep(1) # Başarısız denemeden sonra 1 saniye bekle
    # Tüm denemeler başarısız olursa istisna fırlatır
    raise RuntimeError("3 denemede de pozisyon açılamadı")

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    """Sağlık kontrolü endpoint'i."""
    return "OK", 200

@app.route("/balance", methods=["GET"])
def balance():
    """Hesap bakiyesini döndüren endpoint."""
    try:
        sync_time_with_exchange() # Zamanı senkronize et
        exchange = ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True
        })
        if USE_TESTNET:
            exchange.set_sandbox_mode(True) # Testnet modunu ayarla
        bal = exchange.fetch_balance({"type": "swap"}) # Vadeli işlem bakiyesini çek
        logger.info(f"[BALANCE] Bakiye bilgisi: {bal}")
        return jsonify({
            "USDT_swap_balance": bal['free'].get('USDT', 0), # Kullanılabilir USDT bakiyesi
            "raw": bal # Ham bakiye verisi
        }), 200
    except Exception as e:
        logger.error(f"[BALANCE] Bakiye çekme hatası: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def mexc_webhook():
    """TradingView'den gelen webhook sinyallerini işler."""
    try:
        data = request.get_json()
        logger.info(f"[WEBHOOK] Gelen veri: {data}")

        symbol = data.get("symbol") # TradingView'den gelen sembol (örn: "XRP")
        side = data.get("side")     # TradingView'den gelen taraf (örn: "Long", "Short")
        entry_price = data.get("entry_price") # TradingView'den gelen giriş fiyatı

        if not symbol or not side:
            return jsonify({"error": "Eksik parametreler: symbol veya side"}), 400

        trade_symbol = symbol + "USDT" # MEXC için sembol formatı (örn: "XRPUSDT")

        # ccxt borsasını başlat
        exchange = ccxt.mexc({
            "apiKey": MEXC_API_KEY,
            "secret": MEXC_API_SECRET,
            "enableRateLimit": True
        })
        if USE_TESTNET:
            exchange.set_sandbox_mode(True)

        # 1. Bakiye kontrolü
        usdt_balance = 0
        try:
            sync_time_with_exchange() # Her istekte zamanı tekrar senkronize etmek iyi bir pratik
            bal = exchange.fetch_balance({"type": "swap"})
            usdt_balance = bal['free'].get('USDT', 0)
            logger.info(f"[BAKIYE KONTROL] Mevcut USDT Vadeli İşlem Bakiyesi: {usdt_balance}")
        except Exception as e:
            logger.error(f"[BAKIYE KONTROL] Bakiye çekilirken hata oluştu: {e}")
            return jsonify({"error": "Bakiye çekilirken hata oluştu: " + str(e)}), 500

        # Eğer bakiye yoksa işlem yapma
        if usdt_balance <= 0:
            logger.warning("[BAKIYE KONTROL] Yeterli bakiye yok. İşlem iptal edildi.")
            return jsonify({"status": "failed", "message": "Yeterli bakiye yok"}), 400

        # 2. Açık pozisyonları kontrol et
        open_positions = []
        try:
            positions = exchange.fetch_positions([trade_symbol])
            # Sadece açık pozisyonları filtrele
            open_positions = [p for p in positions if float(p['contracts']) != 0]
            if open_positions:
                logger.info(f"[AÇIK POZİSYONLAR] Mevcut açık pozisyonlar: {open_positions}")
            else:
                logger.info("[AÇIK POZİSYONLAR] Açık pozisyon bulunamadı.")
        except Exception as e:
            logger.error(f"[AÇIK POZİSYONLAR] Pozisyon çekilirken hata oluştu: {e}")
            # Pozisyon çekilemese bile işleme devam edebiliriz, bu yüzden hata döndürmüyoruz

        # 3. Güncel piyasa fiyatını çek (miktar hesaplaması için)
        current_price = 0
        try:
            ticker = exchange.fetch_ticker(trade_symbol)
            current_price = ticker['last']
            logger.info(f"[FİYAT] {trade_symbol} güncel fiyatı: {current_price}")
        except Exception as e:
            logger.error(f"[FİYAT] {trade_symbol} fiyatı çekilirken hata oluştu: {e}")
            # Eğer fiyat çekilemezse, işlem yapamayız
            return jsonify({"error": f"{trade_symbol} fiyatı çekilemedi: " + str(e)}), 500

        if current_price <= 0:
            logger.warning(f"[FİYAT] {trade_symbol} için geçersiz fiyat: {current_price}. İşlem iptal edildi.")
            return jsonify({"status": "failed", "message": "Geçersiz piyasa fiyatı"}), 400

        # 4. Kullanılabilir bakiyenin tamamı ile işlem miktarı hesapla
        # Miktar = (Bakiye * Kaldıraç) / Güncel Fiyat
        # DEFAULT_LEVERAGE'ı burada çarpan olarak kullanıyoruz.
        # quantity: Base currency miktarı (örn: XRP miktarı)
        quantity = (usdt_balance * DEFAULT_LEVERAGE) / current_price
        
        # MEXC'nin XRPUSDT için minimum emir büyüklüğünü kontrol edin. Genellikle 1 XRP veya daha fazla olabilir.
        MIN_ORDER_QUANTITY_XRP = 1.0 # Bu değeri MEXC dokümantasyonundan veya pratik testlerle doğrulayın!
        
        if quantity < MIN_ORDER_QUANTITY_XRP:
            logger.warning(f"[MİKTAR HESAPLAMA] Hesaplanan işlem miktarı ({quantity} {symbol}) minimum emir miktarından ({MIN_ORDER_QUANTITY_XRP} {symbol}) küçük. İşlem iptal edildi.")
            return jsonify({"status": "failed", "message": f"Hesaplanan işlem miktarı çok küçük (Min: {MIN_ORDER_QUANTITY_XRP} {symbol})"}), 400
            
        quantity = float(f"{quantity:.6f}") # Örn: 6 ondalık basamağa yuvarla

        if quantity <= 0:
            logger.warning(f"[MİKTAR HESAPLAMA] Hesaplanan işlem miktarı sıfır veya negatif: {quantity}. İşlem iptal edildi.")
            return jsonify({"status": "failed", "message": "Hesaplanan işlem miktarı çok küçük"}), 400

        logger.info(f"[MİKTAR HESAPLAMA] {usdt_balance} USDT ile {DEFAULT_LEVERAGE}x kaldıraç ile {quantity} {symbol} işlemi yapılacak.")

        # İşlem başlat
        if side.lower() == "long":
            order_result = place_mexc_futures_order(trade_symbol, "long", quantity, price=None, leverage=DEFAULT_LEVERAGE)
            logger.info(f"[ORDER] Long sipariş başarıyla gönderildi: {order_result}")
            return jsonify({"status": "success", "message": "Long sipariş gönderildi", "order": order_result}), 200
        elif side.lower() == "short":
            order_result = place_mexc_futures_order(trade_symbol, "short", quantity, price=None, leverage=DEFAULT_LEVERAGE)
            logger.info(f"[ORDER] Short sipariş başarıyla gönderildi: {order_result}")
            return jsonify({"status": "success", "message": "Short sipariş gönderildi", "order": order_result}), 200
        else:
            return jsonify({"error": "Geçersiz side değeri"}), 400

    except Exception as e:
        logger.error(f"[WEBHOOK] İşlem hatası: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("Sunucu başlıyor...")
    sync_time_with_exchange() # Sunucu başladığında zamanı senkronize et
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
