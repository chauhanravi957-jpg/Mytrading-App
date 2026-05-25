import asyncio
import threading
import time
import json
import pytest
import websockets

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import realtime
import upstox_real
import server


@pytest.mark.asyncio
async def test_realtime_server_polling_mock(monkeypatch):
    # Patch fetch_market_quote to return predictable data
    def fake_fetch(access_token, instrument_keys):
        return {
            instrument_keys[0]: {"last_price": 12345.5}
        }

    monkeypatch.setattr(upstox_real, 'fetch_market_quote', fake_fetch)
    monkeypatch.setattr(upstox_real, 'is_configured', lambda: True)

    async def fake_stream(access_token, instrument_keys, on_tick, stop_event=None):
            await asyncio.sleep(0.1)
            await on_tick([{"symbol": "NIFTY 50", "ltp": 12345.5, "ts": time.time()}])
            stop_event.set()

    monkeypatch.setattr(upstox_real, 'stream_ticks', fake_stream)

    # Start realtime background in a thread
    def bg():
        realtime.run_background(host='127.0.0.1', port=6790, poll_interval=0.2)

    t = threading.Thread(target=bg, daemon=True)
    t.start()

    # Give server a moment to start
    await asyncio.sleep(0.5)

    uri = 'ws://127.0.0.1:6790'
    async with websockets.connect(uri) as ws:
        # Send subscribe
        await ws.send(json.dumps({"action": "subscribe", "symbols": ["NIFTY 50"]}))
        # Wait for ack and ticks
        got_ack = False
        got_tick = False
        t0 = time.time()
        while time.time() - t0 < 5:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get('type') == 'ack' or msg.get('type') == 'subscribed':
                got_ack = True
            if msg.get('type') == 'tick':
                got_tick = True
                # check structure
                assert isinstance(msg['data'], list)
                assert msg['data'][0]['symbol'] == 'NIFTY 50'
                assert 'ltp' in msg['data'][0]
                break
        assert got_ack
        assert got_tick


def test_api_realtime_token_updates_access_token(monkeypatch):
    client = server.app.test_client()
    new_token = 'test_token_123'
    called = {'updated': False}

    def fake_set_access_token(token):
        called['updated'] = token == new_token

    monkeypatch.setattr(realtime, 'set_access_token', fake_set_access_token)

    response = client.post('/api/realtime/token', json={'access_token': new_token})
    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'ok'
    assert called['updated']
