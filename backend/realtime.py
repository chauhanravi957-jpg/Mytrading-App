import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Dict, Set

try:
    import upstox_real
except Exception:
    upstox_real = None

try:
    import websockets
except ImportError:
    websockets = None

# Try to import position_exit_manager for LTP updates
try:
    import position_exit_manager
except ImportError:
    position_exit_manager = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("realtime")


class AsyncRealtimeServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 6789, poll_interval: float = 0.5, heartbeat_interval: float = 20.0, ping_timeout: float = 10.0, access_token: str = None):
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.ping_timeout = ping_timeout
        self.clients: Set = set()
        self.client_symbols: Dict = defaultdict(set)
        self.subscribers: Dict[str, Set] = defaultdict(set)
        self.tick_cache: Dict[str, dict] = {}
        self._stop = False
        # WebSocket stream management (use Upstox real-time feed when available)
        self._ws_task = None
        self._ws_stop_event = None
        self._last_ws_symbols = None
        # server-side external subscriptions (allow subscribing even without WS clients)
        self.external_subscriptions: Set[str] = set()
        # Access token to use when opening Upstox websocket
        self.access_token = access_token

    def _cleanup_client(self, websocket):
        symbols = self.client_symbols.pop(websocket, set())
        for symbol in symbols:
            self.subscribers[symbol].discard(websocket)
            if not self.subscribers[symbol]:
                self.subscribers.pop(symbol, None)
        self.clients.discard(websocket)

    async def _fetch_tick_data(self, symbols):
        if not symbols:
            return []

        if upstox_real and (upstox_real.is_configured() or getattr(self, 'access_token', None)):
            instrument_keys = [upstox_real.INSTRUMENT_KEYS.get(symbol, symbol) for symbol in symbols]
            try:
                access_token = getattr(self, 'access_token', None)
                data = await asyncio.to_thread(upstox_real.fetch_market_quote, access_token, instrument_keys)
            except Exception:
                logger.exception("Failed to fetch market quote from Upstox")
                return []

            ticks = []
            for key, payload in (data or {}).items():
                symbol = next((friendly for friendly, actual in upstox_real.INSTRUMENT_KEYS.items() if actual == key), key)
                ltp = payload.get("last_price") or payload.get("ltp") or 0
                if ltp > 0:
                    ticks.append({"symbol": symbol, "ltp": ltp, "ts": time.time()})
            return ticks

        return []

    async def _broadcast_ticks(self, ticks):
        if not ticks:
            return

        to_broadcast = defaultdict(list)
        for tick in ticks:
            symbol = tick["symbol"]
            last = self.tick_cache.get(symbol)
            if last and last.get("ltp") == tick.get("ltp"):
                continue
            self.tick_cache[symbol] = tick
            
            # Update position exit manager with LTP data
            if position_exit_manager:
                try:
                    ltp = tick.get("ltp")
                    if ltp and ltp > 0:
                        # Try to update using the symbol (which could be instrument_key)
                        position_exit_manager.update_ltp(symbol, ltp)
                except Exception as e:
                    logger.warning("Failed to update position exit manager with tick: %s", str(e))
            
            for ws in list(self.subscribers.get(symbol, [])):
                to_broadcast[ws].append(tick)

        for ws, tick_batch in list(to_broadcast.items()):
            try:
                await ws.send(json.dumps({"type": "tick", "data": tick_batch}))
            except Exception as exc:
                logger.exception('Realtime websocket send failed')
                self._cleanup_client(ws)

    async def handler(self, websocket, path=None):
        self.clients.add(websocket)
        self.client_symbols[websocket] = set()
        logger.info("Realtime client connected %s", id(websocket))

        async def recv_loop():
            try:
                async for message in websocket:
                    try:
                        payload = json.loads(message)
                    except Exception:
                        payload = None
                    if not payload:
                        continue
                    action = payload.get("action")
                    if action == "subscribe":
                        symbols = payload.get("symbols", []) or []
                        for symbol in symbols:
                            if symbol not in self.client_symbols[websocket]:
                                self.client_symbols[websocket].add(symbol)
                                self.subscribers[symbol].add(websocket)
                        await websocket.send(json.dumps({"type": "ack", "subscribed": sorted(self.client_symbols[websocket])}))
                    elif action == "unsubscribe":
                        symbols = payload.get("symbols", []) or []
                        for symbol in symbols:
                            self.client_symbols[websocket].discard(symbol)
                            self.subscribers[symbol].discard(websocket)
                            if not self.subscribers[symbol]:
                                self.subscribers.pop(symbol, None)
                        await websocket.send(json.dumps({"type": "ack", "subscribed": sorted(self.client_symbols[websocket])}))
                    elif action == "ping":
                        await websocket.send(json.dumps({"type": "pong", "ts": time.time()}))
            except Exception:
                logger.debug("Realtime recv loop ended for %s", id(websocket))

        recv_task = asyncio.create_task(recv_loop())

        try:
            await recv_task
        finally:
            self._cleanup_client(websocket)
            logger.info("Realtime client disconnected %s", id(websocket))

    async def poll_loop(self):
        backoff = 1.0
        while not self._stop:
            try:
                # include server-side external subscriptions so backend can subscribe without connected clients
                symbols = set([symbol for symbol, subs in self.subscribers.items() if subs]) | set(self.external_subscriptions)
                symbols = list(symbols)

                # If upstox_real is available and we have configuration or an access token,
                # prefer the real-time websocket feed
                if upstox_real and (upstox_real.is_configured() or getattr(self, 'access_token', None)):
                    # When there are subscribers, ensure a WS stream is running for them
                    if symbols:
                        instrument_keys = [upstox_real.INSTRUMENT_KEYS.get(symbol, symbol) for symbol in symbols]
                        if instrument_keys != self._last_ws_symbols:
                            # restart stream with new instrument set
                            if self._ws_stop_event:
                                try:
                                    self._ws_stop_event.set()
                                except Exception:
                                    pass
                            self._ws_stop_event = asyncio.Event()

                            async def _on_tick(ticks):
                                await self._broadcast_ticks(ticks)

                            # start the upstox WS stream in background
                            try:
                                self._ws_task = asyncio.create_task(
                                    upstox_real.stream_ticks(self.access_token, instrument_keys, _on_tick, stop_event=self._ws_stop_event)
                                )
                                self._last_ws_symbols = instrument_keys
                            except Exception:
                                logger.exception("Failed to start Upstox WS stream")
                    else:
                        # no subscribers — stop any running WS stream
                        if self._ws_stop_event:
                            try:
                                self._ws_stop_event.set()
                            except Exception:
                                pass
                            self._ws_task = None
                            self._last_ws_symbols = None

                    await asyncio.sleep(self.poll_interval)
                else:
                    # fallback: REST poll for LTPs (existing behavior)
                    if symbols:
                        ticks = await self._fetch_tick_data(symbols)
                        await self._broadcast_ticks(ticks)
                    await asyncio.sleep(self.poll_interval)
                backoff = 1.0
            except Exception:
                logger.exception("Realtime poll loop error, backing off")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def start(self):
        if websockets is None:
            raise RuntimeError("Missing required dependency 'websockets'. Install it with: pip install websockets")

        self._stop = False
        try:
            server = await websockets.serve(
                self.handler,
                self.host,
                self.port,
                ping_interval=self.heartbeat_interval,
                ping_timeout=self.ping_timeout,
            )
        except OSError as exc:
            if exc.errno == 10048:
                logger.warning(
                    "Realtime WebSocket port %s is already in use; realtime feed will not start.",
                    self.port,
                )
                return
            raise

        logger.info("Realtime WebSocket server started on ws://%s:%s", self.host, self.port)
        poll_task = asyncio.create_task(self.poll_loop())
        try:
            await server.wait_closed()
        finally:
            self._stop = True
            poll_task.cancel()


