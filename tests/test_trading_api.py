"""Backend tests for Upstox mock trading app"""
import os
import pytest
import requests

BASE = os.environ['EXPO_PUBLIC_BACKEND_URL'].rstrip('/') if os.environ.get('EXPO_PUBLIC_BACKEND_URL') else None
if not BASE:
    # fallback to frontend .env
    with open('/app/frontend/.env') as f:
        for line in f:
            if line.startswith('EXPO_PUBLIC_BACKEND_URL='):
                BASE = line.split('=', 1)[1].strip().rstrip('/')


@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{BASE}/api/auth/upstox/callback", json={"code": "TEST_code_xyz"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["access_token"].startswith("upx_mock_")
    assert data["user"]["user_id"] == "UPX_DEMO_001"
    return data["access_token"]


@pytest.fixture
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# --- Auth tests ---
def test_login_init():
    r = requests.post(f"{BASE}/api/auth/upstox/login")
    assert r.status_code == 200
    d = r.json()
    assert "authorization_url" in d and "state" in d
    assert "upstox.com" in d["authorization_url"]


def test_callback_missing_code():
    r = requests.post(f"{BASE}/api/auth/upstox/callback", json={"code": ""})
    assert r.status_code == 400


def test_me_without_token():
    r = requests.get(f"{BASE}/api/auth/me")
    assert r.status_code == 401


def test_me_invalid_token():
    r = requests.get(f"{BASE}/api/auth/me", headers={"Authorization": "Bearer bad_token"})
    assert r.status_code == 401


def test_me_with_token(auth_headers):
    r = requests.get(f"{BASE}/api/auth/me", headers=auth_headers)
    assert r.status_code == 200
    d = r.json()
    assert d["user_id"] == "UPX_DEMO_001"
    assert d["email"] == "demo@upstox.local"
    assert "available_funds" in d


# --- Market tests ---
def test_indices():
    r = requests.get(f"{BASE}/api/market/indices")
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 4
    symbols = [x["symbol"] for x in d]
    for s in ["NIFTY 50", "BANKNIFTY", "SENSEX", "FINNIFTY"]:
        assert s in symbols
    for ix in d:
        assert "ltp" in ix and "change" in ix and "change_pct" in ix


def test_watchlist():
    r = requests.get(f"{BASE}/api/market/watchlist")
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 10
    assert all("ltp" in s and "symbol" in s and "name" in s for s in d)


def test_option_chain():
    r = requests.get(f"{BASE}/api/market/option-chain?symbol=NIFTY")
    assert r.status_code == 200
    d = r.json()
    assert "spot" in d and "atm_strike" in d
    assert len(d["rows"]) == 21
    for row in d["rows"]:
        assert "strike" in row and "ce" in row and "pe" in row
        assert "ltp" in row["ce"] and "ltp" in row["pe"]


# --- F&O tests ---
def test_fno_all():
    r = requests.get(f"{BASE}/api/market/fno")
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 18, f"Expected 18 F&O instruments, got {len(d)}"
    symbols = [x["symbol"] for x in d]
    for s in ["NIFTY FUT", "BANKNIFTY FUT", "FINNIFTY FUT", "SENSEX FUT",
              "RELIANCE FUT", "TCS FUT", "HDFCBANK FUT",
              "NIFTY 22150 CE", "NIFTY 22150 PE",
              "BANKNIFTY 47800 CE", "BANKNIFTY 47800 PE"]:
        assert s in symbols, f"Missing symbol {s}"
    for it in d:
        for k in ["symbol", "underlying", "type", "lot_size", "expiry", "ltp", "change", "change_pct", "oi"]:
            assert k in it, f"Missing key {k} in {it}"
        assert it["type"] in ("FUT", "CE", "PE")


def test_fno_filter_fut():
    r = requests.get(f"{BASE}/api/market/fno?instrument_type=FUT")
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 14
    assert all(x["type"] == "FUT" for x in d)


def test_fno_filter_ce():
    r = requests.get(f"{BASE}/api/market/fno?instrument_type=CE")
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 2
    assert all(x["type"] == "CE" for x in d)


def test_fno_filter_pe():
    r = requests.get(f"{BASE}/api/market/fno?instrument_type=PE")
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 2
    assert all(x["type"] == "PE" for x in d)


def test_place_order_fno_fut(auth_headers):
    payload = {
        "symbol": "RELIANCE FUT", "quantity": 1, "order_type": "MARKET",
        "transaction_type": "BUY", "price": 0, "product": "MIS",
        "stop_loss": 2839.0, "target": 2849.0,
    }
    r = requests.post(f"{BASE}/api/orders/place", json=payload, headers=auth_headers)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["symbol"] == "RELIANCE FUT"
    assert o["price"] > 0
    assert o["stop_loss"] == 2839.0
    assert o["target"] == 2849.0


def test_place_order_fno_option(auth_headers):
    payload = {
        "symbol": "NIFTY 22150 CE", "quantity": 25, "order_type": "MARKET",
        "transaction_type": "BUY", "price": 0, "product": "MIS",
    }
    r = requests.post(f"{BASE}/api/orders/place", json=payload, headers=auth_headers)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["symbol"] == "NIFTY 22150 CE"
    # Price should be resolved from the FNO base (~95.50), so should be small (not random 100-3000)
    assert 50 < o["price"] < 200, f"Price {o['price']} seems wrong for NIFTY CE option"



# --- Orders tests ---
def test_place_order_no_auth():
    r = requests.post(f"{BASE}/api/orders/place", json={
        "symbol": "RELIANCE", "quantity": 1, "order_type": "MARKET", "transaction_type": "BUY"
    })
    assert r.status_code == 401


def test_place_and_list_and_cancel(auth_headers):
    payload = {
        "symbol": "RELIANCE", "quantity": 5, "order_type": "MARKET",
        "transaction_type": "BUY", "price": 0, "product": "MIS"
    }
    r = requests.post(f"{BASE}/api/orders/place", json=payload, headers=auth_headers)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["id"].startswith("ORD")
    assert order["symbol"] == "RELIANCE"
    assert order["status"] == "COMPLETE"
    assert order["price"] > 0
    oid = order["id"]

    # list
    r2 = requests.get(f"{BASE}/api/orders", headers=auth_headers)
    assert r2.status_code == 200
    orders = r2.json()
    assert any(o["id"] == oid for o in orders)

    # cancel
    r3 = requests.delete(f"{BASE}/api/orders/{oid}", headers=auth_headers)
    assert r3.status_code == 200
    assert r3.json()["status"] == "cancelled"

    # verify cancelled status persisted
    r4 = requests.get(f"{BASE}/api/orders", headers=auth_headers)
    cancelled = [o for o in r4.json() if o["id"] == oid]
    assert cancelled and cancelled[0]["status"] == "CANCELLED"


def test_cancel_unknown_order(auth_headers):
    r = requests.delete(f"{BASE}/api/orders/ORD_NOTEXIST", headers=auth_headers)
    assert r.status_code == 404


# --- Portfolio ---
def test_portfolio_no_auth():
    r = requests.get(f"{BASE}/api/portfolio")
    assert r.status_code == 401


def test_portfolio(auth_headers):
    r = requests.get(f"{BASE}/api/portfolio", headers=auth_headers)
    assert r.status_code == 200
    d = r.json()
    assert len(d["holdings"]) == 5
    assert "total_invested" in d["summary"]
    assert "available_funds" in d["summary"]
    for h in d["holdings"]:
        for k in ["symbol", "quantity", "avg_price", "ltp", "pnl"]:
            assert k in h
