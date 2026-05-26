# Full Upstox API Portal - server.py
from flask_cors import CORS
from flask import Flask, jsonify, request, redirect
import requests
import json
import os
import random
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask_sock import Sock
from realtime import run_background
import threading

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

sock = Sock(app)

# helpers for persisting tokens so they survive process restarts
def _env_path():
    return os.path.join(os.getcwd(), '.env')

def _save_tokens_to_env(access_token, refresh_token=None):
    path = _env_path()
    lines = []
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            lines = []

    def _set_var(lines, key, val):
        prefix = f"{key}="
        for i, l in enumerate(lines):
            if l.startswith(prefix):
                lines[i] = f'{key}="{val}"\n'
                return lines
        lines.append(f'{key}="{val}"\n')
        return lines

    lines = _set_var(lines, 'ACCESS_TOKEN', access_token)
    if refresh_token:
        lines = _set_var(lines, 'REFRESH_TOKEN', refresh_token)

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
    except Exception:
        pass

# =====================================================
# CONFIG
# =====================================================

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN") or "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI1SkI5M0wiLCJqdGkiOiI2YTBmMTE2ODkwMzNhMjYwZTA2NjI1NDAiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc3OTM3MjM5MiwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzc5NDAwODAwfQ.MSGcgIorPYFdD5FXYyoJcFB-t-RKI-HOGJAsX0ntaPg"
BASE_URL = "https://api.upstox.com/v2"

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN") or "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI1SkI5M0wiLCJqdGkiOiI2YTA4NzM5YWJiODUwMDZjZWNmYjZlNTIiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlzRXh0ZW5kZWQiOnRydWUsImlhdCI6MTc3ODkzODc3OCwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxODEwNTA0ODAwfQ.NQdkTjbNHnhycI4PqmUqWqCGL9LnaxdSZ7iTtLnh67k"
CLIENT_ID = "431300c1-0f1f-40a7-8968-6dfb147b2ba9"
CLIENT_SECRET = "q5dtkq1fr7"


def refresh_access_token():
    global ACCESS_TOKEN
    global HEADERS

    url = "https://api.upstox.com/login/authorization/token"

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    

    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    r = requests.post(url, data=payload, headers=headers)

    data = r.json()

    print(data)

    if "access_token" in data:
        ACCESS_TOKEN = data["access_token"]

        HEADERS["Authorization"] = f"Bearer {ACCESS_TOKEN}"

        # persist refreshed tokens so server can reuse them after restart
        try:
            _save_tokens_to_env(ACCESS_TOKEN, data.get('refresh_token'))
        except Exception:
            pass

        print("Token Refreshed")


# =====================================================
# HOME
# =====================================================

@app.route("/")
def home():

    return jsonify({

        "message": "Full Upstox Developer API Portal",

        "categories": {

            "Authentication": "/authentication",
            "User": "/user",
            "Orders": "/orders",
            "GTT Orders": "/gtt",
            "Portfolio": "/portfolio",
            "Funds": "/funds",
            "Charges": "/charges",
            "Margins": "/margins",
            "Market Quote": "/marketquote",
            "Historical Data": "/historical",
            "Option Chain": "/optionchain",
            "Market Information": "/marketinfo",
            "Fundamentals": "/fundamentals",
            "News": "/news",
            "Trade Profit Loss": "/pnl",
            "Mutual Fund": "/mutualfund",
            "Instruments": "/instruments",
            "Expired Instruments": "/expired",
            "Websocket": "/websocket",
            "Webhook": "/webhook",
            "Sandbox": "/sandbox"
        }
    })

# =====================================================
# AUTHENTICATION
# =====================================================

@app.route("/api/authentication")
def authentication():

    return jsonify({

        "Authentication APIs": {

            "Profile": "/api/authentication/profile",
            "Logout": "/api/authentication/logout",
            "Funds": "/api/authentication/funds"
        }
    })

def _unwrap_response(response):
    payload = response.json()
    return payload.get('data', payload)

def _normalize_funds(response):
    data = _unwrap_response(response)
    available = None
    if isinstance(data, dict):
        equity = data.get('equity') or {}
        available = equity.get('available_margin')
        if available is None:
            available = data.get('available_funds') or data.get('funds')
    return {
        'available_funds': available if available is not None else 0,
        **({'raw': data} if isinstance(data, dict) else {})
    }


def _get_order_id(result):
    if not isinstance(result, dict):
        return None
    data = result.get('data')
    if isinstance(data, dict):
        return data.get('order_id') or data.get('client_order_id') or data.get('id')
    return result.get('order_id') or result.get('client_order_id') or result.get('id')

