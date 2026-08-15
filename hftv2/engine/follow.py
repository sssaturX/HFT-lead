from __future__ import annotations

from dataclasses import dataclass

from hftv2.engine.signal import Signal
from hftv2.maths import bps
from hftv2.types import Quote, Thresholds


@dataclass(slots=True)
class PendingFollow:
    t0_ns: int
    direction: int
    impulse_bps: float
    mexc_mid_t0: float
    leader_mid_t0: float
    pair_id: str


@dataclass(slots=True)
class FollowResult:
    pair_id: str
    followed: bool
    lag_ms: float
    impulse_bps: float
    mexc_move_bps: float
    direction: int


class FollowThrough:
    def __init__(self, thresholds: Thresholds) -> None:
        self.thresholds = thresholds
        self.pending: dict[str, PendingFollow] = {}

    def on_impulse(self, signal: Signal) -> None:
        if signal.pair_id in self.pending:
            return
        self.pending[signal.pair_id] = PendingFollow(
            t0_ns=signal.recv_ns,
            direction=signal.direction,
            impulse_bps=signal.impulse_bps,
            mexc_mid_t0=signal.mexc.mid,
            leader_mid_t0=signal.leader.mid,
            pair_id=signal.pair_id,
        )

    def on_mexc(self, quote: Quote) -> FollowResult | None:
        pending = self.pending.get(quote.pair_id)
        if pending is None:
            return None
        timeout_ns = self.thresholds.follow_timeout_ms * 1_000_000
        age_ns = quote.recv_ns - pending.t0_ns
        move = bps(quote.mid - pending.mexc_mid_t0, pending.mexc_mid_t0)
        need = self.thresholds.follow_frac * abs(pending.impulse_bps)
        followed = pending.direction * move >= need
        if followed or age_ns >= timeout_ns:
            self.pending.pop(quote.pair_id, None)
            return FollowResult(
                pair_id=quote.pair_id,
                followed=followed,
                lag_ms=age_ns / 1_000_000.0,
                impulse_bps=pending.impulse_bps,
                mexc_move_bps=move,
                direction=pending.direction,
            )
        return None
