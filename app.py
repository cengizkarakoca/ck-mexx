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

try:
    exchange.load_markets()
    logger.info(f"CCXT markets yüklendi, sembol sayısı: {len(exchange.symbols)}")
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
            msg = "Eksik veri: symbol, side veya entry_price yok"
            logger.warning(msg + f" | data: {data}")
            return jsonify({'error': msg}), 400
        try:
            entry_price = float(entry_price)
        except:
            msg = "Invalid entry_price format"
            logger.warning(msg + f" | entry_price: {entry_price}")
            return jsonify({'error': msg}), 400

        # Suffix temizle: örn "ETHUSDT.P" -> "ETHUSDT"
        coin_raw = coin.upper()
        if '.' in coin_raw:
            coin_raw = coin_raw.split('.')[0]
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

        # CCXT unified symbol arama
        unified_symbol = None
        for m in exchange.symbols:
            if m.replace(":", "").replace("/", "") == symbol.replace("/", ""):
                unified_symbol = m; break
            if ":" in m and m.split(":")[0] == symbol:
                unified_symbol = m; break
        if unified_symbol is None:
            msg = f"Sembol bulunamadı: {symbol}"
            logger.warning(msg)
            return jsonify({'error': msg}), 400
        logger.info(f"Parsed symbol: {symbol} -> unified: {unified_symbol}")

        # Bakiye çekme
        try:
            if 'future' in exchange.has and exchange.has['future']:
                balance = exchange.fetch_balance({'type':'future'})
            else:
                balance = exchange.fetch_balance()
            usdt_bal = None
            if 'free' in balance and 'USDT' in balance['free']:
                usdt_bal = float(balance['free']['USDT'])
            elif 'total' in balance and 'USDT' in balance['total']:
                usdt_bal = float(balance['total']['USDT'])
            else:
                raise Exception("USDT bakiyesi bulunamadı")
        except Exception as e:
            msg = f"Bakiye alınamadı: {e}"
            logger.error(msg)
            return jsonify({'error': msg}), 500
        logger.info(f"USDT bakiyesi: {usdt_bal}")
        if usdt_bal <= 0:
            msg = "Yetersiz bakiye"
            logger.warning(msg)
            return jsonify({'error': msg}), 400

        # Leverage ayarı
        try:
            exchange.set_leverage(DEFAULT_LEVERAGE, unified_symbol)
            logger.info(f"Leverage ayarlandı: {DEFAULT_LEVERAGE}x for {unified_symbol}")
        except Exception as e:
            logger.warning(f"Leverage ayarlanamadı: {e}")

        # Miktar hesaplama ve precision
        qty = (usdt_bal * RISK_RATIO * DEFAULT_LEVERAGE) / entry_price
        try:
            qty = exchange.amount_to_precision(unified_symbol, qty)
            entry_price = exchange.price_to_precision(unified_symbol, entry_price)
        except Exception:
            qty = round(qty, 3)
        if float(qty) <= 0:
            msg = "Qty sıfır veya negatif"
            logger.warning(msg)
            return jsonify({'error': msg}), 400
        logger.info(f"Pozisyon miktarı (qty): {qty}")

        is_long = side.strip().lower() == 'long'
        order_side = 'buy' if is_long else 'sell'

        # Pozisyon açma (market)
        try:
            open_order = exchange.create_order(unified_symbol, 'market', order_side, qty, None, {'leverage': DEFAULT_LEVERAGE})
            logger.info(f"Pozisyon açıldı: {open_order}")
        except Exception as e:
            msg = f"Pozisyon açma hatası: {e}"
            logger.error(msg)
            return jsonify({'error': msg}), 500

        # TP fiyatı %0.4
        tp_price = entry_price * (1.004 if is_long else 0.996)
        try:
            tp_price = exchange.price_to_precision(unified_symbol, tp_price)
        except:
            tp_price = round(tp_price, 2)
        logger.info(f"TP fiyatı belirlendi: {tp_price}")

        # TP emri (reduceOnly)
        try:
            tp_order = exchange.create_order(unified_symbol, 'limit', 'sell' if is_long else 'buy', qty, tp_price, {'reduceOnly': True})
            logger.info(f"TP order kondu: {tp_order}")
        except Exception as e:
            logger.error(f"TP emri hatası: {e}")
            # TP hatasında bile success dönebiliriz ya da bilgi verebiliriz
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
