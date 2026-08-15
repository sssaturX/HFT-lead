from __future__ import annotations

import asyncio
import json
import logging

from hftv2.feeds import Feed, now_ns, parse_ts_ms, sleep_or_stop
from hftv2.types import Quote, Venue

log = logging.getLogger("hftv2.bitget")

URL = "wss://ws.bitget.com/v2/ws/public"


class BitgetFeed(Feed):
    name = "bitget"
    url = URL

    def __init__(self, emit, native_to_pair: dict[str, str], px_mult: dict[str, float] | None = None) -> None:
        super().__init__(emit)
        self.native_to_pair = {k.upper(): v for k, v in native_to_pair.items()}
        self.px_mult = {k.upper(): float(v) for k, v in (px_mult or {}).items()}

    async def _session(self) -> None:
        async with self._connect() as ws:
            args = [
                {"instType": "USDT-FUTURES", "channel": "ticker", "instId": native}
                for native in self.native_to_pair
            ]
            for i in range(0, len(args), 20):
                await ws.send(json.dumps({"op": "subscribe", "args": args[i : i + 20]}))
            log.info("bitget subscribed %d tickers", len(self.native_to_pair))
            ping_task = asyncio.create_task(self._ping_loop(ws), name="bitget-ping")
            try:
                while not self._stop.is_set():
                    raw = await ws.recv()
                    recv_ns = now_ns()
                    if raw == "ping":
                        await ws.send("pong")
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="ignore")
                    if raw in ("pong", "ping"):
                        continue
                    msg = json.loads(raw)
                    if msg.get("event") in ("subscribe", "error"):
                        if msg.get("event") == "error":
                            log.warning("bitget error: %s", msg)
                        continue
                    arg = msg.get("arg") or {}
                    native = str(arg.get("instId") or "")
                    pair_id = self.native_to_pair.get(native.upper())
                    rows = msg.get("data") or []
                    if not pair_id or not rows:
                        continue
                    row = rows[0]
                    mult = self.px_mult.get(native.upper(), 1.0)
                    bid = float(row.get("bidPr") or 0) * mult
                    ask = float(row.get("askPr") or 0) * mult
                    if bid <= 0 or ask <= 0:
                        continue
                    await self.emit(
                        Quote(
                            recv_ns=recv_ns,
                            exch_ts_ms=parse_ts_ms(row.get("ts") or msg.get("ts"), recv_ns),
                            venue=Venue.BITGET,
                            pair_id=pair_id,
                            native_symbol=native,
                            bid=bid,
                            ask=ask,
                            bid_sz=float(row.get("bidSz") or 0),
                            ask_sz=float(row.get("askSz") or 0),
                        )
                    )
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws) -> None:
        try:
            while not self._stop.is_set():
                await ws.send("ping")
                await sleep_or_stop(self._stop, 20)
        except Exception:
            return
