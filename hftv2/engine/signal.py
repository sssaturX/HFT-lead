from __future__ import annotations

from dataclasses import dataclass

from hftv2.engine import MidHistory
from hftv2.maths import residual_bps
from hftv2.types import PairConfig, Quote, Thresholds


@dataclass(slots=True)
class Signal:
    recv_ns: int
    pair_id: str
    direction: int
    impulse_bps: float
    residual_bps: float
    leader: Quote
    mexc: Quote
    reason: str
    edge_bps: float = 0.0


def want_long(impulse: float, residual: float, impulse_min: float, edge_min: float, frac: float) -> bool:
    return (
        impulse >= impulse_min
        and residual <= -edge_min
        and abs(residual) >= frac * abs(impulse)
    )


def want_short(impulse: float, residual: float, impulse_min: float, edge_min: float, frac: float) -> bool:
    return (
        impulse <= -impulse_min
        and residual >= edge_min
        and abs(residual) >= frac * abs(impulse)
    )


class SignalEngine:
    def __init__(self, pair: PairConfig, thresholds: Thresholds) -> None:
        self.pair = pair
        self.thresholds = thresholds
        self.history = MidHistory(thresholds.lookback_ms)
        self._last_impulse_ns = 0

    def on_leader(self, leader: Quote, mexc: Quote | None) -> tuple[float | None, Signal | None]:
        impulse = self.history.impulse_bps(leader.recv_ns, leader.mid)
        if impulse is None or mexc is None:
            return impulse, None
        residual = residual_bps(mexc, leader)
        cooldown_ns = self.pair.impulse_cooldown_ms * 1_000_000
        if leader.recv_ns - self._last_impulse_ns < cooldown_ns:
            return impulse, None
        if abs(impulse) < self.pair.impulse_bps:
            return impulse, None
        direction = 1 if impulse > 0 else -1
        self._last_impulse_ns = leader.recv_ns
        return impulse, Signal(
            recv_ns=leader.recv_ns,
            pair_id=self.pair.id,
            direction=direction,
            impulse_bps=impulse,
            residual_bps=residual,
            leader=leader,
            mexc=mexc,
            reason="impulse",
        )

    def trade_signal(self, leader: Quote, mexc: Quote, impulse: float) -> Signal | None:
        residual = residual_bps(mexc, leader)
        t = self.thresholds
        if mexc.spread_bps > t.max_spread_bps:
            return None
        if want_long(impulse, residual, self.pair.impulse_bps, self.pair.edge_bps, t.residual_frac):
            return Signal(
                recv_ns=max(leader.recv_ns, mexc.recv_ns),
                pair_id=self.pair.id,
                direction=1,
                impulse_bps=impulse,
                residual_bps=residual,
                leader=leader,
                mexc=mexc,
                reason="long",
                edge_bps=self.pair.edge_bps,
            )
        if want_short(impulse, residual, self.pair.impulse_bps, self.pair.edge_bps, t.residual_frac):
            return Signal(
                recv_ns=max(leader.recv_ns, mexc.recv_ns),
                pair_id=self.pair.id,
                direction=-1,
                impulse_bps=impulse,
                residual_bps=residual,
                leader=leader,
                mexc=mexc,
                reason="short",
                edge_bps=self.pair.edge_bps,
            )
        return None
