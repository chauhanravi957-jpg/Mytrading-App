from fastapi import FastAPI, APIRouter, HTTPException, Header, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import asyncio
import json
import logging
import random
import uuid
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import upstox_real  # noqa: E402  (loads after .env)

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ============== Models ==============
class UpstoxLoginInit(BaseModel):
    """Response with Upstox-style authorization URL (mock)."""
    authorization_url: str
    state: str

class UpstoxCallbackRequest(BaseModel):
    code: str
    state: Optional[str] = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 86400
    user: dict

class OrderRequest(BaseModel):
    symbol: str
    quantity: int
    order_type: str  # MARKET, LIMIT
    transaction_type: str  # BUY, SELL
    price: Optional[float] = 0.0
    product: str = "MIS"  # MIS, CNC
    stop_loss: Optional[float] = None
    target: Optional[float] = None

class Order(BaseModel):
    id: str = Field(default_factory=lambda: f"ORD{uuid.uuid4().hex[:10].upper()}")
    symbol: str
    quantity: int
    order_type: str
    transaction_type: str
    price: float
    product: str
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    status: str = "COMPLETE"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ============== Mock data ==============
MOCK_USER = {
    "user_id": "UPX_DEMO_001",
    "user_name": "Demo Trader",
    "email": "demo@upstox.local",
    "broker": "UPSTOX",
    "exchanges": ["NSE", "BSE", "NFO", "MCX"],
    "products": ["I", "D", "CO"],
    "available_funds": 125430.50,
}

MOCK_INDICES_BASE = [
    {"symbol": "NIFTY 50", "exchange": "NSE", "base": 22150.45},
    {"symbol": "BANKNIFTY", "exchange": "NSE", "base": 47820.30},
    {"symbol": "SENSEX", "exchange": "BSE", "base": 73180.95},
    {"symbol": "FINNIFTY", "exchange": "NSE", "base": 21340.10},
]

MOCK_WATCHLIST_BASE = [
    {"symbol": "RELIANCE", "exchange": "NSE", "base": 2840.50, "name": "Reliance Industries"},
    {"symbol": "TCS", "exchange": "NSE", "base": 3920.15, "name": "Tata Consultancy"},
    {"symbol": "HDFCBANK", "exchange": "NSE", "base": 1485.70, "name": "HDFC Bank"},
    {"symbol": "INFY", "exchange": "NSE", "base": 1742.30, "name": "Infosys"},
    {"symbol": "ICICIBANK", "exchange": "NSE", "base": 1098.45, "name": "ICICI Bank"},
    {"symbol": "SBIN", "exchange": "NSE", "base": 745.20, "name": "State Bank of India"},
    {"symbol": "ITC", "exchange": "NSE", "base": 432.85, "name": "ITC Limited"},
    {"symbol": "BHARTIARTL", "exchange": "NSE", "base": 1245.60, "name": "Bharti Airtel"},
    {"symbol": "LT", "exchange": "NSE", "base": 3560.90, "name": "Larsen & Toubro"},
    {"symbol": "ADANIENT", "exchange": "NSE", "base": 2890.25, "name": "Adani Enterprises"},
]