@app.route("/api/authentication/profile")
def auth_profile():

    url = f"{BASE_URL}/user/profile"

    response = requests.get(url, headers=HEADERS)

    return jsonify(_unwrap_response(response))

@app.route("/api/authentication/logout")
def auth_logout():

    url = f"{BASE_URL}/logout"

    response = requests.delete(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/authentication/funds")
def auth_funds():

    url = f"{BASE_URL}/user/get-funds-and-margin"

    response = requests.get(url, headers=HEADERS)

    return jsonify(_normalize_funds(response))

@app.route("/api/auth/me")
def auth_me():
    return auth_profile()

@app.route("/api/auth/logout", methods=["GET", "POST"])
def auth_logout_alias():
    return auth_logout()

@app.route("/api/auth/funds")
def auth_funds_alias():
    return auth_funds()

# =====================================================
# USER APIs
# =====================================================

@app.route("/api/user")
def user():

    return jsonify({

        "User APIs": {

            "Profile": "/api/user/profile",
            "Funds": "/api/user/funds"
        }
    })

@app.route("/api/user/profile")
def user_profile():

    url = f"{BASE_URL}/user/profile"

    response = requests.get(url, headers=HEADERS)

    return jsonify(_unwrap_response(response))

@app.route("/api/user/funds")
def user_funds():

    url = f"{BASE_URL}/user/get-funds-and-margin"

    response = requests.get(url, headers=HEADERS)

    return jsonify(_unwrap_response(response))

# =====================================================
# ORDERS APIs
# =====================================================

@app.route("/api/orders")
def orders():

    return jsonify({

        "Orders APIs": {

            "All Orders": "/api/orders/all",
            "Order History": "/api/orders/history/<order_id>",
            "Place Buy": "/api/orders/buy",
            "Place Sell": "/api/orders/sell",
            "Modify Order": "/api/orders/modify",
            "Cancel Order": "/api/orders/cancel/<order_id>",
            "Trades": "/api/orders/trades"
        }
    })

@app.route("/api/orders/all")
def all_orders():

    url = f"{BASE_URL}/order/retrieve-all"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/orders/history/<order_id>")
def order_history(order_id):

    url = f"{BASE_URL}/order/history?order_id={order_id}"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/orders/trades")
def trades():

    url = f"{BASE_URL}/order/trades/get-trades-for-day"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/orders/buy", methods=["POST"])
def buy_order():
    
  
   
     
    data = request.json or {}
    print("BUY ORDER PAYLOAD:", data)

    url = f"{BASE_URL}/order/place"
    symbol = data.get("symbol", "")
    instrument_key = data.get("instrument_key") or data.get("instrument_token")
    if not data.get("instrument_key") and isinstance(symbol, str):
        if symbol.startswith("NIFTY "):
            instrument_key = f"NSE_INDEX|Nifty {symbol[7:]}"
        elif symbol.startswith("BANKNIFTY "):
            instrument_key = f"NSE_INDEX|Nifty Bank {symbol[10:]}"
        elif symbol.startswith("FINNIFTY "):
            instrument_key = f"NSE_INDEX|Nifty Fin Service {symbol[10:]}"
        elif symbol.startswith("SENSEX"):
            instrument_key = f"BSE_INDEX|{symbol}"
        else:
            instrument_key = data.get("instrument_token") or symbol

    payload = {
        "quantity": data.get("quantity", 1),
        "product": data.get("product", "D"),
        "validity": data.get("validity", "DAY"),
        "price": data.get("price", 0),
        "tag": data.get("tag", "python"),
        "order_type": data.get("order_type", "MARKET"),
        "transaction_type": data.get("transaction_type", "BUY"),
        "disclosed_quantity": data.get("disclosed_quantity", 0),
        "trigger_price": data.get("trigger_price", 0),
        "is_amo": data.get("is_amo", False),
    }
    if instrument_key:
        payload["instrument_key"] = instrument_key
        # Upstox API expects camelCase keys for instrument identifiers
        payload["instrumentKey"] = instrument_key
    if data.get("instrument_token"):
        payload["instrument_token"] = data.get("instrument_token")
        payload["instrumentToken"] = data.get("instrument_token")

    # Log final payload sent to Upstox for debugging
    app.logger.info("UPSTOX REQUEST PAYLOAD: %s", json.dumps(payload))

    headers = {
        **HEADERS,
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, data=json.dumps(payload))
    result = response.json()

    if response.status_code >= 400 or (isinstance(result, dict) and (result.get("status") == "error" or result.get("errors"))):
        return jsonify({"status": "error", "message": result.get("message") or "Order placement failed", "detail": result}), response.status_code if response.status_code >= 400 else 502

    normalized = {
        "symbol": data.get("symbol"),
        "quantity": payload["quantity"],
        "price": payload["price"],
        "transaction_type": payload["transaction_type"],
        "order_type": payload["order_type"],
        "instrument_token": payload.get("instrument_token"),
        "id": _get_order_id(result),
        "raw": result,
    }
    normalized.update({k: v for k, v in result.items() if k not in normalized})
    return jsonify(normalized)

@app.route("/api/orders/sell", methods=["POST"])
def sell_order():
    data = request.json or {}
    print("SELL ORDER PAYLOAD:", data)

    url = f"{BASE_URL}/order/place"
    symbol = data.get("symbol", "")
    instrument_key = data.get("instrument_key") or data.get("instrument_token")
    if not data.get("instrument_key") and isinstance(symbol, str):
        if symbol.startswith("NIFTY "):
            instrument_key = f"NSE_INDEX|Nifty {symbol[7:]}"
        elif symbol.startswith("BANKNIFTY "):
            instrument_key = f"NSE_INDEX|Nifty Bank {symbol[10:]}"
        elif symbol.startswith("FINNIFTY "):
            instrument_key = f"NSE_INDEX|Nifty Fin Service {symbol[10:]}"
        elif symbol.startswith("SENSEX"):
            instrument_key = f"BSE_INDEX|{symbol}"
        else:
            instrument_key = data.get("instrument_token") or symbol

    payload = {
        "quantity": data.get("quantity", 1),
        "product": data.get("product", "D"),
        "validity": data.get("validity", "DAY"),
        "price": data.get("price", 0),
        "tag": data.get("tag", "python"),
        "order_type": data.get("order_type", "MARKET"),
        "transaction_type": data.get("transaction_type", "SELL"),
        "disclosed_quantity": data.get("disclosed_quantity", 0),
        "trigger_price": data.get("trigger_price", 0),
        "is_amo": data.get("is_amo", False),
    }
    if instrument_key:
        payload["instrument_key"] = instrument_key
        payload["instrumentKey"] = instrument_key
    if data.get("instrument_token"):
        payload["instrument_token"] = data.get("instrument_token")
        payload["instrumentToken"] = data.get("instrument_token")

    # Log final payload sent to Upstox for debugging
    app.logger.info("UPSTOX REQUEST PAYLOAD: %s", json.dumps(payload))

    headers = {
        **HEADERS,
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, data=json.dumps(payload))
    result = response.json()

    if response.status_code >= 400 or (isinstance(result, dict) and (result.get("status") == "error" or result.get("errors"))):
        return jsonify({"status": "error", "message": result.get("message") or "Order placement failed", "detail": result}), response.status_code if response.status_code >= 400 else 502

    normalized = {
        "symbol": data.get("symbol"),
        "quantity": payload["quantity"],
        "price": payload["price"],
        "transaction_type": payload["transaction_type"],
        "order_type": payload["order_type"],
        "instrument_token": payload.get("instrument_token"),
        "id": _get_order_id(result),
        "raw": result,
    }
    normalized.update({k: v for k, v in result.items() if k not in normalized})
    return jsonify(normalized)

@app.route("/api/orders/modify")
def modify_order():

    url = f"{BASE_URL}/order/modify"

    payload = {
        "order_id": "ORDER_ID",
        "quantity": 1,
        "price": 100,
        "trigger_price": 0,
        "validity": "DAY",
        "order_type": "LIMIT",
        "disclosed_quantity": 0
    }

    response = requests.put(
        url,
        headers=HEADERS,
        data=json.dumps(payload)
    )

    return jsonify(response.json())

@app.route("/api/orders/cancel/<order_id>")
def cancel_order(order_id):

    url = f"{BASE_URL}/order/cancel?order_id={order_id}"

    response = requests.delete(url, headers=HEADERS)

    return jsonify(response.json())

# =====================================================
# GTT APIs
# =====================================================

@app.route("/api/gtt")
def gtt():

    return jsonify({

        "GTT APIs": {

            "All GTT": "/gtt/all"
        }
    })

@app.route("/api/gtt/all")
def all_gtt():

    url = f"{BASE_URL}/gtt/all"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

# =====================================================
# PORTFOLIO APIs
# =====================================================

@app.route("/api/portfolio")
def portfolio():

    url = f"{BASE_URL}/portfolio/long-term-holdings"
    response = requests.get(url, headers=HEADERS)
    payload = response.json()

    holdings = []
    if isinstance(payload, dict):
        if 'holdings' in payload and isinstance(payload['holdings'], list):
            holdings = payload['holdings']
        elif 'data' in payload and isinstance(payload['data'], list):
            holdings = payload['data']
        else:
            holdings = [payload] if payload else []
    elif isinstance(payload, list):
        holdings = payload

    total_invested = 0.0
    total_current = 0.0
    for h in holdings:
        invested = float(h.get('invested') or 0)
        current = float(h.get('current_value') or h.get('ltp') * h.get('quantity', 0) or 0)
        total_invested += invested
        total_current += current

    total_pnl = round(total_current - total_invested, 2)
    total_pnl_pct = round((total_pnl / total_invested) * 100, 2) if total_invested else 0.0

    return jsonify({
        'holdings': holdings,
        'summary': {
            'total_invested': round(total_invested, 2),
            'current_value': round(total_current, 2),
            'total_pnl': total_pnl,
            'total_pnl_pct': total_pnl_pct,
            'available_funds': 0.0,
        },
    })

@app.route("/api/portfolio/holdings")
def holdings():

    url = f"{BASE_URL}/portfolio/long-term-holdings"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/portfolio/positions")
def positions():

    url = f"{BASE_URL}/portfolio/short-term-positions"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

# =====================================================
# MARKET QUOTE APIs
# =====================================================

@app.route("/api/marketquote")
def marketquote():

    return jsonify({

        "Market Quote APIs": {

            "LTP": "/api/marketquote/ltp",
            "OHLC": "/api/marketquote/ohlc",
            "Full Quote": "/api/marketquote/full"
        }
    })

@app.route("/api/market/indices")
def indices():

    url = "https://api.upstox.com/v2/market-quote/ltp"

    params = {
        "instrument_key": "NSE_INDEX|Nifty 50,NSE_INDEX|Nifty Bank"
    }

    response = requests.get(
        url,
        headers=HEADERS,
        params=params
    )

    data = response.json()
    payload = data.get("data", {}) or {}

    # Normalize Upstox response into a key->item map.
    if isinstance(payload, list):
        normalized = {}
        for item in payload:
            key = item.get("instrument_key") or item.get("symbol") or item.get("instrumentKey")
            if key:
                normalized[key] = item
        payload = normalized

    def item_for(key):
        item = (payload.get(key) if isinstance(payload, dict) else {}) or {}
        last_price = (
            item.get("last_price")
            or item.get("ltp")
            or item.get("last_traded_price")
            or item.get("last_price")
            or item.get("close_price")
            or item.get("open_price")
            or 0
        )
        prev_close = item.get("close_price") or item.get("prev_close") or item.get("previous_close") or 0
        change = last_price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        return {
            "ltp": last_price,
            "change": change,
            "change_pct": change_pct
        }

    nifty = item_for("NSE_INDEX|Nifty 50")
    banknifty = item_for("NSE_INDEX|Nifty Bank")

    return jsonify([
        {
            "symbol": "NIFTY 50",
            "ltp": nifty["ltp"],
            "change": nifty["change"],
            "change_pct": nifty["change_pct"]
        },
        {
            "symbol": "BANKNIFTY",
            "ltp": banknifty["ltp"],
            "change": banknifty["change"],
            "change_pct": banknifty["change_pct"]
        }
    ])
    
@app.route("/api/market/watchlist")
def watchlist():
    return jsonify([])
    



@app.route("/api/market/option-chain")
def option_chain():
    try:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {ACCESS_TOKEN}"
        }

        url = "https://api.upstox.com/v2/option/chain"

        expiry_url = "https://api.upstox.com/v2/option/contract?instrument_key=NSE_INDEX|Nifty 50"

        expiry_res = requests.get(expiry_url, headers=headers)

        expiry_data = expiry_res.json()

        # helper to detect invalid-token / auth failure in Upstox responses
        def _is_invalid_token(resp_json, status_code=None):
            try:
                if status_code == 401:
                    return True
            except Exception:
                pass
            if not isinstance(resp_json, dict):
                return False
            if resp_json.get("status") == "error":
                # check typical error payloads for invalid-token clues
                errs = resp_json.get("errors") or []
                for e in errs:
                    code = (e.get("errorCode") or e.get("error_code") or "")
                    msg = (e.get("message") or "").lower()
                    if code == "UDAPI100050" or "invalid token" in msg or "invalid access token" in msg:
                        return True
            return False

        # If expiry fetch shows invalid token, try refreshing once then retry
        if _is_invalid_token(expiry_data, expiry_res.status_code):
            try:
                refresh_access_token()
                headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
                expiry_res = requests.get(expiry_url, headers=headers)
                expiry_data = expiry_res.json()
            except Exception:
                pass

        # If still invalid or no data, return upstream error to client
        if not isinstance(expiry_data, dict) or "data" not in expiry_data or not isinstance(expiry_data.get("data"), list) or not expiry_data.get("data"):
            app.logger.warning("option_chain: expiry fetch failed")
            return jsonify({"status": "error", "message": "Failed to fetch expiry contracts", "detail": expiry_data}), 502

        expiries = sorted(list(set([
            item["expiry"]
            for item in expiry_data["data"]
        ])))

        nearest_expiry = expiries[0]

        params = {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "expiry_date": nearest_expiry
        }

        r = requests.get(url, headers=headers, params=params)

        data = r.json()

        # If option chain returned auth error, try refresh+retry once
        if _is_invalid_token(data, r.status_code):
            try:
                refresh_access_token()
                headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
                r = requests.get(url, headers=headers, params=params)
                data = r.json()
            except Exception:
                pass

        if not isinstance(data, dict) or "data" not in data or not isinstance(data.get("data"), list):
            app.logger.warning("option_chain: option chain fetch failed")
            return jsonify({"status": "error", "message": "Failed to fetch option chain", "detail": data}), 502

        print(data)

        rows = []

        for item in data.get("data", []):

            lot_size = 65

            rows.append({
                "strike": item.get("strike_price", 0),

                        "ce": {
                    "ltp": item.get("call_options", {}).get("market_data", {}).get("ltp", 0),
                    "change_pct": item.get("call_options", {}).get("option_greeks", {}).get("delta", 0),
                    "oi": item.get("call_options", {}).get("market_data", {}).get("oi", 0),
                    "iv": item.get("call_options", {}).get("option_greeks", {}).get("iv", 0),
                    "instrument_key": item.get("call_options", {}).get("instrument_key"),
                    "instrument_token": item.get("call_options", {}).get("instrument_token"),
                },

                "pe": {
                    "ltp": item.get("put_options", {}).get("market_data", {}).get("ltp", 0),
                    "change_pct": item.get("put_options", {}).get("option_greeks", {}).get("delta", 0),
                    "oi": item.get("put_options", {}).get("market_data", {}).get("oi", 0),
                    "iv": item.get("put_options", {}).get("option_greeks", {}).get("iv", 0),
                    "instrument_key": item.get("put_options", {}).get("instrument_key"),
                    "instrument_token": item.get("put_options", {}).get("instrument_token"),
                },

                "lot_size": lot_size,
            })

        spot = data["data"][0]["underlying_spot_price"] if data.get("data") else 0
        expiry = data["data"][0]["expiry"] if data.get("data") else ""
        strikes = [item.get("strike_price", 0) for item in data.get("data", [])]
        atm_strike = min(strikes, key=lambda s: abs(s - spot)) if strikes else 0

        return jsonify({
            "spot": spot,
            "expiry": expiry,
            "atm_strike": atm_strike,
            "rows": rows
        })
    except Exception as e:
        app.logger.exception("option_chain handler failed")
        return jsonify({"status": "error", "message": "Option chain handler failed", "detail": str(e)}), 500

