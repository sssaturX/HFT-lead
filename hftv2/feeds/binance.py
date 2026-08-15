from __future__ import annotations

import json
import logging

from hftv2.feeds import Feed, now_ns, parse_ts_ms
from hftv2.types import Quote, Venue

log = logging.getLogger("hftv2.binance")

FSTREAM = "wss://fstream.binance.com/stream"


class BinanceFeed(Feed):
    name = "binance"

    def __init__(self, emit, native_to_pair: dict[str, str], px_mult: dict[str, float] | None = None) -> None:
        super().__init__(emit)
        self.native_to_pair = {k.upper(): v for k, v in native_to_pair.items()}
        self.px_mult = {k.upper(): float(v) for k, v in (px_mult or {}).items()}

    async def _session(self) -> None:
        streams = []
        for native in self.native_to_pair:
            low = native.lower()
            streams.append(f"{low}@bookTicker")
        url = FSTREAM + "?streams=" + "/".join(streams)
        async with self._connect(url) as ws:
            log.info("binance connected, %d symbols", len(self.native_to_pair))
            while not self._stop.is_set():
                raw = await ws.recv()
                recv_ns = now_ns()
                msg = json.loads(raw)
                data = msg.get("data") or msg
                if data.get("e") not in ("bookTicker", None) and "b" not in data:
                    continue
                native = str(data.get("s") or "")
                pair_id = self.native_to_pair.get(native)
                if not pair_id:
                    continue
                mult = self.px_mult.get(native, 1.0)
                bid = float(data["b"]) * mult
                ask = float(data["a"]) * mult
                await self.emit(
                    Quote(
                        recv_ns=recv_ns,
                        exch_ts_ms=parse_ts_ms(data.get("E") or data.get("T"), recv_ns),
                        venue=Venue.BINANCE,
                        pair_id=pair_id,
                        native_symbol=native,
                        bid=bid,
                        ask=ask,
                        bid_sz=float(data.get("B") or 0),
                        ask_sz=float(data.get("A") or 0),
                    )
                )