# F&O instruments — Index Futures + Stock Futures + popular Options
MOCK_FNO_BASE = [
    # Index Futures
    {"symbol": "NIFTY FUT", "underlying": "NIFTY 50", "exchange": "NFO", "type": "FUT", "lot": 25, "base": 22165.00, "expiry": "2026-02-27"},
    {"symbol": "BANKNIFTY FUT", "underlying": "BANKNIFTY", "exchange": "NFO", "type": "FUT", "lot": 15, "base": 47850.00, "expiry": "2026-02-27"},
    {"symbol": "FINNIFTY FUT", "underlying": "FINNIFTY", "exchange": "NFO", "type": "FUT", "lot": 25, "base": 21360.00, "expiry": "2026-02-27"},
    {"symbol": "SENSEX FUT", "underlying": "SENSEX", "exchange": "BFO", "type": "FUT", "lot": 10, "base": 73220.00, "expiry": "2026-02-29"},
    # Stock Futures
    {"symbol": "RELIANCE FUT", "underlying": "RELIANCE", "exchange": "NFO", "type": "FUT", "lot": 250, "base": 2843.00, "expiry": "2026-02-27"},
    {"symbol": "TCS FUT", "underlying": "TCS", "exchange": "NFO", "type": "FUT", "lot": 175, "base": 3924.00, "expiry": "2026-02-27"},
    {"symbol": "HDFCBANK FUT", "underlying": "HDFCBANK", "exchange": "NFO", "type": "FUT", "lot": 550, "base": 1487.50, "expiry": "2026-02-27"},
    {"symbol": "INFY FUT", "underlying": "INFY", "exchange": "NFO", "type": "FUT", "lot": 400, "base": 1744.00, "expiry": "2026-02-27"},
    {"symbol": "ICICIBANK FUT", "underlying": "ICICIBANK", "exchange": "NFO", "type": "FUT", "lot": 700, "base": 1099.50, "expiry": "2026-02-27"},
    {"symbol": "SBIN FUT", "underlying": "SBIN", "exchange": "NFO", "type": "FUT", "lot": 750, "base": 746.10, "expiry": "2026-02-27"},
    {"symbol": "ITC FUT", "underlying": "ITC", "exchange": "NFO", "type": "FUT", "lot": 1600, "base": 433.20, "expiry": "2026-02-27"},
    {"symbol": "BHARTIARTL FUT", "underlying": "BHARTIARTL", "exchange": "NFO", "type": "FUT", "lot": 475, "base": 1247.00, "expiry": "2026-02-27"},
    {"symbol": "LT FUT", "underlying": "LT", "exchange": "NFO", "type": "FUT", "lot": 175, "base": 3563.50, "expiry": "2026-02-27"},
    {"symbol": "ADANIENT FUT", "underlying": "ADANIENT", "exchange": "NFO", "type": "FUT", "lot": 300, "base": 2893.00, "expiry": "2026-02-27"},
    # Popular Index Options (ATM-ish)
    {"symbol": "NIFTY 22150 CE", "underlying": "NIFTY 50", "exchange": "NFO", "type": "CE", "lot": 25, "base": 95.50, "expiry": "2026-02-27"},
    {"symbol": "NIFTY 22150 PE", "underlying": "NIFTY 50", "exchange": "NFO", "type": "PE", "lot": 25, "base": 88.20, "expiry": "2026-02-27"},
    {"symbol": "BANKNIFTY 47800 CE", "underlying": "BANKNIFTY", "exchange": "NFO", "type": "CE", "lot": 15, "base": 215.00, "expiry": "2026-02-27"},
    {"symbol": "BANKNIFTY 47800 PE", "underlying": "BANKNIFTY", "exchange": "NFO", "type": "PE", "lot": 15, "base": 198.50, "expiry": "2026-02-27"},
]

def _fluctuate(base: float, vol: float = 0.005) -> float:
    return round(base * (1 + random.uniform(-vol, vol)), 2)