@app.route("/api/market/candles/<path:symbol>")
def market_candles(symbol):
    interval = request.args.get("interval", "1minute")
    count = int(request.args.get("count", 60))
    base_price = 100.0
    if " NIFTY" in symbol or " BANKNIFTY" in symbol or " SENSEX" in symbol:
        base_price = 18000.0
    elif " CE" in symbol or " PE" in symbol:
        base_price = 100.0
    vol = 0.004 if " CE" not in symbol and " PE" not in symbol else 0.012
    interval_minutes = {
        "1minute": 1,
        "5minute": 5,
        "15minute": 15,
        "30minute": 30,
        "day": 1440,
        "week": 10080,
        "month": 43200,
    }.get(interval, 1)
    n = max(5, min(count, 200))
    candles = []
    price = base_price
    now = datetime.now(timezone.utc)
    for i in range(n):
        ts = now - timedelta(minutes=interval_minutes * (n - i - 1))
        o = price
        change = random.uniform(-vol, vol) * o
        c = round(o + change, 2)
        h = round(max(o, c) + abs(random.uniform(0, vol / 2)) * o, 2)
        l = round(min(o, c) - abs(random.uniform(0, vol / 2)) * o, 2)
        v = random.randint(10000, 500000)
        candles.append({
            "ts": ts.isoformat(),
            "open": round(o, 2),
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })
        price = c
    return jsonify({"symbol": symbol, "interval": interval, "candles": candles})

