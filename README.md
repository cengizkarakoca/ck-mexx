# MEXC Webhook Bot

TradingView’den gelen sinyale göre MEXC futures’ta pozisyon açıp %0.4 TP koyan Flask+CCXT sunucu.

## Dosyalar
- app.py       : Flask + CCXT kodu
- requirements.txt
- .gitignore
- README.md

## Ortam Değişkenleri (Render üzerinden ekleyin)
- MEXC_API_KEY
- MEXC_API_SECRET
- USE_TESTNET   (True/False)
- RISK_RATIO    (örn "0.02")
- LEVERAGE      (örn "25")

## Deploy (Render.com)
1. GitHub’a push edilmiş repo.
2. Render’da New Web Service → GitHub repo seç → Build Command: `pip install -r requirements.txt` → Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT`
3. Environment Variables ekleyin.
4. Deploy’u başlatın.

## Pine Script & Alert
- Pine Script’i TradingView Pine Editor’e yapıştırın (örnek kod).
- “Save” ve “Add to Chart” yapın.
- Alert: Condition “Any alert() function call”, Webhook URL: `https://<service>.onrender.com/webhook`, Trigger “Once Per Bar Close”.

## Test
- Deploy sonrası cURL ile test gönderin:
