import os
import traceback
from flask import Flask, request, jsonify
import ccxt
from dotenv import load_dotenv

# Lokal geliştirme için .env yükle (Render'da .env yerine env vars GUI kullanılacak)
load_dotenv()

app = Flask(__name__)

# Ortam değişkenlerinden oku
API_KEY = os.getenv("MEXC_API_KEY")
API_SECRET = os.getenv("MEXC_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("true", "1", "yes")
# Risk ratio: pozisyon açarken bakiye yüzdesi (örn 0.02 => %2)
try:
    RISK_RATIO = float(os.getenv("RISK_RATIO", "1.0"))
except:
    RISK_RATIO = 1.0
# Kaldıraç
try:
    DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "25"))
except:
    DEFAULT_LEVERAGE = 25

if not API_KEY or not API_SECRET:
    raise RuntimeError("MEXC_API_KEY veya MEXC_API_SECRET tanımlı değil.")

# CCXT Exchange nesnesi oluşturma
exchange_config = {
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    # Zaman farkı ayarı (CCXT otomatik zaman farkı düzeltme yapabiliyor)
    'options': {
        # Futures modları MEXC destekliyorsa ayarlar eklenebilir
    },
}
# MEXC testnet endpoint: CCXT destekliyorsa şöyle:
if USE_TESTNET:
    # CCXT ile MEXC futures testnet endpoint belirleme:
    # Bazı borsalar için exchange.set_sandbox_mode(True) kullanılabilir; MEXC CCXT destekliyorsa:
    # exchange = ccxt.mexc({'apiKey':..., 'secret':..., 'enableRateLimit':True})
    # exchange.set_sandbox_mode(True)
    # Ancak MEXC testnet CCXT ile çalışmıyorsa bu kısmı False yapıp küçük miktarla gerçek test edin.
    exchange = ccxt.mexc(exchange_config)
    try:
        exchange.set_sandbox_mode(True)
    except Exception as e:
        print("Testnet modu ayarlanamadı veya desteklenmiyor:", str(e))
else:
    exchange = ccxt.mexc(exchange_config)

# Timeout ve load markets
# load_markets ile sembol bilgilerini önceden yükleyin
try:
    exchange.load_markets()
