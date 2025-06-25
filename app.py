# app.py
import os, traceback
from flask import Flask, request, jsonify
import ccxt
from dotenv import load_dotenv
import logging

# .env okunması (yerelde test için)
load_dotenv()

# Logging
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

# CCXT exchange
exchange_config = {'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True}
exchange = ccxt.mexc(exchange_config)
if USE_TESTNET:
    try:
        exchange.set_sandbox_mode(True)
        logger.info("MEXC testnet modu etkin.")
    except Exception as e:
        logger.warning(f"Testnet modu ayarlanamadı: {e}")
else:
    logger.info("MEXC gerçek modda çalışacak.")

# Markets yükle ve logla
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

        # Suffix temizle
        coin_raw = coin.upper()
        if '.' in coin_raw:
            coin_raw = coin_raw.split('.')[0]
        logger.info(f"Suffix temizlendikten sonra coin_raw: {coin_raw}")

        # Symbol parse: ETHUSDT -> ETH/USDT
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

        base, quote = symbol.split('/')
        unified_symbol = None
        market_id = None
        # Önce swap (perpetual)
        for m, market in exchange.markets.items():
            if market.get('type') == 'swap' and market.get('base') == base and market.get('quote') == quote:
                unified_symbol = m
                market_id = market.get('id')
                logger.info(f"Found swap market: unified_symbol={m}, market_id={market_id}")
                break
        # Fallback spot
        if unified_symbol is None:
            for m, market in exchange.markets.items():
                if market.get('type') == 'spot' and market.get('base') == base and market.get('quote') == quote:
                    unified_symbol = m
                    market_id = market.get('id')
                    logger.info(f"Swap bulunamadı, spot market kullanılıyor: unified_symbol={m}, market_id={market_id}")
                    break
        if unified_symbol is None or market_id is None:
            msg = f"Sembol bulunamadı: {symbol}"
            logger.warning(msg)
            return jsonify({'error': msg}), 400
        logger.info(f"Unified symbol: {unified_symbol}, market_id: {market_id}")

        # Bakiye çekme (swap)
        usdt_bal = None
        try:
            if exchange.has.get('swap'):
                balance = exchange.fetch_balance({'type':'swap'})
                logger.info("fetch_balance({'type':'swap'}) kullanıldı.")
            else:
                balance = exchange.fetch_balance()
                logger.info("fetch_balance fallback kullanıldı.")
            logger.info(f"Balance raw: {balance}")
            # Önce free/total içinden:
            if 'free' in balance and 'USDT' in balance['free']:
                usdt_bal = float(balance['free']['USDT'])
            elif 'total' in balance and 'USDT' in balance['total']:
                usdt_bal = float(balance['total']['USDT'])
            else:
                logger.warning(f"Balance objesinde USDT bulunamadı: free keys={list(balance.get('free',{}).keys())}, total keys={list(balance.get('total',{}).keys())}")
                info = balance.get('info')
                logger.info(f"Balance info kısmı: {info}")
                # info['data'] listesinde USDT entry arama
                if isinstance(info, dict) and isinstance(info.get('data'), list):
                    for entry in info.get('data'):
                        if entry.get('currency') == 'USDT':
                            val = entry.get('availableBalance') or entry.get('availableCash') or entry.get('availableOpen')
                            if val is not None:
                                try:
                                    usdt_bal = float(val)
                                    logger.info(f"Balance info.data entry kullanıldı: available={usdt_bal}")
                                except:
                                    pass
                            break
                if usdt_bal is None:
                    raise Exception("USDT bakiyesi bulunamadı info içinde")
        except Exception as e:
            msg = f"Bakiye alınamadı: {e}"
            logger.error(msg)
            return jsonify({'error': msg}), 500
        logger.info(f"USDT bakiyesi (swap): {usdt_bal}")
        if usdt_bal is None or usdt_bal <= 0:
            msg = f"Yetersiz bakiye: {usdt_bal}"
            logger.warning(msg)
            return jsonify({'error': msg}), 400

        # Pozisyon tarafı
        is_long = side.strip().lower() == 'long'
        # Leverage ayarı
        try:
            params = {'openType': 1, 'positionType': 1 if is_long else 2}
            exchange.set_leverage(DEFAULT_LEVERAGE, unified_symbol, params)
            logger.info(f"Leverage ayarlandı: {DEFAULT_LEVERAGE}x for {unified_symbol} with {params}")
        except Exception as e:
            logger.warning(f"Leverage ayarlanamadı: {e}")

        # Miktar hesapla
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

        # Pozisyon açma: farklı sembol formatlarını deneyen fallback
        order_side = 'buy' if is_long else 'sell'
        open_order = None
        order_errors = []
        # Alternatif semboller: market_id (örn "ETH_USDT"), birleşik ("ETHUSDT"), unified_symbol (örn "ETH/USDT:USDT")
        alt_symbols = []
        if market_id:
            alt_symbols.append(market_id)
        # birleşik format: base+quote
        alt_symbols.append(base + quote)
        # bazen alt tire yerine boşluk veya farklı? ama deneyelim
        alt_symbols.append(base + "_" + quote)
        # unified
        alt_symbols.append(unified_symbol)
        # uniq liste
        seen = set()
        alt_symbols = [s for s in alt_symbols if s and not (s in seen or seen.add(s))]
        for sym in alt_symbols:
            try:
                logger.info(f"Pozisyon açma denemesi sembol ile: {sym}")
                open_order = exchange.create_order(sym, 'market', order_side, qty, None, {'leverage': DEFAULT_LEVERAGE})
                logger.info(f"Pozisyon açıldı with symbol {sym}: {open_order}")
                break
            except Exception as e:
                error_msg = str(e)
                order_errors.append((sym, error_msg))
                logger.warning(f"Pozisyon açma denemesi başarısız symbol={sym}: {error_msg}")
        if open_order is None:
            msg = f"Pozisyon açma hatası, denenen semboller ve hatalar: {order_errors}"
            logger.error(msg)
            return jsonify({'error': msg}), 500

        # TP işlemi: yine fallback sembollerle
        tp_price = entry_price * (1.004 if is_long else 0.996)
        try:
            tp_price = exchange.price_to_precision(unified_symbol, tp_price)
        except:
            tp_price = round(tp_price, 2)
        logger.info(f"TP fiyatı belirlendi: {tp_price}")

        tp_order = None
        tp_errors = []
        for sym in alt_symbols:
            try:
                logger.info(f"TP emri denemesi sembol ile: {sym}")
                tp_order = exchange.create_order(sym, 'limit', 'sell' if is_long else 'buy', qty, tp_price, {'reduceOnly': True})
                logger.info(f"TP order kondu with symbol {sym}: {tp_order}")
                break
            except Exception as e:
                err = str(e)
                tp_errors.append((sym, err))
                logger.warning(f"TP emri denemesi başarısız symbol={sym}: {err}")
        if tp_order is None:
            logger.error(f"TP order konulamadı, denenen semboller ve hatalar: {tp_errors}")
            # TP emri başarısız olsa bile açılan pozisyon var; isterseniz burayı hata döndürmek yerine bilgi döndürün.
            # return jsonify({'error': 'TP emri konulamadı'}), 500

        return jsonify({
            'status': 'success',
            'symbol': unified_symbol,
            'market_id': market_id,
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
