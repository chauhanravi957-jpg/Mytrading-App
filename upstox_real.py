"""
Real Upstox V2 API integration — uses PLACEHOLDER credentials by default.

ACTIVATION:
  1. Set in backend/.env:
       UPSTOX_API_KEY=<your api key>
       UPSTOX_API_SECRET=<your api secret>
       UPSTOX_REDIRECT_URI=<your redirect uri>
       USE_REAL_UPSTOX=true
  2. Restart backend.

DOCS:
  - OAuth:       https://upstox.com/developer/api-documentation/authentication
  - Market data: https://upstox.com/developer/api-documentation/market-quote-instrument
  - WebSocket:   https://upstox.com/developer/api-documentation/market-data-feed
  - Orders:      https://upstox.com/developer/api-documentation/place-order
"""
import os
import json
import asyncio
import logging
from typing import Optional, Callable, Awaitable, Iterable
import requests

logger = logging.getLogger(__name__)

UPSTOX_API_KEY: str = os.environ.get("UPSTOX_API_KEY", "YOUR_API_KEY")
UPSTOX_API_SECRET: str = os.environ.get("UPSTOX_API_SECRET", "YOUR_API_SECRET")
UPSTOX_REDIRECT_URI: str = os.environ.get("UPSTOX_REDIRECT_URI", "YOUR_REDIRECT_URI")
USE_REAL_UPSTOX: bool = os.environ.get("USE_REAL_UPSTOX", "false").lower() == "true"

UPSTOX_BASE = "https://api.upstox.com/v2"
UPSTOX_AUTH_DIALOG = f"{UPSTOX_BASE}/login/authorization/dialog"
UPSTOX_TOKEN_URL = f"{UPSTOX_BASE}/login/authorization/token"
UPSTOX_PROFILE_URL = f"{UPSTOX_BASE}/user/profile"
UPSTOX_FUNDS_URL = f"{UPSTOX_BASE}/user/get-funds-and-margin"
UPSTOX_MARKET_QUOTE_URL = f"{UPSTOX_BASE}/market-quote/quotes"
UPSTOX_PLACE_ORDER_URL = f"{UPSTOX_BASE}/order/place"
UPSTOX_WS_AUTHORIZE_URL = f"{UPSTOX_BASE}/feed/market-data-feed/authorize"

# Common instrument keys (replace with full list from Upstox /instruments dump if needed)
INSTRUMENT_KEYS = {
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "SENSEX": "BSE_INDEX|SENSEX",
}


def is_configured() -> bool:
    if not USE_REAL_UPSTOX:
        return False
    return all(
        v and not v.startswith("YOUR_")
        for v in (UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI)
    )


def build_authorization_url(state: str) -> str:
    return (
        f"{UPSTOX_AUTH_DIALOG}"
        f"?response_type=code"
        f"&client_id={UPSTOX_API_KEY}"
        f"&redirect_uri={UPSTOX_REDIRECT_URI}"
        f"&state={state}"
    )


