# UpTrade — Upstox-Style Trading App (React Native Expo)

## Overview
A mobile trading app that integrates with Upstox API (currently in **mock/demo mode** — no real Upstox credentials are used). The full OAuth-style login flow is simulated end-to-end and live market data is generated server-side to mimic real ticks.

## Login Flow (as per requirement)
1. **LOGIN button** on `/login` screen
2. **Upstox Login Open** → `POST /api/auth/upstox/login` returns authorization URL + state
3. **User Login + Authorization Code** → simulated automatically (mock code)
4. **Access Token Generate** → `POST /api/auth/upstox/callback` exchanges code for `upx_mock_*` token
5. **Dashboard automatic khulse** → token saved (expo-secure-store / localStorage on web), router replaces to `/(tabs)/dashboard`

## Features Implemented
- ✅ Automatic redirect handling (splash checks token, auto-redirects)
- ✅ Token save (secure storage) + auto-login on app re-open
- ✅ **Live WebSocket market feed** — `/api/ws/market`; Dashboard, Option Chain, Order page all consume it
- ✅ Real-time NIFTY 50, BANKNIFTY, SENSEX, FINNIFTY prices (live via WS on Dashboard)
- ✅ **Clickable index cards** on Dashboard — tapping NIFTY 50 / BANKNIFTY / FINNIFTY navigates to Option Chain
- ✅ **F&O Live Prices section** — 18 instruments with filter chips (ALL/FUT/CE/PE)
- ✅ Option Chain with 21 strikes, **ATM strike highlighted in GOLD/YELLOW** (yellow row bg + 3px gold left border + gold strike text + gold ATM badge), WebSocket live CE/PE LTP, "● LIVE" indicator
- ✅ **Full-screen Order page** (`/app/order.tsx`) — header + LTP card with live sparkline (SVG Polyline), MARKET/LIMIT toggle, lot stepper, **auto SL = LTP − 4pts, auto Target = LTP + 6pts**, margin estimate, BUY/SELL footer buttons. SL/Target auto-track WS LTP unless user edits them.
- ✅ Buy/Sell modal (legacy bottom sheet) on Dashboard with ₹4/₹6 defaults
- ✅ Orders list with cancel, Portfolio with P&L, Profile with logout
- ✅ **Live sparkline (tick stream)** + **Historical Candlestick chart** (SVG) on Order page with 1m/5m/15m/1D interval chips — refreshes every 30s, backend endpoint `GET /api/market/candles/{symbol}?interval=...&count=...`
- ✅ **Real Upstox V2 integration scaffold** — `backend/upstox_real.py` with OAuth, profile, funds, market quote, place order, WS authorize. **Full Protobuf WebSocket decoder is WIRED & WORKING** (`MarketDataFeed.proto` + auto-generated `MarketDataFeed_pb2.py`, `HAS_PB2=True`). `_decode_feed_frame()` parses real Upstox MarketDataFeed binary frames into `[{symbol, ltp, ts}]`. Uses PLACEHOLDERS by default — flip `USE_REAL_UPSTOX=true` in `backend/.env` after replacing keys to go live with real ticks.

## Going Live with Real Upstox

1. Open `backend/.env`, replace:
   ```
   UPSTOX_API_KEY="<your real key>"
   UPSTOX_API_SECRET="<your real secret>"
   UPSTOX_REDIRECT_URI="<your redirect uri>"
   USE_REAL_UPSTOX="true"
   ```
2. Restart backend (`sudo supervisorctl restart backend`).
3. The "Login with Upstox" button will now open the REAL Upstox OAuth dialog; after consent, the app exchanges the auth code for a real access token and uses it for subsequent API calls.
4. (Advanced) To stream REAL market ticks, implement the protobuf decoder in `upstox_real.stream_ticks()` (skeleton provided) and replace the random-walk pump inside `/api/ws/market`.

## Architecture

### Backend (`/app/backend/server.py`)
FastAPI with `/api` prefix. Endpoints:
- Auth: `POST /api/auth/upstox/login`, `POST /api/auth/upstox/callback`, `GET /api/auth/me`, `POST /api/auth/logout`
- Market: `GET /api/market/indices`, `GET /api/market/watchlist`, `GET /api/market/quote/{symbol}`, `GET /api/market/option-chain`
- Orders: `POST /api/orders/place`, `GET /api/orders`, `DELETE /api/orders/{id}`
- Portfolio: `GET /api/portfolio`

### Frontend (Expo Router file-based routing)
```
app/
├── _layout.tsx          (Stack root)
├── index.tsx            (Splash + auto-login check)
├── login.tsx            (Upstox OAuth simulation)
└── (tabs)/
    ├── _layout.tsx      (Bottom tabs)
    ├── dashboard.tsx    (Indices + Watchlist)
    ├── options.tsx      (Option Chain)
    ├── orders.tsx       (Orders list)
    ├── portfolio.tsx    (Holdings + P&L)
    └── profile.tsx      (User + Logout)
src/
├── api.ts               (Fetch client + secure storage + colors)
└── OrderModal.tsx       (Buy/Sell bottom sheet w/ Auto SL/Tgt)
```

## Mocked / Real
**MOCKED**: Upstox OAuth (entire login flow), live market data (random fluctuations), option chain, portfolio holdings — all server-generated for demo.

## To Go Live (with real Upstox)
1. Replace mock token generation in `auth/upstox/callback` with a real Upstox token-exchange call (https://api.upstox.com/v2/login/authorization/token) using your `API_KEY` + `API_SECRET` + `REDIRECT_URI`
2. Replace `market_indices/watchlist/option_chain` with Upstox market quote APIs
3. Replace `orders/place` with Upstox order placement endpoint
4. Add WebSocket consumer for real live ticks (Upstox Market Feed)

## Design
Dark "Performance Pro" theme — Obsidian background (#0A0A0C), high-contrast text, brand blue accents, electric green / signal red for buy/sell. Tabular numerals on all prices to prevent jitter.

## Test Coverage
- Backend: 13/13 pytest passed (auth flow, all CRUD, error cases)
- Frontend: Splash → Login → Dashboard E2E verified, all tabs reachable, OrderModal places orders correctly
