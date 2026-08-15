from __future__ import annotations

import random
from dataclasses import dataclass

from hftv2.engine.signal import Signal
from hftv2.maths import residual_bps
from hftv2.types import Quote, Thresholds


@dataclass(slots=True)
class Position:
    pair_id: str
    direction: int
    entry: float
    opened_ns: int
    qty: float
    impulse_bps: float
    residual_bps: float
    notional: float


@dataclass(slots=True)
class Fill:
    pair_id: str
    side: str
    action: str
    price: float
    qty: float
    recv_ns: int
    impulse_bps: float
    residual_bps: float
    pnl_bps: float | None
    hold_ms: float | None
    reason: str
    pnl_usd: float | None = None
    equity: float | None = None


@dataclass(slots=True)
class PendingOpen:
    signal: Signal
    ready_ns: int


@dataclass(slots=True)
class PendingClose:
    pos: Position
    reason: str
    ready_ns: int
    impulse: float
    residual: float


class PaperPortfolio:
    """One account, one position. Entry/exit delayed; 1% adverse = liq stop."""

    def __init__(
        self,
        thresholds: Thresholds,
        rng: random.Random | None = None,
        contract_sizes: dict[str, float] | None = None,
    ) -> None:
        self.t = thresholds
        self.rng = rng or random.Random()
        # MEXC book sizes arrive in contracts; convert to base units for the cap.
        self.contract_sizes = contract_sizes or {}
        self.equity = thresholds.start_equity_usd
        self.start_equity = thresholds.start_equity_usd
        self.position: Position | None = None
        self.pending_open: PendingOpen | None = None
        self.pending_close: PendingClose | None = None
        self.dead = False
        self.n_liq = 0
        self.n_stale = 0
        self.n_skip_busy = 0
        self.n_skip_fade = 0

    def _delay_ns(self) -> int:
        base = self.t.fill_delay_ms
        jitter = self.t.fill_delay_jitter_ms
        ms = base if jitter <= 0 else max(0, base + self.rng.randint(-jitter, jitter))
        return ms * 1_000_000

    def busy(self) -> bool:
        return (
            self.dead
            or self.position is not None
            or self.pending_open is not None
            or self.pending_close is not None
        )

    def on_signal(self, signal: Signal) -> None:
        if self.dead:
            return
        if self.busy():
            self.n_skip_busy += 1
            return
        if signal.reason not in ("long", "short"):
            return
        self.pending_open = PendingOpen(signal, signal.recv_ns + self._delay_ns())

    def on_quotes(self, pair_id: str, leader: Quote, mexc: Quote, impulse: float | None) -> list[Fill]:
        fills: list[Fill] = []
        now = max(leader.recv_ns, mexc.recv_ns)
        if self.pending_open and self.pending_open.signal.pair_id == pair_id:
            fill = self._try_open(leader, mexc, now)
            if fill is not None:
                fills.append(fill)
        pos = self.position
        if pos is not None and pos.pair_id == pair_id and self.pending_close is None:
            liq = self._adverse_bps(pos, mexc)
            if liq >= self.t.liq_price_bps:
                closed = self._close_now(mexc, now, impulse, residual_bps(mexc, leader), "liq")
                if closed is not None:
                    fills.append(closed)
                    return fills
            hold_ns = now - pos.opened_ns
            timeout = hold_ns >= self.t.paper_timeout_ms * 1_000_000
            residual = residual_bps(mexc, leader)
            converged = abs(residual) <= self.t.paper_exit_bps
            reversed_impulse = False
            if impulse is not None:
                reversed_impulse = impulse * pos.direction <= -abs(pos.impulse_bps) * 0.8
            if timeout or converged or reversed_impulse:
                reason = "timeout" if timeout else "reverse" if reversed_impulse else "converge"
                self.pending_close = PendingClose(
                    pos=pos,
                    reason=reason,
                    ready_ns=now + self._delay_ns(),
                    impulse=impulse or 0.0,
                    residual=residual,
                )
        if self.pending_close and self.pending_close.pos.pair_id == pair_id and now >= self.pending_close.ready_ns:
            pc = self.pending_close
            closed = self._close_now(mexc, now, pc.impulse, pc.residual, pc.reason)
            if closed is not None:
                fills.append(closed)
        return fills

    def _try_open(self, leader: Quote, mexc: Quote, now: int) -> Fill | None:
        pending = self.pending_open
        if pending is None or now < pending.ready_ns:
            return None
        self.pending_open = None
        signal = pending.signal
        if mexc.spread_bps > self.t.max_spread_bps:
            self.n_stale += 1
            return None
        live_res = residual_bps(mexc, leader)
        if not self._edge_still_alive(signal, live_res):
            self.n_skip_fade += 1
            return None
        if signal.direction > 0:
            price = mexc.ask
            qty_cap = mexc.ask_sz
        else:
            price = mexc.bid
            qty_cap = mexc.bid_sz
        if price <= 0 or self.equity <= 0:
            self.n_stale += 1
            return None
        notional = self.equity * self.t.leverage
        qty = notional / price
        if qty_cap > 0:
            qty = min(qty, qty_cap * self.contract_sizes.get(signal.pair_id, 1.0))
        if qty <= 0:
            self.n_stale += 1
            return None
        self.position = Position(
            pair_id=signal.pair_id,
            direction=signal.direction,
            entry=price,
            opened_ns=now,
            qty=qty,
            impulse_bps=signal.impulse_bps,
            residual_bps=live_res,
            notional=qty * price,
        )
        return Fill(
            pair_id=signal.pair_id,
            side="buy" if signal.direction > 0 else "sell",
            action="open",
            price=price,
            qty=qty,
            recv_ns=now,
            impulse_bps=signal.impulse_bps,
            residual_bps=live_res,
            pnl_bps=None,
            hold_ms=None,
            reason=signal.reason,
            equity=self.equity,
        )

    def _edge_still_alive(self, signal: Signal, live_res: float) -> bool:
        frac = self.t.fill_keep_frac
        if frac <= 0:
            return True
        need = max(self.t.paper_exit_bps, signal.edge_bps * frac)
        if signal.direction > 0:
            return live_res <= -need
        return live_res >= need

    def _adverse_bps(self, pos: Position, mexc: Quote) -> float:
        mark = mexc.mid
        if pos.direction > 0:
            return (pos.entry - mark) / pos.entry * 10_000.0
        return (mark - pos.entry) / pos.entry * 10_000.0

    def _close_now(
        self, mexc: Quote, now: int, impulse: float, residual: float, reason: str
    ) -> Fill | None:
        pos = self.position
        self.pending_close = None
        if pos is None:
            return None
        if pos.direction > 0:
            price = mexc.bid
            pnl_bps = (price - pos.entry) / pos.entry * 10_000.0
        else:
            price = mexc.ask
            pnl_bps = (pos.entry - price) / pos.entry * 10_000.0
        pnl_usd = pos.qty * (price - pos.entry) * pos.direction
        self.equity += pnl_usd
        if reason == "liq":
            self.n_liq += 1
        if self.equity <= 0:
            self.equity = 0.0
            self.dead = True
        self.position = None
        hold_ms = (now - pos.opened_ns) / 1_000_000.0
        return Fill(
            pair_id=pos.pair_id,
            side="sell" if pos.direction > 0 else "buy",
            action="close",
            price=price,
            qty=pos.qty,
            recv_ns=now,
            impulse_bps=impulse,
            residual_bps=residual,
            pnl_bps=pnl_bps,
            hold_ms=hold_ms,
            reason=reason,
            pnl_usd=pnl_usd,
            equity=self.equity,
        )
