# Full Upstox API Portal - server.py
from flask_cors import CORS
from flask import Flask, jsonify, request, redirect
import requests
import json
import os
import random
import base64
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask_sock import Sock
from realtime import run_background
import threading
import logging

# Import position exit manager
import position_exit_manager

ACTIVE_POSITIONS = {}

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Configure logging
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

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
TOKEN_SOURCE = "env" if os.getenv("ACCESS_TOKEN") else "startup_fallback"
TOKEN_UPDATED_AT = datetime.now(timezone.utc)
LATEST_LOGIN_ACCESS_TOKEN = None
LATEST_LOGIN_RECEIVED_AT = None
LATEST_LOGIN_METADATA = {}

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {ACCESS_TOKEN}"
}

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN") or "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI1SkI5M0wiLCJqdGkiOiI2YTA4NzM5YWJiODUwMDZjZWNmYjZlNTIiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlzRXh0ZW5kZWQiOnRydWUsImlhdCI6MTc3ODkzODc3OCwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxODEwNTA0ODAwfQ.NQdkTjbNHnhycI4PqmUqWqCGL9LnaxdSZ7iTtLnh67k"
CLIENT_ID = "431300c1-0f1f-40a7-8968-6dfb147b2ba9"
CLIENT_SECRET = "q5dtkq1fr7"


def _decode_jwt_payload(token):
    if not token or '.' not in token:
        return {}

    try:
        payload = token.split('.')[1]
        padding = '=' * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode('utf-8')
        return json.loads(decoded)
    except Exception:
        return {}


def _token_metadata(token):
    payload = _decode_jwt_payload(token)
    iat = payload.get('iat')
    exp = payload.get('exp')
    now = datetime.now(timezone.utc).timestamp()

    def _format_timestamp(value):
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        return None

    expiry = _format_timestamp(exp)
    issued_at = _format_timestamp(iat)
    return {
        'issued_at': issued_at,
        'expires_at': expiry,
        'expired': bool(exp and now >= exp),
        'token_fingerprint': token[:10] + '...' + token[-10:] if token else None,
    }


def _set_token_state(token, source, *, record_login=False):
    global ACCESS_TOKEN, HEADERS, TOKEN_SOURCE, TOKEN_UPDATED_AT, LATEST_LOGIN_ACCESS_TOKEN, LATEST_LOGIN_RECEIVED_AT, LATEST_LOGIN_METADATA

    ACCESS_TOKEN = token
    HEADERS['Authorization'] = f'Bearer {ACCESS_TOKEN}'
    TOKEN_SOURCE = source
    TOKEN_UPDATED_AT = datetime.now(timezone.utc)

    if record_login:
        LATEST_LOGIN_ACCESS_TOKEN = token
        LATEST_LOGIN_RECEIVED_AT = TOKEN_UPDATED_AT
        LATEST_LOGIN_METADATA = _token_metadata(token)
        try:
            _save_tokens_to_env(ACCESS_TOKEN)
        except Exception:
            pass


def _extract_bearer_token(headers):
    auth_header = headers.get('Authorization') or headers.get('authorization')
    if isinstance(auth_header, str) and auth_header.startswith('Bearer '):
        return auth_header.split(' ', 1)[1]
    return None


def _sync_token_to_latest_login():
    global ACCESS_TOKEN, HEADERS, TOKEN_SOURCE, TOKEN_UPDATED_AT

    if not LATEST_LOGIN_ACCESS_TOKEN:
        return False

    if ACCESS_TOKEN != LATEST_LOGIN_ACCESS_TOKEN:
        ACCESS_TOKEN = LATEST_LOGIN_ACCESS_TOKEN
        HEADERS['Authorization'] = f"Bearer {ACCESS_TOKEN}"
        TOKEN_SOURCE = 'login'
        TOKEN_UPDATED_AT = datetime.now(timezone.utc)
        return True

    return False


