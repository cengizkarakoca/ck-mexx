# app.py
import os, traceback
from flask import Flask, request, jsonify
import ccxt
from dotenv import load_dotenv

# Lokal test için .env dosyası; Render’da ortam değişkenleri GUI’den girilecek
load_dotenv()
app = Flask(__name__)

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
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET tanımlı değil.")

# CCXT exchange oluşturma
exchange_config = {'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True}
if USE_TESTNET:
    exchange = ccxt.mexc(exchange_config)
    try:
        exchange.set_sandbox_mode(True)
    except Exception:
        pass
else:
    exchange = ccxt.mexc(exchange_config)

try:
    exchange.load_markets()
except Exception:
    pass

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        coin = data.get('symbol')
        side = data.get('side')
        entry_price = data.get('entry_price')
        if not all([coin, side, entry_price]):
            return jsonify({'error': 'Eksik veri'}), 400
        try:
            entry_price = float(entry_price)
        except:
            return jsonify({'error': 'Invalid entry_price'}), 400

        # Symbol parse
        symbol = coin.upper()
        if '/' not in symbol:
            if symbol.endswith("USDT"):
                base = symbol[:-4]; quote = "USDT"
                symbol = base + "/" + quote
            else:
                return jsonify({'error': 'Symbol formatı anlaşılmadı'}), 400

        # CCXT unified symbol arama
        unified_symbol = None
        for m in exchange.symbols:
            if m.replace(":", "").replace("/", "") == symbol.replace("/", ""):
                unified_symbol = m; break
            if ":" in m and m.split(":")[0] == symbol:
                unified_symbol = m; break
        if unified_symbol is None:
            return jsonify({'error': f"Sembol bulunamadı: {symbol}"}), 400

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
                return jsonify({'error': 'USDT bakiyesi alınamadı'}), 500
        except Exception as e:
            return jsonify({'error': f'Bakiye alınamadı: {e}'}), 500
        if usdt_bal <= 0:
            return jsonify({'error': 'Yetersiz bakiye'}), 400

        # Leverage ayarı
        try:
            exchange.set_leverage(DEFAULT_LEVERAGE, unified_symbol)
        except Exception:
            pass

        # Miktar hesaplama ve precision
        qty = (usdt_bal * RISK_RATIO * DEFAULT_LEVERAGE) / entry_price
        try:
            qty = exchange.amount_to_precision(unified_symbol, qty)
            entry_price = exchange.price_to_precision(unified_symbol, entry_price)
        except Exception:
            qty = round(qty, 3)
        if float(qty) <= 0:
            return jsonify({'error': 'Qty sıfır veya negatif'}), 400

        is_long = side.strip().lower() == 'long'
        order_side = 'buy' if is_long else 'sell'

        # Market order ile pozisyon açma
        try:
            open_order = exchange.create_order(unified_symbol, 'market', order_side, qty, None, {'leverage': DEFAULT_LEVERAGE})
        except Exception as e:
            return jsonify({'error': f'Pozisyon açma hatası: {e}'}), 500

        # TP fiyatı %0.4
        tp_price = entry_price * (1.004 if is_long else 0.996)
        try:
            tp_price = exchange.price_to_precision(unified_symbol, tp_price)
        except:
            tp_price = round(tp_price, 2)

        # TP emri (reduceOnly)
        try:
            tp_order = exchange.create_order(unified_symbol, 'limit', 'sell' if is_long else 'buy', qty, tp_price, {'reduceOnly': True})
        except Exception:
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
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