@app.route("/api/marketquote/ltp")
def ltp():

    instrument = "NSE_EQ|INE848E01016"

    url = f"{BASE_URL}/market-quote/ltp?instrument_key={instrument}"

    response = requests.get(url, headers=HEADERS)

    print(response.json())

    return jsonify(response.json())

@app.route("/api/marketquote/ohlc")
def ohlc():

    instrument = "NSE_EQ|INE848E01016"

    url = f"{BASE_URL}/market-quote/ohlc?instrument_key={instrument}&interval=1d"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/marketquote/full")
def full_quote():

    instrument = "NSE_EQ|INE848E01016"

    url = f"{BASE_URL}/market-quote/quotes?instrument_key={instrument}"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

# =====================================================
# HISTORICAL APIs
# =====================================================

@app.route("/api/historical")
def historical():

    return jsonify({

        "Historical APIs": {

            "Daily Candle": "/api/historical/daily",
            "Intraday Candle": "/api/historical/intraday"
        }
    })

@app.route("/api/historical/daily")
def daily_history():

    url = f"{BASE_URL}/historical-candle/NSE_EQ|INE848E01016/day/2025-01-01/2024-01-01"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/historical/intraday")
def intraday_history():

    url = f"{BASE_URL}/historical-candle/intraday/NSE_EQ|INE848E01016/1minute"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

