import os
import time
import hmac
import hashlib
import logging
import traceback
from functools import wraps
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import ccxt
import requests

# Environment and Logging Setup
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("mexc_futures.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "20"))
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30

# API Constants
BASE_URL = "https://contract.mexc.com/api/v1"
WS_URL = "wss://contract.mexc.com/ws"
FUTURES_TYPE = "swap"

if not all([MEXC_API_KEY, MEXC_API_SECRET]):
    raise RuntimeError("API credentials missing in environment variables")

# Utility Functions
def generate_signature(params=None):
    """Generate HMAC-SHA256 signature for private endpoints"""
    timestamp = str(int(time.time() * 1000))
    message = f"{MEXC_API_KEY}{timestamp}"
    signature = hmac.new(
        MEXC_API_SECRET.encode(), 
        message.encode(), 
        hashlib.sha256
    ).hexdigest()
    return signature, timestamp

def normalize_symbol(symbol):
    """Convert symbol to MEXC Futures format (BTC_USDT)"""
    symbol = symbol.upper().replace("-", "_")
    if not symbol.endswith(("_USDT", "_USD")):
        if "USDT" in symbol:
            symbol = symbol.replace("USDT", "_USDT")
        elif "USD" in symbol:
            symbol = symbol.replace("USD", "_USD")
    return symbol

def handle_api_errors(func):
    """Decorator for API error handling"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except ccxt.NetworkError as e:
                if attempt == MAX_RETRIES - 1:
                    logger.error(f"Network error: {str(e)}")
                    raise
                time.sleep(1)
            except ccxt.ExchangeError as e:
                logger.error(f"Exchange error: {str(e)}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error: {str(e)}")
                raise
    return wrapper

# Exchange Initialization
def init_exchange():
    """Initialize CCXT MEXC Futures exchange"""
    exchange = ccxt.mexc({
        'apiKey': MEXC_API_KEY,
        'secret': MEXC_API_SECRET,
        'enableRateLimit': True,
        'options': {
            'defaultType': FUTURES_TYPE,
            'adjustForTimeDifference': True
        }
    })
    exchange.load_markets()
    return exchange

# Core Trading Functions
@handle_api_errors
def set_leverage(exchange, symbol, leverage):
    """Set leverage according to MEXC API specs"""
    return exchange.set_leverage(
        leverage, 
        symbol,
        params={
            'openType': 1,  # 1: isolated, 2: cross
            'positionType': 1  # Required but can be changed per position
        }
    )

@handle_api_errors
def place_futures_order(exchange, symbol, side, amount, price=None, params=None):
    """Place futures order with proper MEXC parameters"""
    order_type = 'market' if price is None else 'limit'
    return exchange.create_order(
        symbol=symbol,
        type=order_type,
        side='buy' if side.lower() == 'long' else 'sell',
        amount=amount,
        price=price,
        params={
            'type': FUTURES_TYPE,
            'positionMode': 2,  # 1: hedge, 2: one-way
            'leverage': DEFAULT_LEVERAGE,
            **(params or {})
        }
    )

# Flask Application
app = Flask(__name__)

@app.route('/api/v1/ping', methods=['GET'])
def ping():
    """Check server status"""
    try:
        response = requests.get(f"{BASE_URL}/contract/ping", timeout=REQUEST_TIMEOUT)
        return jsonify(response.json()), 200
    except Exception as e:
        logger.error(f"Ping error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/v1/contract/details', methods=['GET'])
def contract_details():
    """Get contract details"""
    symbol = request.args.get('symbol')
    if not symbol:
        return jsonify({"error": "Symbol parameter is required"}), 400
    
    try:
        normalized_symbol = normalize_symbol(symbol)
        response = requests.get(
            f"{BASE_URL}/contract/detail",
            params={'symbol': normalized_symbol},
            timeout=REQUEST_TIMEOUT
        )
        return jsonify(response.json()), 200
    except Exception as e:
        logger.error(f"Contract details error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/v1/futures/order', methods=['POST'])
def create_order():
    """Create futures order"""
    try:
        data = request.get_json()
        required_fields = ['symbol', 'side', 'quantity']
        if not all(field in data for field in required_fields):
            return jsonify({"error": "Missing required fields"}), 400

        exchange = init_exchange()
        symbol = normalize_symbol(data['symbol'])
        
        # Set leverage
        set_leverage(exchange, symbol, DEFAULT_LEVERAGE)
        
        # Place order
        order = place_futures_order(
            exchange=exchange,
            symbol=symbol,
            side=data['side'],
            amount=float(data['quantity']),
            price=float(data.get('price')) if data.get('price') else None
        )
        
        return jsonify({
            "status": "success",
            "order": order
        }), 200

    except ccxt.InsufficientFunds as e:
        return jsonify({"error": "Insufficient funds"}), 400
    except ccxt.InvalidOrder as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Order error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/v1/futures/balance', methods=['GET'])
def get_balance():
    """Get futures account balance"""
    try:
        exchange = init_exchange()
        balance = exchange.fetch_balance({'type': FUTURES_TYPE})
        return jsonify({
            "free": balance['free'],
            "used": balance['used'],
            "total": balance['total']
        }), 200
    except Exception as e:
        logger.error(f"Balance error: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=os.getenv("DEBUG", "false").lower() == "true")