def _log_token_context(endpoint, headers):
    _sync_token_to_latest_login()
    if headers is not None:
        headers['Authorization'] = f"Bearer {ACCESS_TOKEN}"

    bearer_token = _extract_bearer_token(headers)
    token_meta = _token_metadata(bearer_token) if bearer_token else {}
    latest_login_token_recorded = bool(LATEST_LOGIN_ACCESS_TOKEN)
    same_as_latest_login = bool(bearer_token and LATEST_LOGIN_ACCESS_TOKEN and bearer_token == LATEST_LOGIN_ACCESS_TOKEN)

    app.logger.info(
        "ORDER_TOKEN_CONTEXT endpoint=%s token_source=%s token_issue_time=%s token_expiry=%s token_expired=%s latest_login_token_recorded=%s latest_login_received_at=%s latest_login_issue_time=%s latest_login_expiry=%s same_as_latest_login=%s",
        endpoint,
        TOKEN_SOURCE,
        token_meta.get('issued_at'),
        token_meta.get('expires_at'),
        token_meta.get('expired'),
        latest_login_token_recorded,
        LATEST_LOGIN_RECEIVED_AT.isoformat() if isinstance(LATEST_LOGIN_RECEIVED_AT, datetime) else None,
        LATEST_LOGIN_METADATA.get('issued_at') if isinstance(LATEST_LOGIN_METADATA, dict) else None,
        LATEST_LOGIN_METADATA.get('expires_at') if isinstance(LATEST_LOGIN_METADATA, dict) else None,
        same_as_latest_login,
    )


def refresh_access_token():
    global ACCESS_TOKEN
    global HEADERS

    url = "https://api-v2.upstox.com/login/authorization/token"

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
        _set_token_state(data["access_token"], "refresh")

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


def _log_upstox_payload(label, payload):
    print(f"UPSTOX {label} RESPONSE:")
    try:
        print(json.dumps(payload, indent=2, default=str))
    except TypeError:
        print(payload)


def _normalize_profile(payload):
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)

    user_id = (
        payload.get('user_id')
        or payload.get('userId')
        or payload.get('client_id')
        or payload.get('clientId')
        or payload.get('id')
    )
    user_name = (
        payload.get('user_name')
        or payload.get('userName')
        or payload.get('name')
        or payload.get('full_name')
    )
    email = (
        payload.get('email')
        or payload.get('email_id')
        or payload.get('emailAddress')
    )
    client_id = (
        payload.get('client_id')
        or payload.get('clientId')
    )

    if user_id is not None:
        normalized['user_id'] = user_id
    if user_name is not None:
        normalized['user_name'] = user_name
    if email is not None:
        normalized['email'] = email
    if client_id is not None:
        normalized['client_id'] = client_id

    normalized['broker'] = payload.get('broker') or 'UPSTOX'
    normalized['exchanges'] = payload.get('exchanges') or payload.get('exchange') or []
    normalized['products'] = payload.get('products') or []
    normalized['order_types'] = payload.get('order_types') or payload.get('orderTypes') or []
    normalized['poa'] = payload.get('poa')
    normalized['ddpi'] = payload.get('ddpi')
    normalized['is_active'] = payload.get('is_active')
    normalized['user_type'] = payload.get('user_type') or payload.get('userType')

    return normalized


def _get_order_id(result):
    if not isinstance(result, dict):
        return None
    data = result.get('data')
    if isinstance(data, dict):
        return data.get('order_id') or data.get('client_order_id') or data.get('id')
    return result.get('order_id') or result.get('client_order_id') or result.get('id')


