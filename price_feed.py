# price_feed.py
import json
import asyncio
import time
import logging
import websockets
from collections import deque
from dotenv import load_dotenv
import os

load_dotenv()
log = logging.getLogger("price_feed")


class BinancePriceFeed:
    """
    Real-time BTC price feed via Binance aggTrade WebSocket.

    Uses @aggTrade (not @kline_1m) for trade-level updates — typically
    10-50 messages/second, giving sub-second price resolution instead
    of the 1-second kline updates.

    Features:
    - Auto-reconnect with exponential backoff on disconnect
    - Readiness gating: is_ready property blocks trading until first price arrives
    - Stale price detection: is_stale flags if no update in 10+ seconds
    - Window open price auto-capture from the 5-min boundary
    - Tracks last_update_time so the bot can pause on feed failure
    """

    RECONNECT_BASE_DELAY = 1.0   # seconds
    RECONNECT_MAX_DELAY = 30.0   # seconds
    STALE_THRESHOLD = 10.0       # seconds without update = stale
    HISTORY_SIZE = 300           # ~5 min of per-second samples

    def __init__(self):
        self.ws_url = os.getenv(
            "BINANCE_WS",
            "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
        )
        self.current_price: float | None = None
        self.last_update_time: float = 0.0
        self.window_open_price: float | None = None
        self._current_window_start: int | None = None

        # Downsampled 1-per-second history for momentum calculation
        self.price_history: deque[float] = deque(maxlen=self.HISTORY_SIZE)
        self._last_history_second: int = 0

        self._ws = None
        self._running = False
        self._reconnect_delay = self.RECONNECT_BASE_DELAY
        self._connect_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self):
        """Start the WebSocket listener. Call once at bot startup."""
        self._running = True
        self._connect_task = asyncio.create_task(self._connection_loop())

    async def stop(self):
        """Gracefully shut down the WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._connect_task:
            self._connect_task.cancel()

    # ── Connection loop with auto-reconnect ────────────────────

    async def _connection_loop(self):
        """Outer loop: connects, listens, reconnects on failure."""
        while self._running:
            try:
                log.info(f"Connecting to Binance WS: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,    # Binance expects pings
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = self.RECONNECT_BASE_DELAY  # reset on success
                    log.info("Binance WS connected")
                    await self._listen(ws)

            except (
                websockets.ConnectionClosed,
                websockets.InvalidStatusCode,
                ConnectionRefusedError,
                OSError,
            ) as e:
                log.warning(f"Binance WS disconnected: {e}")

            except asyncio.CancelledError:
                log.info("Binance WS task cancelled")
                return

            except Exception as e:
                log.error(f"Unexpected Binance WS error: {e}")

            finally:
                self._ws = None

            if self._running:
                log.info(f"Reconnecting in {self._reconnect_delay:.1f}s...")
                await asyncio.sleep(self._reconnect_delay)
                # Exponential backoff, capped
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self.RECONNECT_MAX_DELAY
                )

    async def _listen(self, ws):
        """Inner loop: process each aggTrade message."""
        async for raw_msg in ws:
            try:
                msg = json.loads(raw_msg)
                # aggTrade payload: {"e":"aggTrade","p":"87654.32",...}
                price = float(msg["p"])
                self.current_price = price
                self.last_update_time = time.monotonic()

                # Downsample to 1 entry per second for history
                now_sec = int(time.time())
                if now_sec != self._last_history_second:
                    self.price_history.append(price)
                    self._last_history_second = now_sec

                # Auto-capture window open price at 5-min boundaries
                self._maybe_capture_window_open(price)

            except (KeyError, ValueError) as e:
                log.debug(f"Skipping malformed aggTrade message: {e}")

    # ── Window open price auto-capture ─────────────────────────

    def _maybe_capture_window_open(self, price: float):
        """
        Automatically set window_open_price at each 5-min boundary.
        Detects when the window has changed and captures the first price.
        """
        now = int(time.time())
        current_window = now - (now % 300)
        if current_window != self._current_window_start:
            self._current_window_start = current_window
            self.window_open_price = price
            log.info(
                f"New 5-min window {current_window} | "
                f"Open price: ${price:,.2f}"
            )

    # ── Readiness checks ───────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """True once we have received at least one price."""
        return self.current_price is not None

    @property
    def is_stale(self) -> bool:
        """True if no price update received in STALE_THRESHOLD seconds."""
        if self.last_update_time == 0:
            return True
        return (time.monotonic() - self.last_update_time) > self.STALE_THRESHOLD

    @property
    def is_connected(self) -> bool:
        """True if WebSocket connection is currently open."""
        return self._ws is not None and self._ws.open

    async def wait_until_ready(self, timeout: float = 30.0):
        """
        Block until the first price arrives or timeout.
        Raises TimeoutError if no price within timeout.
        """
        start = time.monotonic()
        while not self.is_ready:
            if time.monotonic() - start > timeout:
                raise TimeoutError(
                    f"Binance price feed not ready after {timeout}s"
                )
            await asyncio.sleep(0.1)
        log.info(f"Price feed ready | BTC = ${self.current_price:,.2f}")

    # ── Price signals ──────────────────────────────────────────

    def get_window_delta(self) -> float:
        """
        Percentage change from window open to current price.
        Returns 0.0 if either price is unavailable.
        """
        if self.window_open_price and self.current_price:
            return (
                (self.current_price - self.window_open_price)
                / self.window_open_price
            )
        return 0.0

    def get_momentum(self, lookback: int = 10) -> float:
        """
        Simple momentum: price change over last `lookback` seconds.
        Uses the downsampled 1-per-second price history.
        Returns 0.0 if insufficient data.
        """
        if len(self.price_history) < lookback + 1:
            return 0.0
        recent = list(self.price_history)
        start_price = recent[-(lookback + 1)]
        end_price = recent[-1]
        if start_price == 0:
            return 0.0
        return (end_price - start_price) / start_price

    def get_volatility(self, lookback: int = 30) -> float:
        """
        Rolling standard deviation of 1-second returns.
        Useful for gauging whether a window is unusually volatile.
        """
        if len(self.price_history) < lookback + 1:
            return 0.0
        prices = list(self.price_history)[-(lookback + 1):]
        returns = [
            (prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices))
            if prices[i - 1] != 0
        ]
        if not returns:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return variance ** 0.5