def _verify_token(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = authorization.replace("Bearer ", "")
    if upstox_real.is_configured() and not token.startswith("upx_mock_"):
        # In real mode, accept Upstox-issued tokens. Light validation by hitting /profile.
        try:
            upstox_real.fetch_profile(token)
            return MOCK_USER  # placeholder — replace with real profile mapping if needed
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid Upstox token")
    if not token.startswith("upx_mock_"):
        raise HTTPException(status_code=401, detail="Invalid token")
    return MOCK_USER

# ============== Auth (Upstox OAuth mock) ==============
@api_router.get("/")
async def root():
    return {"message": "Upstox Trading API (mock mode)", "status": "ok"}

@api_router.post("/auth/upstox/login", response_model=UpstoxLoginInit)
async def upstox_login_init():
    """Step 1: Initiates Upstox OAuth. Returns the authorization URL.
    If USE_REAL_UPSTOX=true AND credentials are configured, returns the real
    Upstox OAuth dialog URL — otherwise returns a mock URL for demo mode.
    """
    state = uuid.uuid4().hex
    if upstox_real.is_configured():
        url = upstox_real.build_authorization_url(state)
    else:
        url = f"https://api.upstox.com/v2/login/authorization/dialog?state={state}"
    return UpstoxLoginInit(authorization_url=url, state=state)

@api_router.post("/auth/upstox/callback", response_model=TokenResponse)
async def upstox_callback(payload: UpstoxCallbackRequest):
    """Step 2: Exchange authorization code for access token.
    Real mode: calls Upstox token endpoint with API key/secret.
    Mock mode: returns a generated upx_mock_* token.
    """
    if not payload.code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    if upstox_real.is_configured():
        try:
            data = upstox_real.exchange_code_for_token(payload.code)
            access_token = data["access_token"]
            profile = upstox_real.fetch_profile(access_token)
            funds = upstox_real.fetch_funds(access_token)
            user = {
                "user_id": profile.get("user_id"),
                "user_name": profile.get("user_name") or profile.get("name"),
                "email": profile.get("email"),
                "broker": "UPSTOX",
                "exchanges": profile.get("exchanges", []),
                "products": profile.get("products", []),
                "available_funds": (funds.get("equity") or {}).get("available_margin", 0.0),
            }
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Upstox auth failed: {e}")
    else:
        access_token = f"upx_mock_{uuid.uuid4().hex}"
        user = MOCK_USER
    await db.sessions.insert_one({
        "_id": access_token,
        "user_id": user["user_id"],
        "code": payload.code,
        "real": upstox_real.is_configured(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return TokenResponse(access_token=access_token, user=user)

@api_router.get("/auth/me")
async def auth_me(authorization: Optional[str] = Header(None)):
    user = _verify_token(authorization)
    return user

@api_router.post("/auth/logout")
async def auth_logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
        await db.sessions.delete_one({"_id": token})
    return {"status": "logged_out"}

# ============== Market data ==============
@api_router.get("/market/indices")
async def market_indices():
    out = []
    for ix in MOCK_INDICES_BASE:
        ltp = _fluctuate(ix["base"], 0.003)
        change = round(ltp - ix["base"], 2)
        change_pct = round((change / ix["base"]) * 100, 2)
        out.append({
            "symbol": ix["symbol"],
            "exchange": ix["exchange"],
            "ltp": ltp,
            "change": change,
            "change_pct": change_pct,
            "open": ix["base"],
            "high": round(ix["base"] * 1.008, 2),
            "low": round(ix["base"] * 0.992, 2),
            "close": ix["base"],
        })
    return out

@api_router.get("/market/watchlist")
async def market_watchlist():
    out = []
    for st in MOCK_WATCHLIST_BASE:
        ltp = _fluctuate(st["base"], 0.008)
        change = round(ltp - st["base"], 2)
        change_pct = round((change / st["base"]) * 100, 2)
        out.append({
            "symbol": st["symbol"],
            "name": st["name"],
            "exchange": st["exchange"],
            "ltp": ltp,
            "change": change,
            "change_pct": change_pct,
            "volume": random.randint(100000, 5000000),
        })
    return out

@api_router.get("/market/fno")
async def market_fno(instrument_type: Optional[str] = None):
    """Live prices for F&O instruments: index futures, stock futures, options.
    Optional filter: instrument_type=FUT|CE|PE
    """
    out = []
    for it in MOCK_FNO_BASE:
        if instrument_type and it["type"] != instrument_type:
            continue
        vol = 0.012 if it["type"] in ("CE", "PE") else 0.004
        ltp = _fluctuate(it["base"], vol)
        change = round(ltp - it["base"], 2)
        change_pct = round((change / it["base"]) * 100, 2)
        out.append({
            "symbol": it["symbol"],
            "underlying": it["underlying"],
            "exchange": it["exchange"],
            "type": it["type"],
            "lot_size": it["lot"],
            "expiry": it["expiry"],
            "ltp": ltp,
            "change": change,
            "change_pct": change_pct,
            "oi": random.randint(50000, 5000000),
            "volume": random.randint(50000, 800000),
        })
    return out

@api_router.get("/market/quote/{symbol}")
async def market_quote(symbol: str):
    base = next((s for s in MOCK_WATCHLIST_BASE + MOCK_INDICES_BASE + MOCK_FNO_BASE if s["symbol"] == symbol), None)
    if not base:
        raise HTTPException(status_code=404, detail="Symbol not found")
    ltp = _fluctuate(base["base"], 0.005)
    return {
        "symbol": symbol,
        "ltp": ltp,
        "open": base["base"],
        "high": round(base["base"] * 1.012, 2),
        "low": round(base["base"] * 0.988, 2),
        "close": base["base"],
        "change": round(ltp - base["base"], 2),
        "change_pct": round((ltp - base["base"]) / base["base"] * 100, 2),
    }

@api_router.get("/market/candles/{symbol}")
async def market_candles(symbol: str, interval: str = "1minute", count: int = 60):
    """Historical OHLCV candles for a symbol.
    interval: 1minute | 30minute | day | week | month
    count: number of candles to return (max 200 for mock).

    Real mode: would call upstox_real /v2/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}.
    Mock mode: generates a realistic OHLC walk from the symbol's base price.
    """
    base = next((s for s in MOCK_WATCHLIST_BASE + MOCK_INDICES_BASE + MOCK_FNO_BASE if s["symbol"] == symbol), None)
    base_price = base["base"] if base else (80.0 if (" CE" in symbol or " PE" in symbol) else 100.0)
    vol = 0.012 if (" CE" in symbol or " PE" in symbol) else 0.004
    interval_minutes = {"1minute": 1, "5minute": 5, "15minute": 15, "30minute": 30, "day": 1440, "week": 10080, "month": 43200}.get(interval, 1)
    n = max(5, min(count, 200))
    candles = []
    price = base_price
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    for i in range(n):
        ts = now - timedelta(minutes=interval_minutes * (n - i - 1))
        o = price
        # random walk
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
    return {"symbol": symbol, "interval": interval, "candles": candles}

@api_router.get("/market/option-chain")
async def option_chain(symbol: str = "NIFTY", expiry: Optional[str] = None):
    spot = next((s for s in MOCK_INDICES_BASE if s["symbol"] == ("NIFTY 50" if symbol == "NIFTY" else symbol)), MOCK_INDICES_BASE[0])
    spot_price = _fluctuate(spot["base"], 0.002)
    atm = round(spot_price / 50) * 50
    strikes = [atm + (i - 10) * 50 for i in range(21)]
    rows = []
    for k in strikes:
        moneyness = (spot_price - k) / spot_price
        ce_ltp = max(2.0, round(spot_price - k + random.uniform(20, 80), 2)) if k <= spot_price else max(1.0, round(random.uniform(5, 60) * (1 - abs(moneyness) * 5), 2))
        pe_ltp = max(2.0, round(k - spot_price + random.uniform(20, 80), 2)) if k >= spot_price else max(1.0, round(random.uniform(5, 60) * (1 - abs(moneyness) * 5), 2))
        rows.append({
            "strike": k,
            "ce": {
                "ltp": round(ce_ltp, 2),
                "change_pct": round(random.uniform(-15, 15), 2),
                "oi": random.randint(5000, 250000),
                "volume": random.randint(1000, 50000),
                "iv": round(random.uniform(11, 22), 2),
            },
            "pe": {
                "ltp": round(pe_ltp, 2),
                "change_pct": round(random.uniform(-15, 15), 2),
                "oi": random.randint(5000, 250000),
                "volume": random.randint(1000, 50000),
                "iv": round(random.uniform(11, 22), 2),
            },
        })
    return {
        "symbol": symbol,
        "spot": spot_price,
        "atm_strike": atm,
        "expiry": expiry or "2026-02-27",
        "rows": rows,
    }

# ============== Orders ==============
@api_router.post("/orders/place", response_model=Order)
async def place_order(req: OrderRequest, authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    # Use LTP if MARKET order with no price
    price = req.price or 0.0
    if req.order_type == "MARKET" or price == 0:
        base = next((s for s in MOCK_WATCHLIST_BASE + MOCK_INDICES_BASE + MOCK_FNO_BASE if s["symbol"] == req.symbol), None)
        price = _fluctuate(base["base"], 0.003) if base else round(random.uniform(100, 3000), 2)
    order = Order(
        symbol=req.symbol,
        quantity=req.quantity,
        order_type=req.order_type,
        transaction_type=req.transaction_type,
        price=price,
        product=req.product,
        stop_loss=req.stop_loss,
        target=req.target,
    )
    await db.orders.insert_one(order.dict())
    return order

@api_router.get("/orders", response_model=List[Order])
async def list_orders(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    cursor = db.orders.find({}, {"_id": 0}).sort("created_at", -1).limit(100)
    docs = await cursor.to_list(100)
    return [Order(**d) for d in docs]

@api_router.delete("/orders/{order_id}")
async def cancel_order(order_id: str, authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    res = await db.orders.update_one({"id": order_id}, {"$set": {"status": "CANCELLED"}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"status": "cancelled", "id": order_id}

# ============== Portfolio ==============
@api_router.get("/portfolio")
async def portfolio(authorization: Optional[str] = Header(None)):
    _verify_token(authorization)
    holdings = []
    total_invested = 0.0
    total_current = 0.0
    sample = MOCK_WATCHLIST_BASE[:5]
    for st in sample:
        qty = random.randint(5, 50)
        avg = round(st["base"] * random.uniform(0.85, 1.05), 2)
        ltp = _fluctuate(st["base"], 0.01)
        invested = round(avg * qty, 2)
        current = round(ltp * qty, 2)
        pnl = round(current - invested, 2)
        total_invested += invested
        total_current += current
        holdings.append({
            "symbol": st["symbol"],
            "name": st["name"],
            "quantity": qty,
            "avg_price": avg,
            "ltp": ltp,
            "invested": invested,
            "current_value": current,
            "pnl": pnl,
            "pnl_pct": round(pnl / invested * 100, 2),
        })
    return {
        "holdings": holdings,
        "summary": {
            "total_invested": round(total_invested, 2),
            "current_value": round(total_current, 2),
            "total_pnl": round(total_current - total_invested, 2),
            "total_pnl_pct": round((total_current - total_invested) / total_invested * 100, 2),
            "available_funds": MOCK_USER["available_funds"],
        },
    }

# ============== Mount ==============
app.include_router(api_router)


# ============== WebSocket: Live Market Feed ==============
# Mock mode: server pushes random LTP ticks for the requested instruments every ~700ms.
# Real mode: when USE_REAL_UPSTOX=true & credentials provided, replace the loop body
# with a relay from Upstox MarketDataFeed WS (see upstox_real.stream_ticks skeleton).

def _base_for(symbol: str) -> Optional[float]:
    for s in MOCK_WATCHLIST_BASE + MOCK_INDICES_BASE + MOCK_FNO_BASE:
        if s["symbol"] == symbol:
            return s["base"]
    # Option chain strike LTPs are dynamic; return a small base for CE/PE symbols
    if " CE" in symbol or " PE" in symbol:
        return 80.0
    return None

@app.websocket("/api/ws/market")
async def ws_market(websocket: WebSocket):
    await websocket.accept()
    subs: set = set()
    real_task: Optional[asyncio.Task] = None
    real_stop = asyncio.Event()
    pump_task: Optional[asyncio.Task] = None
    try:
        mode = "real" if upstox_real.is_configured() else "mock"
        await websocket.send_json({"type": "hello", "mode": mode})

        async def mock_pump():
            while True:
                if subs:
                    ticks = []
                    for sym in list(subs):
                        base = _base_for(sym) or 100.0
                        ltp = _fluctuate(base, 0.006 if " CE" in sym or " PE" in sym else 0.004)
                        ticks.append({"symbol": sym, "ltp": ltp, "ts": datetime.now(timezone.utc).isoformat()})
                    await websocket.send_json({"type": "tick", "data": ticks})
                await asyncio.sleep(0.7)

        async def on_real_tick(ticks: list):
            await websocket.send_json({"type": "tick", "data": ticks})

        async def start_real(access_token: str):
            instrument_keys = [upstox_real.get_instrument_key(s) or s for s in subs]
            instrument_keys = [k for k in instrument_keys if k]
            if not instrument_keys:
                return
            await upstox_real.stream_ticks(access_token, instrument_keys, on_real_tick, real_stop)

        if mode == "mock":
            pump_task = asyncio.create_task(mock_pump())

        while True:
            msg = await websocket.receive_text()
            try:
                cmd = json.loads(msg)
            except Exception:
                continue
            action = cmd.get("action")
            syms = cmd.get("symbols", [])
            access_token = cmd.get("access_token")  # frontend can pass for real mode
            if action == "subscribe":
                for s in syms:
                    subs.add(s)
                await websocket.send_json({"type": "subscribed", "symbols": list(subs)})
                # Real mode: (re)start the upstream stream with the new sub set
                if mode == "real" and access_token:
                    if real_task and not real_task.done():
                        real_stop.set()
                        try:
                            await asyncio.wait_for(real_task, timeout=2)
                        except Exception:
                            real_task.cancel()
                    real_stop = asyncio.Event()
                    real_task = asyncio.create_task(start_real(access_token))
            elif action == "unsubscribe":
                for s in syms:
                    subs.discard(s)
                await websocket.send_json({"type": "unsubscribed", "symbols": list(subs)})
    except WebSocketDisconnect:
        pass
    finally:
        for t in (pump_task, real_task):
            try:
                if t:
                    t.cancel()
            except Exception:
                pass
        real_stop.set()

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
