"""Iteration 3 tests: WebSocket /api/ws/market + upstox_real module + .env placeholders."""
import os
import json
import asyncio
import pytest
import websockets

# Read backend public URL
BASE = None
with open('/app/frontend/.env') as f:
    for line in f:
        if line.startswith('EXPO_PUBLIC_BACKEND_URL='):
            BASE = line.split('=', 1)[1].strip().rstrip('/')

WS_URL = BASE.replace('https://', 'wss://').replace('http://', 'ws://') + '/api/ws/market'


# --- WebSocket tests ---
@pytest.mark.asyncio
async def test_ws_hello_message():
    """WS should send {type:'hello', mode:'mock'} on connect."""
    async with websockets.connect(WS_URL) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(raw)
        assert msg["type"] == "hello"
        assert msg["mode"] == "mock"


@pytest.mark.asyncio
async def test_ws_subscribe_and_receive_ticks():
    """Subscribe to symbols and receive tick broadcasts ~700ms cadence."""
    async with websockets.connect(WS_URL) as ws:
        # Consume hello
        await asyncio.wait_for(ws.recv(), timeout=5)
        # Subscribe
        await ws.send(json.dumps({"action": "subscribe", "symbols": ["NIFTY 50 22150 CE", "RELIANCE"]}))
        # Expect a 'subscribed' ack first
        ack = None
        tick = None
        # Read up to 5 frames within 5s
        for _ in range(5):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("type") == "subscribed":
                ack = m
                assert "NIFTY 50 22150 CE" in m["symbols"]
            elif m.get("type") == "tick":
                tick = m
                break
        assert ack is not None, "Did not get 'subscribed' ack"
        assert tick is not None, "Did not receive any tick within 5 frames"
        assert isinstance(tick["data"], list)
        # Each tick item has symbol, ltp, ts
        symbols_seen = {t["symbol"] for t in tick["data"]}
        assert symbols_seen.issubset({"NIFTY 50 22150 CE", "RELIANCE"})
        for t in tick["data"]:
            assert "ltp" in t and isinstance(t["ltp"], (int, float))
            assert "ts" in t


@pytest.mark.asyncio
async def test_ws_unsubscribe():
    """Unsubscribe removes symbols from active set."""
    async with websockets.connect(WS_URL) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5)  # hello
        await ws.send(json.dumps({"action": "subscribe", "symbols": ["RELIANCE"]}))
        await asyncio.wait_for(ws.recv(), timeout=3)  # subscribed ack
        await ws.send(json.dumps({"action": "unsubscribe", "symbols": ["RELIANCE"]}))
        # Drain until we get unsubscribed ack
        got_ack = False
        for _ in range(5):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("type") == "unsubscribed":
                got_ack = True
                assert "RELIANCE" not in m["symbols"]
                break
        assert got_ack


@pytest.mark.asyncio
async def test_ws_disconnect_no_crash():
    """Server should not crash when client disconnects; new client can still connect."""
    async with websockets.connect(WS_URL) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5)
    # Reconnect
    async with websockets.connect(WS_URL) as ws2:
        msg = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5))
        assert msg["type"] == "hello"


# --- upstox_real module tests ---
def test_upstox_real_module_exists_and_functions():
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("upstox_real", "/app/backend/upstox_real.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["upstox_real"] = mod
    spec.loader.exec_module(mod)
    for fn in [
        "build_authorization_url", "exchange_code_for_token",
        "fetch_profile", "fetch_funds", "fetch_market_quote",
        "place_order", "authorize_ws_feed", "is_configured",
    ]:
        assert hasattr(mod, fn), f"Missing function {fn} in upstox_real.py"


def test_upstox_is_configured_returns_false_with_placeholders():
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("upstox_real", "/app/backend/upstox_real.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["upstox_real"] = mod
    spec.loader.exec_module(mod)
    assert mod.is_configured() is False


def test_env_has_upstox_placeholders():
    """backend/.env contains UPSTOX_* with YOUR_* placeholders and USE_REAL_UPSTOX=false."""
    env = {}
    with open('/app/backend/.env') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    assert env.get("UPSTOX_API_KEY") == "YOUR_API_KEY"
    assert env.get("UPSTOX_API_SECRET") == "YOUR_API_SECRET"
    assert env.get("UPSTOX_REDIRECT_URI") == "YOUR_REDIRECT_URI"
    assert env.get("USE_REAL_UPSTOX", "").lower() == "false"


def test_login_init_returns_mock_url():
    """In mock mode, /api/auth/upstox/login returns a generic upstox dialog URL (no client_id)."""
    import requests
    r = requests.post(f"{BASE}/api/auth/upstox/login")
    assert r.status_code == 200
    url = r.json()["authorization_url"]
    assert "upstox.com" in url
    # Mock URL should NOT contain client_id (real-mode marker)
    assert "client_id=" not in url, f"Expected mock URL without client_id, got {url}"
