"""Iteration 5 tests: protobuf decoder + candles endpoint + regression."""
import os
import sys
import pytest
import requests

BASE_URL = os.environ.get('EXPO_BACKEND_URL', '').rstrip('/') or \
           os.environ.get('EXPO_PUBLIC_BACKEND_URL', '').rstrip('/')
# Read from frontend/.env if missing
if not BASE_URL:
    try:
        with open('/app/frontend/.env') as f:
            for line in f:
                if line.startswith('EXPO_PUBLIC_BACKEND_URL='):
                    BASE_URL = line.split('=', 1)[1].strip().strip('"').rstrip('/')
                    break
    except Exception:
        pass

sys.path.insert(0, '/app/backend')


# ============== Protobuf decoder tests ==============
class TestProtobufDecoder:
    def test_pb2_file_exists(self):
        assert os.path.exists('/app/backend/MarketDataFeed.proto')
        assert os.path.exists('/app/backend/MarketDataFeed_pb2.py')

    def test_has_pb2_imported(self):
        import upstox_real
        assert upstox_real.HAS_PB2 is True, "MarketDataFeed_pb2 should import successfully"

    def test_decode_empty_frame(self):
        import upstox_real
        assert upstox_real._decode_feed_frame(b'') == []

    def test_decode_garbage(self):
        import upstox_real
        # Garbage bytes should not crash
        assert upstox_real._decode_feed_frame(b'\xff\xff\xff\xff') == []

    def test_decode_valid_ltpc_frame(self):
        """Build a FeedResponse with one LTPC entry; verify decoder returns symbol+ltp."""
        import upstox_real
        from MarketDataFeed_pb2 import FeedResponse, Feed, LTPC
        resp = FeedResponse()
        resp.currentTsMillis = 1700000000000
        feed = Feed()
        feed.ltpc.ltp = 22150.45
        feed.ltpc.ltt = 1700000000123
        resp.feeds['NSE_INDEX|Nifty 50'].CopyFrom(feed)
        buf = resp.SerializeToString()

        decoded = upstox_real._decode_feed_frame(buf)
        assert isinstance(decoded, list)
        assert len(decoded) == 1
        item = decoded[0]
        assert item['symbol'] == 'NIFTY 50'  # reverse-mapped
        assert abs(item['ltp'] - 22150.45) < 0.001

    def test_decode_unknown_instrument_key_passthrough(self):
        import upstox_real
        from MarketDataFeed_pb2 import FeedResponse, Feed
        resp = FeedResponse()
        feed = Feed()
        feed.ltpc.ltp = 999.99
        resp.feeds['NSE_EQ|UNKNOWN-XYZ'].CopyFrom(feed)
        buf = resp.SerializeToString()
        decoded = upstox_real._decode_feed_frame(buf)
        assert len(decoded) == 1
        assert decoded[0]['symbol'] == 'NSE_EQ|UNKNOWN-XYZ'
        assert abs(decoded[0]['ltp'] - 999.99) < 0.001


# ============== Candles endpoint ==============
class TestCandlesEndpoint:
    def test_default_count_60(self):
        r = requests.get(f"{BASE_URL}/api/market/candles/NIFTY 50", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d['symbol'] == 'NIFTY 50'
        assert d['interval'] == '1minute'
        assert len(d['candles']) == 60
        c = d['candles'][0]
        for k in ('ts', 'open', 'high', 'low', 'close', 'volume'):
            assert k in c
        assert c['high'] >= c['low']
        assert c['volume'] > 0

    def test_custom_count(self):
        r = requests.get(f"{BASE_URL}/api/market/candles/NIFTY 50?count=10", timeout=10)
        assert r.status_code == 200
        assert len(r.json()['candles']) == 10

    def test_count_clamped_low(self):
        r = requests.get(f"{BASE_URL}/api/market/candles/NIFTY 50?count=1", timeout=10)
        assert r.status_code == 200
        assert len(r.json()['candles']) == 5  # min clamp

    def test_count_clamped_high(self):
        r = requests.get(f"{BASE_URL}/api/market/candles/NIFTY 50?count=999", timeout=10)
        assert r.status_code == 200
        assert len(r.json()['candles']) == 200  # max clamp

    @pytest.mark.parametrize("interval", ["1minute", "5minute", "15minute", "30minute", "day", "week", "month"])
    def test_intervals(self, interval):
        r = requests.get(f"{BASE_URL}/api/market/candles/NIFTY 50?interval={interval}&count=10", timeout=10)
        assert r.status_code == 200
        assert r.json()['interval'] == interval
        assert len(r.json()['candles']) == 10

    def test_options_symbol(self):
        r = requests.get(f"{BASE_URL}/api/market/candles/NIFTY 22150 CE?count=20", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert len(d['candles']) == 20
        for c in d['candles']:
            assert c['high'] >= c['low']
            assert c['volume'] > 0


# ============== Regression ==============
class TestRegression:
    def test_indices(self):
        r = requests.get(f"{BASE_URL}/api/market/indices", timeout=10)
        assert r.status_code == 200
        assert len(r.json()) == 4

    def test_watchlist(self):
        r = requests.get(f"{BASE_URL}/api/market/watchlist", timeout=10)
        assert r.status_code == 200
        assert len(r.json()) >= 10

    def test_fno(self):
        r = requests.get(f"{BASE_URL}/api/market/fno", timeout=10)
        assert r.status_code == 200
        assert len(r.json()) >= 14

    def test_option_chain(self):
        r = requests.get(f"{BASE_URL}/api/market/option-chain?symbol=NIFTY", timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert 'rows' in d and len(d['rows']) == 21

    def test_login_init(self):
        r = requests.post(f"{BASE_URL}/api/auth/upstox/login", timeout=10)
        assert r.status_code == 200
        assert 'authorization_url' in r.json()

    def test_full_order_flow(self):
        # callback for token
        r = requests.post(f"{BASE_URL}/api/auth/upstox/callback",
                          json={"code": "TEST_CODE", "state": "x"}, timeout=10)
        assert r.status_code == 200
        token = r.json()['access_token']
        headers = {"Authorization": f"Bearer {token}"}
        # place BUY with SL/Target
        payload = {
            "symbol": "NIFTY 22150 CE",
            "quantity": 25,
            "order_type": "MARKET",
            "transaction_type": "BUY",
            "stop_loss": 91.5,
            "target": 101.5,
        }
        r = requests.post(f"{BASE_URL}/api/orders/place", json=payload, headers=headers, timeout=10)
        assert r.status_code == 200, r.text
        order = r.json()
        assert order['symbol'] == 'NIFTY 22150 CE'
        assert order['transaction_type'] == 'BUY'
        assert order['quantity'] == 25
        assert order['stop_loss'] == 91.5
        assert order['target'] == 101.5
        assert order['id'].startswith('ORD')

        # SELL
        payload['transaction_type'] = 'SELL'
        r = requests.post(f"{BASE_URL}/api/orders/place", json=payload, headers=headers, timeout=10)
        assert r.status_code == 200
        assert r.json()['transaction_type'] == 'SELL'

        # list orders
        r = requests.get(f"{BASE_URL}/api/orders", headers=headers, timeout=10)
        assert r.status_code == 200
        assert len(r.json()) >= 2

    def test_portfolio(self):
        r = requests.post(f"{BASE_URL}/api/auth/upstox/callback",
                          json={"code": "X"}, timeout=10)
        token = r.json()['access_token']
        r = requests.get(f"{BASE_URL}/api/portfolio",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert r.status_code == 200
        assert 'holdings' in r.json()
