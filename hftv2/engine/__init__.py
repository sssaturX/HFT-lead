from __future__ import annotations

from collections import deque

from hftv2.types import Quote, Venue


class Books:
    def __init__(self) -> None:
        self._quotes: dict[tuple[Venue, str], Quote] = {}

    def update(self, quote: Quote) -> None:
        self._quotes[(quote.venue, quote.pair_id)] = quote

    def get(self, venue: Venue, pair_id: str) -> Quote | None:
        return self._quotes.get((venue, pair_id))


class MidHistory:
    """Keeps mids so impulse can be measured over a trailing window."""

    def __init__(self, lookback_ms: int) -> None:
        self.lookback_ns = lookback_ms * 1_000_000
        self._buf: deque[tuple[int, float]] = deque()

    def impulse_bps(self, recv_ns: int, mid: float) -> float | None:
        self._buf.append((recv_ns, mid))
        cutoff = recv_ns - self.lookback_ns
        while len(self._buf) > 1 and self._buf[1][0] <= cutoff:
            self._buf.popleft()
        if len(self._buf) < 2:
            return None
        old = self._buf[0][1]
        if old <= 0:
            return None
        return (mid - old) / old * 10_000.0
