from __future__ import annotations

import asyncio
import json
import logging
import zlib

from hftv2.feeds import Feed, now_ns, parse_ts_ms, sleep_or_stop
from hftv2.types import Quote, Venue

log = logging.getLogger("hftv2.mexc")

URL = "wss://contract.mexc.com/edge"


def _decode(raw: str | bytes) -> dict:
    if isinstance(raw, bytes):
        for decoder in (
            lambda b: json.loads(b),
            lambda b: json.loads(zlib.decompress(b)),
            lambda b: json.loads(zlib.decompress(b, -zlib.MAX_WBITS)),
        ):
            try:
                return decoder(raw)
            except Exception:
                continue
        raise ValueError("unreadable mexc frame")
    return json.loads(raw)


def _top(levels: list) -> tuple[float, float]:
    if not levels:
        return 0.0, 0.0
    row = levels[0]
    if isinstance(row, dict):
        return float(row.get("price") or row.get("p") or 0), float(
            row.get("volume") or row.get("v") or row.get("qty") or 0
        )
    return float(row[0]), float(row[1] if len(row) > 1 else 0)


def _best_bid_ask(bids: list, asks: list) -> tuple[float, float, float, float] | None:
    if not bids or not asks:
        return None
    bid, bid_sz = _top(bids)
    ask, ask_sz = _top(asks)
    if bid > 0 and ask > 0 and bid <= ask:
        return bid, ask, bid_sz, ask_sz
    bid2, bid_sz2 = _top(list(reversed(bids)))
    ask2, ask_sz2 = _top(list(reversed(asks)))
    if bid2 > 0 and ask2 > 0 and bid2 <= ask2:
        return bid2, ask2, bid_sz2, ask_sz2
    return None


class MexcFeed(Feed):
    name = "mexc"
    url = URL

    def __init__(self, emit, symbols: list[str]) -> None:
        super().__init__(emit)
        self.symbols = symbols

    async def _session(self) -> None:
        async with self._connect() as ws:
            for symbol in self.symbols:
                await ws.send(
                    json.dumps(
                        {"method": "sub.depth.full", "param": {"symbol": symbol, "limit": 5}}
                    )
                )
                await ws.send(json.dumps({"method": "sub.ticker", "param": {"symbol": symbol}}))
            log.info("mexc subscribed depth.full+ticker x%d", len(self.symbols))
            ping_task = asyncio.create_task(self._ping_loop(ws), name="mexc-ping")
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    recv_ns = now_ns()
                    msg = _decode(raw)
                    channel = str(msg.get("channel") or "")
                    if channel.startswith("rs."):
                        continue
                    if channel == "pong":
                        continue
                    if "depth" not in channel and channel != "push.ticker":
                        continue
                    data = msg.get("data") or {}
                    symbol = str(msg.get("symbol") or data.get("symbol") or "")
                    if symbol not in self.symbols and symbol:
                        continue
                    if not symbol:
                        continue
                    bids = data.get("bids") or data.get("b") or []
                    asks = data.get("asks") or data.get("a") or []
                    if channel == "push.ticker":
                        bid = float(data.get("bid1") or 0)
                        ask = float(data.get("ask1") or 0)
                        bid_sz = float(data.get("bid1Vol") or 0)
                        ask_sz = float(data.get("ask1Vol") or 0)
                    else:
                        top = _best_bid_ask(bids, asks)
                        if top is None:
                            continue
                        bid, ask, bid_sz, ask_sz = top
                    if bid <= 0 or ask <= 0 or bid > ask:
                        continue
                    await self.emit(
                        Quote(
                            recv_ns=recv_ns,
                            exch_ts_ms=parse_ts_ms(
                                data.get("cts") or data.get("timestamp") or msg.get("ts"),
                                recv_ns,
                            ),
                            venue=Venue.MEXC,
                            pair_id=symbol,
                            native_symbol=symbol,
                            bid=bid,
                            ask=ask,
                            bid_sz=bid_sz,
                            ask_sz=ask_sz,
                        )
                    )
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws) -> None:
        try:
            while not self._stop.is_set():
                await ws.send(json.dumps({"method": "ping"}))
                await sleep_or_stop(self._stop, 15)
        except Exception:
            return
