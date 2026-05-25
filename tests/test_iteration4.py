"""Iteration 4 tests: optional access_token in WS subscribe; upstox_real protobuf skeleton."""
import os
import json
import asyncio
import importlib.util
import sys
import pytest
import websockets

BASE = None
with open('/app/frontend/.env') as f:
    for line in f:
        if line.startswith('EXPO_PUBLIC_BACKEND_URL='):
            BASE = line.split('=', 1)[1].strip().rstrip('/')

WS_URL = BASE.replace('https://', 'wss://').replace('http://', 'ws://') + '/api/ws/market'


def _load_upstox_real():
    spec = importlib.util.spec_from_file_location("upstox_real", "/app/backend/upstox_real.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["upstox_real"] = mod
    spec.loader.exec_module(mod)
    return mod


# --- upstox_real protobuf scaffolding ---
def test_upstox_real_has_protobuf_scaffolding():
    mod = _load_upstox_real()
    # protobuf flag
    assert hasattr(mod, 'HAS_PB2')
    # iter5: pb2 file is now generated, so HAS_PB2 must be True
    assert mod.HAS_PB2 is True, "HAS_PB2 must be True now that MarketDataFeed_pb2 is generated"
    # Decoder + helpers
    for fn in ["_decode_feed_frame", "get_instrument_key", "stream_ticks"]:
        assert hasattr(mod, fn), f"Missing {fn} in upstox_real"
    # INSTRUMENT_KEYS dict
    assert isinstance(mod.INSTRUMENT_KEYS, dict)
    for key in ["NIFTY 50", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
        assert key in mod.INSTRUMENT_KEYS


def test_decode_feed_frame_no_pb2_returns_empty():
    mod = _load_upstox_real()
    # Should not raise and should return [] when HAS_PB2 False
    result = mod._decode_feed_frame(b"\x00\x01\x02garbage")
    assert result == []


def test_get_instrument_key_mapping():
    mod = _load_upstox_real()
    assert mod.get_instrument_key("NIFTY 50") == "NSE_INDEX|Nifty 50"
    assert mod.get_instrument_key("BANKNIFTY") == "NSE_INDEX|Nifty Bank"
    assert mod.get_instrument_key("FINNIFTY") == "NSE_INDEX|Nifty Fin Service"
    assert mod.get_instrument_key("UNKNOWN_SYM") is None


# --- WS access_token field accepted in subscribe (mock mode ignores it) ---
@pytest.mark.asyncio
async def test_ws_subscribe_with_access_token_ignored_in_mock():
    """Subscribe with optional access_token field must not break mock mode."""
    async with websockets.connect(WS_URL) as ws:
        hello_raw = await asyncio.wait_for(ws.recv(), timeout=5)
        hello = json.loads(hello_raw)
        assert hello["type"] == "hello"
        assert hello["mode"] == "mock"

        # Send subscribe with extra access_token field
        await ws.send(json.dumps({
            "action": "subscribe",
            "symbols": ["NIFTY 50", "BANKNIFTY"],
            "access_token": "upx_mock_dummy_token_for_future_real_mode",
        }))

        # Expect 'subscribed' ack and then 'tick' broadcasts (~700ms cadence)
        ack = None
        tick = None
        for _ in range(6):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("type") == "subscribed":
                ack = m
            elif m.get("type") == "tick":
                tick = m
                break
        assert ack is not None
        assert "NIFTY 50" in ack["symbols"]
        assert tick is not None, "No tick received despite mock pump running"
        symbols_in_ticks = {t["symbol"] for t in tick["data"]}
        assert symbols_in_ticks.issubset({"NIFTY 50", "BANKNIFTY"})


@pytest.mark.asyncio
async def test_ws_tick_cadence_under_one_second():
    """Mock pump should send ticks roughly every 700ms."""
    async with websockets.connect(WS_URL) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5)  # hello
        await ws.send(json.dumps({"action": "subscribe", "symbols": ["NIFTY 50"]}))
        # Drain ack
        ticks_received = 0
        import time
        t0 = time.time()
        while time.time() - t0 < 3.0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("type") == "tick":
                ticks_received += 1
        # Expect ~3-4 ticks in 3 seconds (700ms cadence)
        assert ticks_received >= 2, f"Expected >=2 ticks in 3s, got {ticks_received}"
