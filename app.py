# app.py
import os, traceback
from flask import Flask, request, jsonify
import ccxt
from dotenv import load_dotenv
import logging

# .env okumayı lokal test için
load_dotenv()

# Logging yapılandırma
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Ortam değişkenleri
API_KEY = os.getenv("MEXC_API_KEY")
API_SECRET = os.getenv("MEXC_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("true","1","yes")
try:
    RISK_RATIO = float(os.getenv("RISK_RATIO", "1.0"))
except:
    RISK_RATIO = 1.0
try:
    DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "25"))
except:
    DEFAULT_LEVERAGE = 25

if not API_KEY or not API_SECRET:
    logger.error("MEXC_API_KEY veya MEXC_API_SECRET tanımlı değil.")
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET tanımlı değil.")

# CCXT exchange oluşturma
exchange_config = {'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True}
if USE_TESTNET:
    exchange = ccxt.mexc(exchange_config)
    try:
        exchange.set_sandbox_mode(True)
        logger.info("MEXC testnet modu etkin.")
    except Exception as e:
        logger.warning(f"Testnet modu ayarlanamadı: {e}")
else:
    exchange = ccxt.mexc(exchange_config)
    logger.info("MEXC gerçek modda çalışacak.")

# load_markets ve has loglama
try:
    exchange.load_markets()
    logger.info(f"CCXT markets yüklendi, sembol sayısı: {len(exchange.symbols)}")
    logger.info(f"Exchange.has: {exchange.has}")
except Exception as e:
    logger.error(f"load_markets hatası: {e}")

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        logger.info(f"[WEBHOOK] Alındı: {data}")

        # Gerekli alanlar
        coin = data.get('symbol')
        side = data.get('side')
        entry_price = data.get('entry_price')
        if not all([coin, side, entry_price]):
            msg = "Eksik veri: symbol veya side veya entry_price yok"
            logger.warning(msg + f" | data: {data}")
            return jsonify({'error': msg}), 400
        try:
            entry_price = float(entry_price)
        except:
            msg = "Invalid entry_price format"
            logger.warning(msg + f" | entry_price: {entry_price}")
            return jsonify({'error': msg}), 400

        # Suffix temizle (örn ".P", ".PERP" varsa)
        coin_raw = coin.upper()
        if '.' in coin_raw:
            coin_raw = coin_raw.split('.')[0]
        logger.info(f"Suffix temizlendikten sonra coin_raw: {coin_raw}")

        # Symbol parse: "ETHUSDT" -> "ETH/USDT"
        symbol = coin_raw
        if '/' not in symbol:
            if symbol.endswith("USDT"):
                base = symbol[:-4]; quote = "USDT"
                symbol = base + "/" + quote
            else:
                msg = f"Symbol formatı anlaşılmadı: {coin}"
                logger.warning(msg)
                return jsonify({'error': msg}), 400
        logger.info(f"Parsed symbol string: {symbol}")

        # Futures (swap) unified symbol bulma
        base, quote = symbol.split('/')
        unified_symbol = None
        # Önce spot aramayacağız, doğrudan swap pazarında arıyoruz:
        for m, market in exchange.markets.items():
            # market['type'] genellikle 'swap' ise futures/perpetual
            if market.get('type') == 'swap' and market.get('base') == base and market.get('quote') == quote:
                unified_symbol = m
                logger.info(f"Found swap market: {m}")
                break
        if unified_symbol is None:
            # Eğer swap bulunamazsa fallback spot arama
            for m, market in exchange.markets.items():
                if market.get('type') in ('spot',) and market.get('base') == base and market.get('quote') == quote:
                    unified_symbol = m
                    logger.info(f"Swap market bulunamadı, spot market kullanılıyor: {m}")
                    break
        if unified_symbol is None:
            msg = f"Sembol bulunamadı: {symbol} (swap veya spot)"
            logger.warning(msg)
            return jsonify({'error': msg}), 400
        logger.info(f"Unified symbol seçildi: {unified_symbol}")

        # Bakiye çekme: futures hesabı
        usdt_bal = None
        try:
            if 'future' in exchange.has and exchange.has['future']:
                balance = exchange.fetch_balance({'type':'future'})
                logger.info("fetch_balance({'type':'future'}) kullanıldı.")
            else:
                # Bazı CCXT adaptörlerinde futures fetch farklı olabilir; yine de deneyelim
                balance = exchange.fetch_balance({'type':'linear'}) if 'linear' in exchange.has and exchange.has['linear'] else exchange.fetch_balance()
                logger.info("fetch_balance fallback olarak spot veya linear kullanıldı.")
            # Balance içinde USDT futures bakiyesi
            if 'free' in balance and 'USDT' in balance['free']:
                usdt_bal = float(balance['free']['USDT'])
            elif 'total' in balance and 'USDT' in balance['total']:
                usdt_bal = float(balance['total']['USDT'])
            else:
                # Bazı durumlarda farklı anahtar olabilir
                logger.warning(f"Balance objesinde USDT bulunamadı: keys free={list(balance.get('free',{}).keys())}, total={list(balance.get('total',{}).keys())}")
                raise Exception("USDT bakiyesi bulunamadı")
        except Exception as e:
            msg = f"Bakiye alınamadı: {e}"
            logger.error(msg)
            return jsonify({'error': msg}), 500
        logger.info(f"USDT bakiyesi (futures): {usdt_bal}")
        if usdt_bal is None or usdt_bal <= 0:
            msg = f"Yetersiz bakiye: {usdt_bal}"
            logger.warning(msg)
            return jsonify({'error': msg}), 400

        # Pozisyon tarafı
        is_long = side.strip().lower() == 'long'
        # Leverage ayarı MEXC (openType, positionType parametreleri ile)
        try:
            params = {'openType': 1, 'positionType': 1 if is_long else 2}
            exchange.set_leverage(DEFAULT_LEVERAGE, unified_symbol, params)
            logger.info(f"Leverage ayarlandı: {DEFAULT_LEVERAGE}x for {unified_symbol} with {params}")
        except Exception as e:
            logger.warning(f"Leverage ayarlanamadı: {e}")

        # Miktar hesaplama
        qty = (usdt_bal * RISK_RATIO * DEFAULT_LEVERAGE) / entry_price
        try:
            qty = exchange.amount_to_precision(unified_symbol, qty)
            entry_price = exchange.price_to_precision(unified_symbol, entry_price)
        except Exception:
            qty = round(qty, 3)
        if float(qty) <= 0:
            msg = f"Qty sıfır veya negatif (bakiye: {usdt_bal}, hesaplanan qty: {qty})"
            logger.warning(msg)
            return jsonify({'error': msg}), 400
        logger.info(f"Pozisyon miktarı (qty): {qty}")

        # Pozisyon açma (market)
        order_side = 'buy' if is_long else 'sell'
        try:
            open_order = exchange.create_order(unified_symbol, 'market', order_side, qty, None, {'leverage': DEFAULT_LEVERAGE})
            logger.info(f"Pozisyon açıldı: {open_order}")
        except Exception as e:
            msg = f"Pozisyon açma hatası: {e}"
            logger.error(msg)
            return jsonify({'error': msg}), 500

        # TP işlemi
        tp_price = entry_price * (1.004 if is_long else 0.996)
        try:
            tp_price = exchange.price_to_precision(unified_symbol, tp_price)
        except:
            tp_price = round(tp_price, 2)
        logger.info(f"TP fiyatı belirlendi: {tp_price}")

        try:
            tp_order = exchange.create_order(unified_symbol, 'limit', 'sell' if is_long else 'buy', qty, tp_price, {'reduceOnly': True})
            logger.info(f"TP order kondu: {tp_order}")
        except Exception as e:
            logger.error(f"TP emri hatası: {e}")
            tp_order = None

        return jsonify({
            'status': 'success',
            'symbol': unified_symbol,
            'side': side,
            'qty': qty,
            'tp_price': tp_price,
            'open_order': open_order,
            'tp_order': tp_order
        }), 200

    except Exception as e:
        trace = traceback.format_exc()
        logger.exception("Webhook işlenirken beklenmedik hata")
        return jsonify({'error': str(e), 'trace': trace}), 500

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

if __name__ == '__main__':
    logger.info("Sunucu başlatılıyor")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