except Exception as e:
    print("load_markets hatası:", str(e))

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        # Beklenen alanlar: symbol, side, entry_price
        coin = data.get('symbol')      # Örn "BTCUSDT" veya "BTC/USDT"
        side = data.get('side')        # "Long" veya "Short"
        entry_price = data.get('entry_price')  # float ya da str
        if not all([coin, side, entry_price]):
            return jsonify({'error': 'Eksik veri: symbol, side veya entry_price yok'}), 400

        # entry_price float çevir
        try:
            entry_price = float(entry_price)
        except:
            return jsonify({'error': 'Geçersiz entry_price formatı'}), 400

        # symbol formatlama: CCXT unified format: "BTC/USDT"
        symbol = coin.upper()
        # Eğer slash yoksa ekle: "BTCUSDT" -> "BTC/USDT"
        if '/' not in symbol:
            # Basit ayrıştırma: sondaki "USDT" ile böl
            if symbol.endswith("USDT"):
                base = symbol[:-4]
                quote = "USDT"
                symbol = base + "/" + quote
            else:
                return jsonify({'error': f"Symbol formatı anlaşılmadı: {coin}"}), 400

        # CCXT sembol doğruluğu: futures piyasası sembolü CCXT’de genellikle "BTC/USDT:USDT"
        # Ancak MEXC CCXT entegre futures sembol standardı farklı olabilir. 
        # Örnek: MEXC futures piyasasında CCXT’de sembol listesine bakın:
        # markets = exchange.load_markets()
        # futures sembolleri genelde 'BTC/USDT:USDT' formatında olabilir. Burada basitçe spot modu kullanıyorsanız pozisyon açamayabilirsiniz.
        # Eğer sadece spot işlemlerse, kaldıraç yoktur; futures için futures endpoint'ine yönelmelisiniz.
        # Bu örnekte futures olduğunu varsayıyoruz ve CCXT’nin unified futures sembol formatını kullanıyoruz:
        # “BTC/USDT:USDT” formatı CCXT’deki unified symbol olabilir. Bunu load_markets() sonrası kontrol edin:
        unified_symbol = None
        # Arama: CCXT markets içinden eşleşme bul
        for m in exchange.symbols:
            if m.replace(":", "").replace("/", "") == symbol.replace("/", ""):
                # Eşleşen spot symbol
                unified_symbol = m
                break
            # Futures sembol kontrol: bazen "BTC/USDT:USDT"
            if ":" in m and m.split(":")[0] == symbol:
                unified_symbol = m
                break
        if unified_symbol is None:
            return jsonify({'error': f"CCXT markets içinde sembol bulunamadı: {symbol}"}), 400

        # Pozisyon büyüklüğü: bakiye ve risk oranı
        # Futures account balance USDT cinsinden alınmalı. CCXT fetch_balance kullanılır:
        try:
            balance = exchange.fetch_balance({'type': 'future'}) if 'future' in exchange.has and exchange.has['future'] else exchange.fetch_balance()
            # CCXT balance: {'total': {'USDT': ...}, 'free': {...}, ...}
            usdt_balance = None
            if 'USDT' in balance.get('free', {}):
                usdt_balance = float(balance['free']['USDT'])
            elif 'USDT' in balance.get('total', {}):
                usdt_balance = float(balance['total']['USDT'])
            else:
                return jsonify({'error': 'USDT bakiyesi alınamadı'}), 500
        except Exception as e:
            return jsonify({'error': f'Cüzdan bakiyesi alınamadı: {str(e)}'}), 500

        if usdt_balance <= 0:
            return jsonify({'error': 'Yetersiz bakiye'}), 400

        # Leverage ayarı: CCXT setLeverage
        try:
            # CCXT unified setLeverage: setLeverage(leverage, symbol)
            exchange.set_leverage(DEFAULT_LEVERAGE, unified_symbol)
        except Exception as e:
            # Hata logla, ancak devam edebiliriz
            print("Leverage ayarlanamadı:", str(e))

        # Pozisyon miktarı (contract miktarı):
        # Futures pozisyon büyüklüğü hesaplaması borsaya göre değişir; CCXT için amount param genelde sözleşme miktarıdır.
        # Basit mantık: usdt_balance * RISK_RATIO * leverage / entry_price
        qty = (usdt_balance * RISK_RATIO * DEFAULT_LEVERAGE) / entry_price
        # Yuvarlama: sembolün amount precision’ına göre round etmek ideal. Basit: 3 ondalık
        qty = round(qty, 3)
        if qty <= 0:
            return jsonify({'error': 'Hesaplanan qty sıfır veya negatif'}), 400

        # Side kontrol
        is_long = side.strip().lower() == 'long'
        order_side = 'buy' if is_long else 'sell'

        # Market order ile pozisyon aç
        try:
            # CCXT create_order: exchange.create_order(symbol, type, side, amount, price=None, params={})
            open_order = exchange.create_order(
                unified_symbol,
                'market',
                order_side,
                qty,
                None,
                {
                    'leverage': DEFAULT_LEVERAGE,
                    # 'reduceOnly': False  # create market entry
                }
            )
        except Exception as e:
            return jsonify({'error': f'Pozisyon açma hatası: {str(e)}'}), 500

        # TP fiyatı: %0.4 kazanç
        if is_long:
            tp_price = entry_price * 1.004
        else:
            tp_price = entry_price * 0.996
        # Yuvarla price precision: örn 2 ondalık; ideal olarak market tick size’dan alınmalı
        tp_price = round(tp_price, 2)

        # Reduce-only limit order ile TP koy
        try:
            tp_order = exchange.create_order(
                unified_symbol,
                'limit',
                'sell' if is_long else 'buy',
                qty,
                tp_price,
                {
                    'reduceOnly': True,
                    # 'timeInForce': 'GTC'  # CCXT bazı borsalarda bu param gerekir
                }
            )
        except Exception as e:
            print("TP emri hatası:", str(e))
            tp_order = None

        return jsonify({
            'status': 'success',
            'symbol': unified_symbol,
            'side': side,
            'entry_price': entry_price,
            'qty': qty,
            'tp_price': tp_price,
            'open_order': open_order,
            'tp_order': tp_order
        }), 200

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({'error': str(e), 'trace': tb}), 500

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    # Render için debug=False tavsiye edilir
    app.run(host="0.0.0.0", port=port, debug=False)