def run_background(host: str = "0.0.0.0", port: int = 6789, poll_interval: float = 0.5, access_token: str = None):
    loop = asyncio.new_event_loop()

    async def runner():
        srv = AsyncRealtimeServer(host=host, port=port, poll_interval=poll_interval, access_token=access_token)
        # expose the running server instance for external control
        global CURRENT_SERVER
        CURRENT_SERVER = srv
        await srv.start()

    try:
        loop.run_until_complete(runner())
    except RuntimeError:
        logger.exception("Realtime server could not start")
    except Exception:
        logger.exception("realtime server stopped")


# Module-level handle to the running AsyncRealtimeServer (set by run_background)
CURRENT_SERVER = None


def add_external_subscriptions(symbols):
    """Add server-side subscriptions (symbols are friendly names)."""
    global CURRENT_SERVER
    if not CURRENT_SERVER:
        raise RuntimeError("Realtime server not running")
    for s in symbols:
        CURRENT_SERVER.external_subscriptions.add(s)


def remove_external_subscriptions(symbols):
    global CURRENT_SERVER
    if not CURRENT_SERVER:
        raise RuntimeError("Realtime server not running")
    for s in symbols:
        CURRENT_SERVER.external_subscriptions.discard(s)


def set_access_token(access_token: str):
    """Update the access token used by the running realtime server."""
    global CURRENT_SERVER
    if not CURRENT_SERVER:
        return
    CURRENT_SERVER.access_token = access_token
    # Force restart of any active Upstox WS stream so new token is used.
    if CURRENT_SERVER._ws_stop_event and not CURRENT_SERVER._ws_stop_event.is_set():
        try:
            CURRENT_SERVER._ws_stop_event.set()
        except Exception:
            pass
    CURRENT_SERVER._ws_task = None
    CURRENT_SERVER._last_ws_symbols = None