# =====================================================
# OPTION CHAIN APIs
# =====================================================

@app.route("/api/optionchain")
def optionchain():

    return jsonify({

        "Option APIs": {

            "Nifty": "/api/optionchain/nifty"
        }
    })

@app.route("/api/optionchain/nifty")
def nifty_option():

    url = f"{BASE_URL}/option/chain"

    params = {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-05-19"
    }

    response = requests.get(
        url,
        headers=HEADERS,
        params=params
    )
    print(response.json())

    data = response.json()

    return jsonify(response.json())

# =====================================================
# MARKET INFO APIs
# =====================================================

@app.route("/api/marketinfo")
def marketinfo():

    return jsonify({

        "Market Info APIs": {

            "Status": "/api/marketinfo/status"
        }
    })

@app.route("/api/marketinfo/status")
def market_status():

    url = f"{BASE_URL}/market/status/NSE"

    response = requests.get(url, headers=HEADERS)

    return jsonify(response.json())

# =====================================================
# INSTRUMENT APIs
# =====================================================

@app.route("/api/instruments")
def instruments():

    return jsonify({

        "Instrument APIs": {

            "NSE": "/api/instruments/nse"
        }
    })

@app.route("/api/instruments/nse")
def instrument_nse():

    return jsonify({
        "download": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    })

