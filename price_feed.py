# price_feed.py
import json
import asyncio
import os
import websockets
from collections import deque
from dotenv import load_dotenv

load_dotenv()

class BinancePriceFeed:
    def __init__(self):
        self.current_price = None
        self.window_open_price = None
        self.price_history = deque(maxlen=60)  # last 60 ticks
        self._ws = None

    async def connect(self):
        uri = os.getenv("BINANCE_WS", "wss://stream.binance.us:9443/ws/btcusdt@kline_1m")
        self._ws = await websockets.connect(uri)
        asyncio.create_task(self._listen())

    async def _listen(self):
        async for msg in self._ws:
            data = json.loads(msg)
            kline = data.get("k", {})
            self.current_price = float(kline.get("c", 0))  # close price
            self.price_history.append(self.current_price)

    def set_window_open(self, price):
        """Call this at the start of each 5-min window."""
        self.window_open_price = price

    def get_window_delta(self):
        """How much BTC has moved since the window opened."""
        if self.window_open_price and self.current_price:
            return (self.current_price - self.window_open_price) / self.window_open_price
        return 0.0

    def get_momentum(self, lookback=10):
        """Simple momentum: avg price change over last N ticks."""
        if len(self.price_history) < lookback + 1:
            return 0.0
        recent = list(self.price_history)[-lookback:]
        return (recent[-1] - recent[0]) / recent[0]
