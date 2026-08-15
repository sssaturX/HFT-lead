from __future__ import annotations

import asyncio
import json
import logging

from hftv2.feeds import Feed, now_ns, parse_ts_ms, sleep_or_stop
from hftv2.types import Quote, Venue

log = logging.getLogger("hftv2.hyperliquid")

URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidFeed(Feed):
    name = "hyperliquid"
    url = URL

    def __init__(self, emit, native_to_pair: dict[str, str], px_mult: dict[str, float] | None = None) -> None:
        super().__init__(emit)
        self.native_to_pair = native_to_pair
        self.px_mult = dict(px_mult or {})

    async def _session(self) -> None:
        async with self._connect() as ws:
            for coin in self.native_to_pair:
                await ws.send(
                    json.dumps(
                        {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
                    )
                )
            log.info("hyperliquid subscribed %s", list(self.native_to_pair))
            ping_task = asyncio.create_task(self._ping_loop(ws), name="hl-ping")
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    recv_ns = now_ns()
                    msg = json.loads(raw)
                    if msg.get("channel") != "l2Book":
                        continue
                    data = msg.get("data") or {}
                    coin = str(data.get("coin") or "")
                    pair_id = self.native_to_pair.get(coin)
                    levels = data.get("levels") or []
                    if not pair_id or len(levels) < 2:
                        continue
                    bids, asks = levels[0], levels[1]
                    if not bids or not asks:
                        continue
                    mult = self.px_mult.get(coin, 1.0)
                    bid = float(bids[0]["px"]) * mult
                    ask = float(asks[0]["px"]) * mult
                    await self.emit(
                        Quote(
                            recv_ns=recv_ns,
                            exch_ts_ms=parse_ts_ms(data.get("time"), recv_ns),
                            venue=Venue.HYPERLIQUID,
                            pair_id=pair_id,
                            native_symbol=coin,
                            bid=bid,
                            ask=ask,
                            bid_sz=float(bids[0].get("sz") or 0),
                            ask_sz=float(asks[0].get("sz") or 0),
                        )
                    )
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws) -> None:
        try:
            while not self._stop.is_set():
                await ws.send(json.dumps({"method": "ping"}))
                await sleep_or_stop(self._stop, 20)
        except Exception:
            return