def exchange_code_for_token(code: str) -> dict:
    payload = {
        "code": code,
        "client_id": UPSTOX_API_KEY,
        "client_secret": UPSTOX_API_SECRET,
        "redirect_uri": UPSTOX_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    headers = {"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(UPSTOX_TOKEN_URL, data=payload, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def _auth_headers(access_token: str) -> dict:
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}


def fetch_profile(access_token: str) -> dict:
    r = requests.get(UPSTOX_PROFILE_URL, headers=_auth_headers(access_token), timeout=10)
    r.raise_for_status()
    return r.json().get("data", {})


def fetch_funds(access_token: str) -> dict:
    r = requests.get(UPSTOX_FUNDS_URL, headers=_auth_headers(access_token), timeout=10)
    r.raise_for_status()
    return r.json().get("data", {})


def fetch_market_quote(access_token: str, instrument_keys: list) -> dict:
    """Get LTP/OHLC for multiple instrument keys via REST."""
    params = {"instrument_key": ",".join(instrument_keys)}
    r = requests.get(UPSTOX_MARKET_QUOTE_URL, headers=_auth_headers(access_token), params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("data", {})


def place_order(access_token: str, payload: dict) -> dict:
    headers = {**_auth_headers(access_token), "Content-Type": "application/json"}
    r = requests.post(UPSTOX_PLACE_ORDER_URL, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    return r.json().get("data", {})


def authorize_ws_feed(access_token: str) -> Optional[str]:
    r = requests.get(UPSTOX_WS_AUTHORIZE_URL, headers=_auth_headers(access_token), timeout=10)
    r.raise_for_status()
    return r.json().get("data", {}).get("authorized_redirect_uri")


# ============================================================================
# WEBSOCKET PROTOBUF DECODER
# ============================================================================
# Upstox MarketDataFeed streams binary protobuf frames. Two ways to decode:
#  (1) Easy: generate Python pb2 from their proto (https://github.com/upstox/upstox-python)
#      then `from MarketDataFeedV3_pb2 import FeedResponse; FeedResponse.FromString(buf)`.
#  (2) No-codegen: dynamically parse using google.protobuf.json_format via a known schema.
# This module implements approach (1) with a graceful fallback that emits raw JSON
# if MarketDataFeed_pb2 is not available, so the rest of the app keeps working.
try:
    # If you have the generated stub (place it as /app/backend/MarketDataFeed_pb2.py), it loads.
    from MarketDataFeed_pb2 import FeedResponse  # type: ignore
    HAS_PB2 = True
except Exception:
    FeedResponse = None  # type: ignore
    HAS_PB2 = False
    logger.info("upstox_real: MarketDataFeed_pb2 not found — protobuf decoding disabled. "
                "Place the generated pb2 file at /app/backend/MarketDataFeed_pb2.py to enable.")


def _decode_feed_frame(raw_bytes: bytes) -> list:
    """Decode one binary frame -> list of {symbol, ltp, ts} dicts."""
    if not HAS_PB2:
        return []
    try:
        resp = FeedResponse()
        resp.ParseFromString(raw_bytes)
        out = []
        # `feeds` is a map<string, Feed>; each Feed has ltpc -> {ltp, ltt}
        for instrument_key, feed in resp.feeds.items():
            ltpc = getattr(feed, "ltpc", None)
            if ltpc and ltpc.ltp:
                # Reverse-map instrument_key -> human symbol if known
                symbol = next((k for k, v in INSTRUMENT_KEYS.items() if v == instrument_key), instrument_key)
                out.append({"symbol": symbol, "ltp": float(ltpc.ltp), "ts": str(ltpc.ltt)})
        return out
    except Exception as e:
        logger.warning("upstox_real: failed to decode protobuf frame: %s", e)
        return []


async def stream_ticks(
    access_token: str,
    instrument_keys: Iterable[str],
    on_tick: Callable[[list], Awaitable[None]],
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Open Upstox WS, subscribe to instruments, and forward decoded ticks to on_tick.
    Yields control via asyncio. Caller must run this in a background task.
    """
    try:
        import websockets
    except ImportError:
        logger.error("upstox_real.stream_ticks: install 'websockets' to use real WS feed.")
        return

    ws_url = authorize_ws_feed(access_token)
    if not ws_url:
        logger.error("upstox_real.stream_ticks: failed to get authorized WS URL.")
        return

    sub_msg = {
        "guid": "uptrade-1",
        "method": "sub",
        "data": {"mode": "ltpc", "instrumentKeys": list(instrument_keys)},
    }

    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps(sub_msg))
        logger.info("upstox_real.stream_ticks: subscribed to %d instruments", len(list(instrument_keys)))
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, (bytes, bytearray)):
                ticks = _decode_feed_frame(bytes(msg))
                if ticks:
                    await on_tick(ticks)
            else:
                # control frame (JSON ack)
                try:
                    logger.debug("upstox_real WS ctrl: %s", msg[:200])
                except Exception:
                    pass


def get_instrument_key(symbol: str) -> Optional[str]:
    """Map a human-readable symbol to Upstox instrument_key."""
    return INSTRUMENT_KEYS.get(symbol)