# =====================================================
# EXPIRED APIs
# =====================================================

@app.route("/api/expired")
def expired():

    return jsonify({

        "Expired Instrument APIs": {

            "Expired Contracts": "/api/expired/contracts"
        }
    })

@app.route("/api/expired/contracts")
def expired_contracts():

    return jsonify({
        "message": "Expired contracts endpoint"
    })

# =====================================================
# WEBSOCKET APIs
# =====================================================

@app.route("/api/websocket")
def websocket():

    return jsonify({

        "Websocket APIs": {

            "Market Feed": "wss://api.upstox.com/v2/feed/market-data-feed"
        }
    })

@app.route("/ws/market")
def ws_market():
    return jsonify({
        "status": "websocket ok"
    })

# =====================================================
# WEBHOOK APIs
# =====================================================

@app.route("/api/webhook")
def webhook():

    return jsonify({

        "Webhook APIs": {

            "Webhook URL": "Configure your webhook endpoint here"
        }
    })

# =====================================================
# SANDBOX APIs
# =====================================================

@app.route("/api/sandbox")
def sandbox():

    return jsonify({

        "Sandbox APIs": {

            "Sandbox Mode": "Enabled"
        }
    })

# =====================================================
# NEWS APIs
# =====================================================

@app.route("/api/news")
def news():

    return jsonify({
        "message": "News APIs section"
    })

# =====================================================
# FUNDAMENTALS APIs
# =====================================================