def _is_near_expiry_contract(expiry_date_str):
    """
    Check if a contract is near expiry (within 24 hours).
    Returns (is_near_expiry, days_remaining)
    """
    if not expiry_date_str:
        return False, None
    
    try:
        from datetime import datetime as dt
        expiry = dt.strptime(expiry_date_str, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        days_remaining = (expiry - today).days
        is_near_expiry = days_remaining <= 1
        return is_near_expiry, days_remaining
    except Exception:
        return False, None


def _build_instrument_validation_error(instrument_key, symbol, expiry_date=None):
    """
    Build a helpful error message when instrument validation fails.
    """
    is_near_expiry, days = _is_near_expiry_contract(expiry_date)
    
    message = f"Instrument {instrument_key} (for {symbol}) is not available for trading"
    details = []
    
    if is_near_expiry and days is not None:
        details.append(f"This contract expires in {days} day(s) - near-expiry contracts have restricted trading")
    
    if details:
        message += ". " + "; ".join(details) + "."
    
    return {
        "status": "error",
        "message": message,
        "error_code": "INSTRUMENT_UNAVAILABLE",
        "instrument_key": instrument_key,
        "symbol": symbol,
        "expiry_date": expiry_date,
        "is_near_expiry": is_near_expiry,
        "days_to_expiry": days,
    }


def _extract_upstox_error_payload(response, fallback_message="Order placement failed"):
    response_body = ''
    try:
        response_body = response.text
    except Exception:
        response_body = ''

    parsed = None
    try:
        parsed = response.json()
    except ValueError:
        parsed = None

    message = fallback_message
    error_code = None
    
    if isinstance(parsed, dict):
        if isinstance(parsed.get('errors'), list):
            for err in parsed['errors']:
                if isinstance(err, dict):
                    nested_message = err.get('message') or err.get('errorMessage') or err.get('error_message')
                    if nested_message:
                        message = nested_message
                    err_code = err.get('errorCode') or err.get('error_code')
                    if err_code:
                        error_code = err_code
                        # Add helpful context for specific error codes
                        if err_code == "UDAPI100060":
                            message = f"{message} - This instrument is no longer available for trading (may be expired, delisted, or not yet available)"
                        elif err_code == "UDAPI100050":
                            message = f"{message} - Your session token has expired or is invalid. Please log in again"
                        break
        if message == fallback_message:
            message = parsed.get('message') or parsed.get('detail') or parsed.get('error') or fallback_message
        if isinstance(message, (dict, list)):
            try:
                message = json.dumps(message)
            except TypeError:
                message = str(message)

    elif response_body:
        message = response_body

    if not isinstance(parsed, dict):
        parsed = {}

    return {
        'status_code': getattr(response, 'status_code', 502),
        'message': message,
        'error_code': error_code,
        'response_body': response_body,
        'detail': parsed,
    }


def _build_upstox_error_response(error_details):
    detail = error_details['detail'] if isinstance(error_details.get('detail'), dict) else {}
    errors = detail.get('errors') if isinstance(detail.get('errors'), list) else []
    return {
        'status': 'error',
        'message': error_details['message'],
        'status_code': error_details['status_code'],
        'error_code': error_details.get('error_code'),
        'response_body': error_details['response_body'],
        'detail': detail,
        'upstream_response': detail,
        'errors': errors,
    }


def _normalize_order_identifiers(data):
    normalized = dict(data)
    instrument_key = normalized.get('instrument_key') or normalized.get('instrument_token')
    instrument_token = normalized.get('instrument_token')

    if not instrument_token and instrument_key:
        normalized['instrument_token'] = instrument_key
        instrument_token = instrument_key
    elif instrument_token and not instrument_key:
        normalized['instrument_key'] = instrument_token
        instrument_key = instrument_token

    return normalized


def _build_upstox_order_payload(data):
    normalized = _normalize_order_identifiers(data)
    instrument_token = normalized.get('instrument_token')
    instrument_key = normalized.get('instrument_key')

    payload = {
        'quantity': normalized.get('quantity', 1),
        'product': normalized.get('product', 'D'),
        'validity': normalized.get('validity', 'DAY'),
        'price': normalized.get('price', 0),
        'tag': normalized.get('tag', 'python'),
        'order_type': normalized.get('order_type', 'MARKET'),
        'transaction_type': normalized.get('transaction_type', 'BUY'),
        'disclosed_quantity': normalized.get('disclosed_quantity', 0),
        'trigger_price': normalized.get('trigger_price', 0),
        'is_amo': normalized.get('is_amo', False),
        'market_protection': normalized.get('market_protection', -1),
    }

    if instrument_token:
        payload['instrument_token'] = instrument_token
        payload['instrumentToken'] = instrument_token
    if instrument_key:
        payload['instrument_key'] = instrument_key
        payload['instrumentKey'] = instrument_key

    return payload


def _mask_auth_headers(headers):
    if not isinstance(headers, dict):
        return headers
    masked = dict(headers)
    if 'Authorization' in masked:
        masked['Authorization'] = 'Bearer <REDACTED>'
    return masked


# =====================================================
# POSITION EXIT MANAGEMENT - Callbacks
# =====================================================

def _place_automatic_sell_order(instrument_key: str, symbol: str, quantity: int, 
                                 exit_reason) -> dict:
    """
    Callback function for placing automatic SELL orders when stop loss or target is hit.
    Called by position_exit_manager module.
    
    Args:
        instrument_key: Upstox instrument key
        symbol: Trading symbol
        quantity: Position quantity to sell
        exit_reason: position_exit_manager.ExitReason enum value
    
    Returns:
        dict with order result
    """
    try:
        app.logger.info(
            "AUTO_SELL_CALLBACK_INVOKED | instrument_key=%s symbol=%s quantity=%s exit_reason=%s",
            instrument_key, symbol, quantity, exit_reason
        )
        
        url = f"{BASE_URL}/order/place"
        
        # Build SELL order payload
        payload = {
            'quantity': quantity,
            'product': 'D',  # Day trading
            'validity': 'DAY',
            'price': 0,
            'order_type': 'MARKET',  # Market order for immediate execution
            'transaction_type': 'SELL',
            'tag': f'auto_exit_{exit_reason.value}',
            'instrument_token': instrument_key,
            'instrument_key': instrument_key,
            'disclosed_quantity': 0,
            'trigger_price': 0,
            'is_amo': False,
            'market_protection': -1,
        }
        
        headers = {
            **HEADERS,
            "Content-Type": "application/json",
        }
        
        app.logger.info(
            "AUTO_SELL_ORDER_REQUEST | instrument_key=%s symbol=%s quantity=%s "
            "order_type=MARKET transaction_type=SELL payload=%s",
            instrument_key, symbol, quantity, json.dumps(payload)
        )
        
        # Place the order
        response = requests.post(url, headers=headers, json=payload)
        
        app.logger.info(
            "AUTO_SELL_ORDER_RESPONSE | status_code=%s response=%s",
            response.status_code, response.text
        )
        
        try:
            result = response.json()
        except ValueError:
            result = {"raw": response.text}
        
        # Check for errors
        if response.status_code >= 400 or (isinstance(result, dict) and 
                                           (result.get("status") == "error" or result.get("errors"))):
            error_details = _extract_upstox_error_payload(response, 
                                                          fallback_message=result.get("message") or "Auto sell order failed")
            error_payload = _build_upstox_error_response(error_details)
            
            app.logger.error(
                "AUTO_SELL_ORDER_FAILED | instrument_key=%s symbol=%s status_code=%s "
                "error_code=%s message=%s",
                instrument_key, symbol, error_payload["status_code"],
                error_payload.get("error_code"), error_payload["message"]
            )
            
            return {
                "status": "error",
                "message": error_payload["message"],
                "error_details": error_payload,
            }
        
        # Success - extract order ID
        order_id = _get_order_id(result)
        
        app.logger.info(
            "AUTO_SELL_ORDER_SUCCESS | instrument_key=%s symbol=%s order_id=%s exit_reason=%s",
            instrument_key, symbol, order_id, exit_reason
        )
        
        return {
            "status": "success",
            "order_id": order_id,
            "instrument_key": instrument_key,
            "symbol": symbol,
            "quantity": quantity,
            "exit_reason": str(exit_reason),
            "result": result,
        }
    
    except Exception as e:
        app.logger.exception(
            "AUTO_SELL_CALLBACK_ERROR | instrument_key=%s symbol=%s quantity=%s error=%s",
            instrument_key, symbol, quantity, str(e)
        )
        return {
            "status": "error",
            "message": f"Exception during auto sell: {str(e)}",
            "error": str(e),
        }


# =====================================================
# REAL-TIME MARKET DATA SUBSCRIPTION
# =====================================================

def _subscribe_to_position_instruments():
    """Subscribe to real-time market data for all active positions."""
    try:
        instruments = list(ACTIVE_POSITIONS.keys())
        if instruments:
            app.logger.info("SUBSCRIBE_TO_POSITIONS | instruments=%s", instruments)
            position_exit_manager.add_external_subscriptions(instruments)
    except Exception as e:
        app.logger.error("SUBSCRIBE_TO_POSITIONS_ERROR | error=%s", str(e))


@app.route("/api/internal/position-tick-update", methods=["POST"])
def internal_position_tick_update():
    """
    Internal endpoint for real-time server to push tick updates.
    Called by realtime.py when it receives LTP data.
    
    Expected JSON:
    {
        "instrument_key": "NSE_EQ|...",
        "ltp": 100.5
    }
    """
    try:
        data = request.json or {}
        instrument_key = data.get("instrument_key")
        ltp = data.get("ltp")
        
        if not instrument_key or ltp is None:
            return jsonify({"status": "error", "message": "Missing instrument_key or ltp"}), 400
        
        # Update position exit manager with current LTP
        position_exit_manager.update_ltp(instrument_key, ltp)
        
        return jsonify({"status": "ok"})
    
    except Exception as e:
        app.logger.error("POSITION_TICK_UPDATE_ERROR | error=%s", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/positions/status")
def positions_status():
    """Get status of all active positions and their exit management."""
    try:
        status = position_exit_manager.get_status()
        return jsonify(status)
    except Exception as e:
        app.logger.error("POSITIONS_STATUS_ERROR | error=%s", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/positions/<path:instrument_key>")
def position_details(instrument_key):
    """Get details for a specific position."""
    try:
        position = position_exit_manager.get_position(instrument_key)
        if position:
            return jsonify(position)
        else:
            return jsonify({"status": "not_found", "message": f"Position {instrument_key} not found"}), 404
    except Exception as e:
        app.logger.error("POSITION_DETAILS_ERROR | instrument_key=%s error=%s", instrument_key, str(e))
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/authentication/profile")
def auth_profile():

    url = f"{BASE_URL}/user/profile"

    response = requests.get(url, headers=HEADERS)
    payload = _unwrap_response(response)
    _log_upstox_payload('PROFILE', payload)

    return jsonify(_normalize_profile(payload))

@app.route("/api/authentication/logout")
def auth_logout():

    url = f"{BASE_URL}/logout"

    response = requests.delete(url, headers=HEADERS)

    return jsonify(response.json())

@app.route("/api/authentication/funds")
def auth_funds():

    url = f"{BASE_URL}/user/get-funds-and-margin"

    response = requests.get(url, headers=HEADERS)
    payload = _unwrap_response(response)
    _log_upstox_payload('FUNDS', payload)

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
    raw_body = request.get_data(as_text=True)
    data = request.json or {}
    app.logger.info("APP BUY REQUEST RECEIVED: headers=%s body=%s", _mask_auth_headers(dict(request.headers)), raw_body)
    app.logger.info("APP BUY ORDER PAYLOAD PARSED: %s", data)

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

    data = _normalize_order_identifiers(data)
    if instrument_key and not data.get("instrument_token"):
        data["instrument_token"] = instrument_key
    if instrument_key and not data.get("instrument_key"):
        data["instrument_key"] = instrument_key

    payload = _build_upstox_order_payload(data)

    headers = {
        **HEADERS,
        "Content-Type": "application/json",
    }

    _log_token_context("/api/orders/buy", headers)

    # Log final payload sent to Upstox for debugging
    app.logger.info("UPSTOX BUY REQUEST PAYLOAD: instrument_key=%s symbol=%s quantity=%s order_type=%s full_payload=%s", 
                    instrument_key, symbol, payload.get('quantity'), payload.get('order_type'), json.dumps(payload))

    response = requests.post(url, headers=headers, json=payload)

    result = response.json()

    if response.status_code < 400:
        ACTIVE_POSITIONS[instrument_key] = {
            "symbol": data.get("symbol"),
            "quantity": payload["quantity"],
            "stop_loss": data.get("stop_loss"),
            "target": data.get("target"),
            "instrument_key": instrument_key
        }

        print("POSITION SAVED:", ACTIVE_POSITIONS[instrument_key])
        
        # Add position to exit manager for automatic stop loss / target handling
        try:
            buy_price = payload.get("price")  # Could be 0 for market orders
            position_exit_manager.add_position(
                instrument_key=instrument_key,
                symbol=data.get("symbol"),
                quantity=payload["quantity"],
                stop_loss=data.get("stop_loss"),
                target=data.get("target"),
                buy_price=buy_price if buy_price > 0 else None
            )
            
            app.logger.info(
                "POSITION_ADDED_TO_EXIT_MANAGER | instrument_key=%s symbol=%s quantity=%s "
                "stop_loss=%s target=%s",
                instrument_key, data.get("symbol"), payload["quantity"],
                data.get("stop_loss"), data.get("target")
            )
        except Exception as e:
            app.logger.error(
                "FAILED_TO_ADD_POSITION_TO_EXIT_MANAGER | instrument_key=%s error=%s",
                instrument_key, str(e),
                exc_info=True
            )
    
    # Log the exact HTTP request (headers + body) that was sent to Upstox
    try:
        req = getattr(response, 'request', None)
        if req is not None:
            req_body = req.body
            if isinstance(req_body, bytes):
                try:
                    req_body = req_body.decode('utf-8')
                except Exception:
                    req_body = str(req_body)
            app.logger.info("UPSTOX SENT REQUEST: url=%s headers=%s body=%s", getattr(req, 'url', None), dict(getattr(req, 'headers', {})), req_body)
    except Exception as e:
        app.logger.error("Failed to log sent Upstox request: %s", e)

    app.logger.info("UPSTOX RESPONSE STATUS: %s", response.status_code)
    app.logger.info("UPSTOX RESPONSE BODY: %s", response.text)
    try:
        result = response.json()
    except ValueError:
        result = {"raw": response.text}

    if response.status_code >= 400 or (isinstance(result, dict) and (result.get("status") == "error" or result.get("errors"))):
        error_details = _extract_upstox_error_payload(response, fallback_message=result.get("message") or "Order placement failed")
        error_payload = _build_upstox_error_response(error_details)
        app.logger.error(
            "Upstox order placement failed | endpoint=%s | instrument_key=%s | symbol=%s | status_code=%s | error_code=%s | message=%s | response_body=%s | upstream_response=%s",
            "/api/orders/buy",
            instrument_key,
            symbol,
            error_payload["status_code"],
            error_payload.get("error_code"),
            error_payload["message"],
            error_payload["response_body"],
            json.dumps(error_payload["upstream_response"], default=str),
        )
        return jsonify(error_payload), error_payload["status_code"] if error_payload["status_code"] >= 400 else 502

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
    raw_body = request.get_data(as_text=True)
    data = request.json or {}
    app.logger.info("APP SELL REQUEST RECEIVED: headers=%s body=%s", _mask_auth_headers(dict(request.headers)), raw_body)
    app.logger.info("APP SELL ORDER PAYLOAD PARSED: %s", data)

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

    data = _normalize_order_identifiers(data)
    if instrument_key and not data.get("instrument_token"):
        data["instrument_token"] = instrument_key
    if instrument_key and not data.get("instrument_key"):
        data["instrument_key"] = instrument_key

    payload = _build_upstox_order_payload(data)

    headers = {
        **HEADERS,
        "Content-Type": "application/json",
    }

    _log_token_context("/api/orders/sell", headers)

    # Log final payload sent to Upstox for debugging
    app.logger.info("UPSTOX SELL REQUEST PAYLOAD: instrument_key=%s symbol=%s quantity=%s order_type=%s full_payload=%s", 
                    instrument_key, symbol, payload.get('quantity'), payload.get('order_type'), json.dumps(payload))

    response = requests.post(url, headers=headers, json=payload)
    
    # Log the exact HTTP request (headers + body) that was sent to Upstox
    try:
        req = getattr(response, 'request', None)
        if req is not None:
            req_body = req.body
            if isinstance(req_body, bytes):
                try:
                    req_body = req_body.decode('utf-8')
                except Exception:
                    req_body = str(req_body)
            app.logger.info("UPSTOX SENT REQUEST: url=%s headers=%s body=%s", getattr(req, 'url', None), dict(getattr(req, 'headers', {})), req_body)
    except Exception as e:
        app.logger.error("Failed to log sent Upstox request: %s", e)

    app.logger.info("UPSTOX RESPONSE STATUS: %s", response.status_code)
    app.logger.info("UPSTOX RESPONSE BODY: %s", response.text)
    try:
        result = response.json()
    except ValueError:
        result = {"raw": response.text}

    if response.status_code >= 400 or (isinstance(result, dict) and (result.get("status") == "error" or result.get("errors"))):
        error_details = _extract_upstox_error_payload(response, fallback_message=result.get("message") or "Order placement failed")
        error_payload = _build_upstox_error_response(error_details)
        app.logger.error(
            "Upstox order placement failed | endpoint=%s | instrument_key=%s | symbol=%s | status_code=%s | error_code=%s | message=%s | response_body=%s | upstream_response=%s",
            "/api/orders/sell",
            instrument_key,
            symbol,
            error_payload["status_code"],
            error_payload.get("error_code"),
            error_payload["message"],
            error_payload["response_body"],
            json.dumps(error_payload["upstream_response"], default=str),
        )
        return jsonify(error_payload), error_payload["status_code"] if error_payload["status_code"] >= 400 else 502

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
        app.logger.info("OPTION_CHAIN: nearest_expiry=%s | available_expiries=%s", nearest_expiry, expiries[:5])

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
            strike_price = item.get("strike_price", 0)
            expiry = item.get("expiry", "")
            
            ce = item.get("call_options", {})
            pe = item.get("put_options", {})
            
            ce_key = ce.get("instrument_key")
            pe_key = pe.get("instrument_key")

            rows.append({
                "strike": strike_price,

                        "ce": {
                    "ltp": item.get("call_options", {}).get("market_data", {}).get("ltp", 0),
                    "change_pct": item.get("call_options", {}).get("option_greeks", {}).get("delta", 0),
                    "oi": item.get("call_options", {}).get("market_data", {}).get("oi", 0),
                    "iv": item.get("call_options", {}).get("option_greeks", {}).get("iv", 0),
                    "instrument_key": ce_key,
                    "instrument_token": item.get("call_options", {}).get("instrument_token"),
                },

                "pe": {
                    "ltp": item.get("put_options", {}).get("market_data", {}).get("ltp", 0),
                    "change_pct": item.get("put_options", {}).get("option_greeks", {}).get("delta", 0),
                    "oi": item.get("put_options", {}).get("market_data", {}).get("oi", 0),
                    "iv": item.get("put_options", {}).get("option_greeks", {}).get("iv", 0),
                    "instrument_key": pe_key,
                    "instrument_token": item.get("put_options", {}).get("instrument_token"),
                },

                "lot_size": lot_size,
                "expiry": expiry,
            })
            
            # Log instrument keys for debugging
            if strike_price in [23350, 23400, 23450]:  # Log strikes near 23400
                app.logger.debug("OPTION_CHAIN_STRIKE: strike=%d ce_key=%s pe_key=%s expiry=%s", 
                               strike_price, ce_key, pe_key, expiry)

        spot = data["data"][0]["underlying_spot_price"] if data.get("data") else 0
        expiry = data["data"][0]["expiry"] if data.get("data") else ""
        strikes = [item.get("strike_price", 0) for item in data.get("data", [])]
        atm_strike = min(strikes, key=lambda s: abs(s - spot)) if strikes else 0

        app.logger.info("OPTION_CHAIN_SUMMARY: spot=%s expiry=%s atm_strike=%s total_strikes=%d", 
                       spot, expiry, atm_strike, len(rows))

        return jsonify({
            "spot": spot,
            "expiry": expiry,
            "atm_strike": atm_strike,
            "rows": rows
        })
    except Exception as e:
        app.logger.exception("option_chain handler failed")
        return jsonify({"status": "error", "message": "Option chain handler failed", "detail": str(e)}), 500


@app.route("/api/debug/validate-instrument/<path:instrument_key>", methods=["GET"])
def validate_instrument(instrument_key):
    """
    Debug endpoint to validate if an instrument_key is tradeable.
    Usage: GET /api/debug/validate-instrument/NSE_FO|57026
    """
    app.logger.info("VALIDATE_INSTRUMENT: instrument_key=%s", instrument_key)
    
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }
    
    # Try to get market quote for the instrument
    url = f"{BASE_URL}/market-quote/quotes"
    params = {"instrument_key": instrument_key}
    
    try:
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        
        if response.status_code >= 400:
            return jsonify({
                "status": "error",
                "message": f"Instrument {instrument_key} returned status {response.status_code}",
                "instrument_key": instrument_key,
                "response": data,
            }), response.status_code
        
        # If we got here, instrument exists
        if isinstance(data.get("data"), dict) and instrument_key in data["data"]:
            quote = data["data"][instrument_key]
            return jsonify({
                "status": "valid",
                "instrument_key": instrument_key,
                "ltp": quote.get("ltp") or quote.get("last_price"),
                "oi": quote.get("oi"),
                "volume": quote.get("volume"),
                "last_trade_time": quote.get("last_trade_time"),
                "quote": quote,
            })
        else:
            return jsonify({
                "status": "not_found",
                "message": f"Instrument {instrument_key} not found in market quote response",
                "instrument_key": instrument_key,
            }), 404
    
    except Exception as e:
        app.logger.exception("validate_instrument error")
        return jsonify({
            "status": "error",
            "message": str(e),
            "instrument_key": instrument_key,
        }), 500

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

    app.logger.info("AUTH_LOGIN_REQUEST: app_redirect_uri=%s redirect_uri=%s", app_redirect_uri, redirect_uri)
     
    return redirect(auth_url)

@app.route("/api/auth/callback")
def auth_callback():
    from urllib.parse import unquote

    code = request.args.get("code")
    state = request.args.get("state") or ""

    app.logger.info("AUTH_CALLBACK_RECEIVED: code=%s state=%s", code, state)

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

    token_fingerprint = None
    if isinstance(token_data, dict) and token_data.get('access_token'):
        token_fingerprint = token_data['access_token'][:10] + '...' + token_data['access_token'][-10:]
    app.logger.info("AUTH_CALLBACK_TOKEN_DATA: success=%s token_fingerprint=%s response_body=%s", 'access_token' in token_data, token_fingerprint, response.text)

    global ACCESS_TOKEN
    global HEADERS

    if "access_token" in token_data:
        _set_token_state(token_data["access_token"], "login", record_login=True)
        login_meta = _token_metadata(ACCESS_TOKEN)
        app.logger.info(
            "LOGIN_TOKEN_SAVED source=%s token_issue_time=%s token_expiry=%s token_received_at=%s",
            TOKEN_SOURCE,
            login_meta.get('issued_at'),
            login_meta.get('expires_at'),
            datetime.now(timezone.utc).isoformat(),
        )
        print("NEW TOKEN SAVED")
      

    target = unquote(state) if state else "uptrade://auth"
    if "access_token" in token_data:
        sep = '&' if '?' in target else '?'
        target = f"{target}{sep}access_token={ACCESS_TOKEN}"

    print("FINAL REDIRECT:", target) 

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
    raw_body = request.get_data(as_text=True)
    data = request.json or {}
    app.logger.info("APP PLACE_ORDER REQUEST RECEIVED: headers=%s body=%s", _mask_auth_headers(dict(request.headers)), raw_body)
    app.logger.info("APP PLACE_ORDER ORDER PAYLOAD PARSED: %s", data)
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
    # Start the real-time market data background server
    threading.Thread(target=lambda: run_background(host='0.0.0.0', port=6789, poll_interval=0.5, access_token=ACCESS_TOKEN), daemon=True).start()
    
    # Define subscription callback for position exit manager
    def subscribe_to_realtime_updates(instruments: list):
        """Subscribe to real-time updates for position instruments."""
        try:
            import realtime
            realtime.add_external_subscriptions(instruments)
        except Exception as e:
            app.logger.warning("Failed to subscribe to realtime updates: %s", str(e))
    
    # Initialize position exit manager
    try:
        app.logger.info("INITIALIZING_POSITION_EXIT_MANAGER")
        position_exit_manager.initialize_manager(
            place_sell_order_callback=_place_automatic_sell_order,
            active_positions_dict=ACTIVE_POSITIONS,
            subscribe_callback=subscribe_to_realtime_updates,
            poll_interval=0.5  # Check every 500ms for exit conditions
        )
        
        # Start the manager's monitoring loop
        manager = position_exit_manager.get_manager()
        if manager:
            manager.start()
            app.logger.info("POSITION_EXIT_MANAGER_INITIALIZED_AND_STARTED")
    except Exception as e:
        app.logger.error(
            "FAILED_TO_INITIALIZE_POSITION_EXIT_MANAGER | error=%s",
            str(e),
            exc_info=True
        )

    app.run(host="0.0.0.0", port=5000)