@app.route("/api/fundamentals")
def fundamentals():

    return jsonify({
        "message": "Fundamentals APIs section"
    })

# =====================================================
# CHARGES APIs
# =====================================================

@app.route("/api/charges")
def charges():

    return jsonify({
        "message": "Charges APIs section"
    })

# =====================================================
# MARGINS APIs
# =====================================================

@app.route("/api/margins")
def margins():

    return jsonify({
        "message": "Margins APIs section"
    })

# =====================================================
# PNL APIs
# =====================================================

@app.route("/api/pnl")
def pnl():

    return jsonify({
        "message": "Trade Profit Loss APIs section"
    })

# =====================================================
# MUTUAL FUND APIs
# =====================================================

@app.route("/api/mutualfund")
def mutualfund():

    return jsonify({
        "message": "Mutual Fund APIs section"
    })


# =====================================================
# START SERVER
# =====================================================

@app.route("/api/auth/upstox/login", methods=["GET", "POST"])
def upstox_login():
    from urllib.parse import quote

    redirect_uri = os.getenv("UPSTOX_REDIRECT_URI") or (request.host_url.rstrip('/') + '/api/auth/callback')
    callback_redirect = quote(redirect_uri)
    app_redirect_uri = request.args.get("app_redirect_uri", "") or "uptrade://auth"
    encoded_app_redirect = quote(app_redirect_uri)

    auth_url = (
        f"https://api-v2.upstox.com/login/authorization/dialog?response_type=code"
        f"&client_id=431300c1-0f1f-40a7-8968-6dfb147b2ba9"
        f"&redirect_uri={callback_redirect}"
        f"&state={encoded_app_redirect}"
    )

    return redirect(auth_url)

@app.route("/api/auth/callback")
def auth_callback():
    from urllib.parse import unquote

    code = request.args.get("code")
    state = request.args.get("state") or ""

    if not code:
        return jsonify({"status": "error", "message": "Missing authorization code"}), 400

    url = "https://api-v2.upstox.com/login/authorization/token"

    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    data = {
        "code": code,
        "client_id": "431300c1-0f1f-40a7-8968-6dfb147b2ba9",
        "client_secret": "q5dtkq1fr7",
        "redirect_uri": os.getenv("UPSTOX_REDIRECT_URI") or (request.host_url.rstrip('/') + '/api/auth/callback'),
        "grant_type": "authorization_code"
    }

    response = requests.post(url, headers=headers, data=data)
    token_data = response.json()

    print(token_data)

    global ACCESS_TOKEN
    global HEADERS

    if "access_token" in token_data:
        ACCESS_TOKEN = token_data["access_token"]
        HEADERS["Authorization"] = f"Bearer {ACCESS_TOKEN}"
        print("NEW TOKEN SAVED")

    target = unquote(state) if state else "uptrade://auth"
    if "access_token" in token_data:
        sep = '&' if '?' in target else '?'
        target = f"{target}{sep}access_token={ACCESS_TOKEN}"

    return f"""
    <html>
    <head>
    <script>
    window.location.href = {json.dumps(target)};
    </script>
    </head>
    <body></body>
    </html>
    """


@sock.route('/ws/market')
def market_ws(ws):

    from datetime import datetime
    import json
    subs: set = set()
    try:
        while True:
            try:
                msg = ws.receive(timeout=1)
            except Exception:
                msg = None

            if msg:
                try:
                    m = json.loads(msg)
                except Exception:
                    m = None
                if m and m.get('action') == 'subscribe':
                    for s in m.get('symbols', []):
                        subs.add(s)
                    ws.send(json.dumps({'type': 'ack', 'symbols': list(subs)}))
                elif m and m.get('action') == 'unsubscribe':
                    for s in m.get('symbols', []):
                        subs.discard(s)
                    ws.send(json.dumps({'type': 'ack', 'symbols': list(subs)}))

            # Periodically push ticks for subscribed symbols
            if subs:
                try:
                    if 'upstox_real' in globals() and upstox_real.is_configured():
                        # Map friendly symbols to Upstox instrument keys where possible
                        instr_keys = [upstox_real.INSTRUMENT_KEYS.get(s, s) for s in subs]
                        data = upstox_real.fetch_market_quote(ACCESS_TOKEN, instr_keys)
                        ticks = []
                        # data expected as dict keyed by instrument_key
                        for k, v in (data or {}).items():
                            # reverse map instrument key to friendly symbol if possible
                            sym = next((ks for ks, kv in upstox_real.INSTRUMENT_KEYS.items() if kv == k), k)
                            ltp = v.get('last_price') or v.get('ltp') or 0
                            ticks.append({'symbol': sym, 'ltp': ltp, 'ts': datetime.utcnow().isoformat()})
                        if ticks:
                            ws.send(json.dumps({'type': 'tick', 'data': ticks}))
                    else:
                        # Not configured for real Upstox — no live tick updates available
                        ticks = []
                    if ticks:
                        ws.send(json.dumps({'type': 'tick', 'data': ticks}))
                except Exception:
                    # swallow errors to keep connection alive
                    try:
                        ws.send(json.dumps({'type': 'error', 'message': 'tick-fetch-failed'}))
                    except Exception:
                        pass
    except Exception:
        # connection closed or unexpected error — just exit
        return


def _start_realtime_background():
    try:
        import threading
        from realtime import run_background

        def _runner():
            try:
                realtime_host = os.environ.get('REALTIME_WS_HOST', '0.0.0.0')
                realtime_port = int(os.environ.get('REALTIME_WS_PORT', '6789'))
                # pass current ACCESS_TOKEN so realtime can open Upstox WS when configured
                run_background(host=realtime_host, port=realtime_port, poll_interval=0.5, access_token=ACCESS_TOKEN)
            except Exception:
                pass

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
    except Exception:
        pass


@app.route('/api/realtime/token', methods=['POST'])
def api_realtime_token():
    data = request.json or {}
    access_token = data.get('access_token')
    if not access_token:
        return jsonify({'status': 'error', 'message': 'access_token is required'}), 400

    global ACCESS_TOKEN
    global HEADERS
    ACCESS_TOKEN = access_token
    HEADERS['Authorization'] = f'Bearer {ACCESS_TOKEN}'

    try:
        import realtime
        realtime.set_access_token(ACCESS_TOKEN)
    except Exception:
        pass

    # persist the access token so it survives server restarts
    try:
        _save_tokens_to_env(ACCESS_TOKEN)
    except Exception:
        pass

    return jsonify({'status': 'ok', 'access_token': 'updated'})


@app.route('/api/realtime/subscribe', methods=['POST'])
def api_realtime_subscribe():
    data = request.json or {}
    symbols = data.get('symbols', []) or []
    try:
        import realtime
        # add external subscriptions to the background realtime server
        realtime.add_external_subscriptions(symbols)
        return jsonify({'status': 'ok', 'subscribed': symbols})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/realtime/unsubscribe', methods=['POST'])
def api_realtime_unsubscribe():
    data = request.json or {}
    symbols = data.get('symbols', []) or []
    try:
        import realtime
        realtime.remove_external_subscriptions(symbols)
        return jsonify({'status': 'ok', 'unsubscribed': symbols})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


def get_lot_size(symbol):
    url = "https://api.upstox.com/v2/option/contract"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }

    params = {
        "instrument_key": f"NSE_INDEX|{symbol}"
    }

    r = requests.get(url, headers=headers, params=params)

    data = r.json()

    try:
        return data["data"][0]["lot_size"]
    except:
        return 0
    
@app.route("/api/orders/place", methods=["POST"])
def place_order():
    return jsonify({"msg": "route working"})

@app.route("/api/market/fno", methods=["GET", "OPTIONS"])
def get_fno():
    return jsonify({
        "status": "ok",
        "data": []
    })  

# =====================================================
# FUNDS APIs
# =====================================================

@app.get("/api/funds")
def funds():

    response = requests.get(
        "https://api.upstox.com/v2/user/get-funds-and-margin",
        headers=HEADERS
    )

    data = response.json()

    print(data)

    return jsonify(data)

@app.get("/api/market/indices")
def get_indices():

    url = "https://api.upstox.com/v2/market-quote/quotes"

    params = {
        "instrument_key":
        "NSE_INDEX|Nifty 50,NSE_INDEX|Nifty Bank"
    }

    r = requests.get(
        url,
        headers=HEADERS,
        params=params
    )

    data = r.json()

    nifty = data["data"]["NSE_INDEX|Nifty 50"]["last_price"]

    banknifty = data["data"]["NSE_INDEX|Nifty Bank"]["last_price"]

    return jsonify({
        "nifty": nifty,
        "banknifty": banknifty
    })


if __name__ == "__main__":
    threading.Thread(target=lambda: run_background(host='0.0.0.0', port=6789, poll_interval=0.5, access_token=ACCESS_TOKEN), daemon=True).start()

    app.run(host="0.0.0.0", port=5000)