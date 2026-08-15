from hftv2.engine import MidHistory
from hftv2.engine.paper import PaperPortfolio
from hftv2.engine.signal import Signal, want_long, want_short
from hftv2.maths import bps, residual_bps
from hftv2.types import Leader, PairConfig, Quote, Thresholds, Venue


def _th(**kwargs) -> Thresholds:
    base = dict(
        lookback_ms=150,
        impulse_bps=6,
        edge_bps=8,
        residual_frac=0.5,
        follow_timeout_ms=2000,
        follow_frac=0.5,
        impulse_cooldown_ms=400,
        paper_timeout_ms=2500,
        paper_exit_bps=1.5,
        max_spread_bps=8,
        max_notional_usd=200,
        crypto_impulse_bps=6,
        crypto_edge_bps=8,
        stock_impulse_bps=8,
        stock_edge_bps=10,
        fill_delay_ms=0,
        fill_delay_jitter_ms=0,
        leverage=50,
        start_equity_usd=20,
        liq_price_bps=100,
        quote_min_interval_ms=10,
        fill_keep_frac=0.5,
    )
    base.update(kwargs)
    return Thresholds(**base)


def _pair() -> PairConfig:
    return PairConfig(
        id="SOL_USDT",
        kind="crypto",
        mexc="SOL_USDT",
        zero_taker=True,
        leaders=[Leader(Venue.BINANCE, "SOLUSDT", True)],
        impulse_bps=6,
        edge_bps=8,
        impulse_cooldown_ms=400,
    )


def _quote(venue: Venue, pair: str, bid: float, ask: float, recv_ns: int = 1_000_000_000) -> Quote:
    return Quote(
        recv_ns=recv_ns,
        exch_ts_ms=recv_ns // 1_000_000,
        venue=venue,
        pair_id=pair,
        native_symbol=pair,
        bid=bid,
        ask=ask,
        bid_sz=10,
        ask_sz=10,
    )


def test_bps_and_residual() -> None:
    assert abs(bps(1, 100) - 100) < 1e-9
    mexc = _quote(Venue.MEXC, "SOL_USDT", 100.0, 100.2)
    lead = _quote(Venue.BINANCE, "SOL_USDT", 100.4, 100.6)
    assert residual_bps(mexc, lead) < 0


def test_want_long_short() -> None:
    assert want_long(10, -9, 6, 8, 0.5)
    assert not want_long(10, -2, 6, 8, 0.5)
    assert not want_long(4, -9, 6, 8, 0.5)
    assert want_short(-10, 9, 6, 8, 0.5)
    assert not want_short(-10, 2, 6, 8, 0.5)


def test_mid_history_impulse() -> None:
    h = MidHistory(150)
    t0 = 1_000_000_000
    assert h.impulse_bps(t0, 100.0) is None
    later = t0 + 150_000_000
    imp = h.impulse_bps(later, 100.1)
    assert imp is not None
    assert 9.5 < imp < 10.5


def test_paper_round_trip_zero_delay() -> None:
    book = PaperPortfolio(_th())
    mexc = _quote(Venue.MEXC, "SOL_USDT", 100.0, 100.02, 10)
    lead = _quote(Venue.BINANCE, "SOL_USDT", 100.10, 100.12, 10)
    sig = Signal(
        recv_ns=10,
        pair_id="SOL_USDT",
        direction=1,
        impulse_bps=10,
        residual_bps=-10,
        leader=lead,
        mexc=mexc,
        reason="long",
    )
    book.on_signal(sig)
    opened = book.on_quotes("SOL_USDT", lead, mexc, 10.0)
    assert opened and opened[0].action == "open"
    mexc2 = _quote(Venue.MEXC, "SOL_USDT", 100.10, 100.12, 20)
    lead2 = _quote(Venue.BINANCE, "SOL_USDT", 100.10, 100.12, 20)
    closed = book.on_quotes("SOL_USDT", lead2, mexc2, 0.0)
    assert closed and closed[0].action == "close"
    assert closed[0].pnl_bps is not None and closed[0].pnl_bps > 0
    assert closed[0].equity is not None and closed[0].equity > 20


def test_fill_delay_skips_same_tick() -> None:
    book = PaperPortfolio(_th(fill_delay_ms=100, fill_delay_jitter_ms=0))
    t0 = 1_000_000_000
    mexc = _quote(Venue.MEXC, "SOL_USDT", 100.0, 100.02, t0)
    lead = _quote(Venue.BINANCE, "SOL_USDT", 100.10, 100.12, t0)
    sig = Signal(
        recv_ns=t0,
        pair_id="SOL_USDT",
        direction=1,
        impulse_bps=10,
        residual_bps=-10,
        leader=lead,
        mexc=mexc,
        reason="long",
    )
    book.on_signal(sig)
    assert book.on_quotes("SOL_USDT", lead, mexc, 10.0) == []
    later = t0 + 100_000_000
    mexc2 = _quote(Venue.MEXC, "SOL_USDT", 100.0, 100.02, later)
    lead2 = _quote(Venue.BINANCE, "SOL_USDT", 100.10, 100.12, later)
    opened = book.on_quotes("SOL_USDT", lead2, mexc2, 10.0)
    assert opened and opened[0].action == "open"


def test_liq_at_one_percent() -> None:
    book = PaperPortfolio(_th(fill_delay_ms=0, leverage=50, liq_price_bps=100))
    t0 = 1_000_000_000
    mexc = _quote(Venue.MEXC, "SOL_USDT", 100.0, 100.0, t0)
    lead = _quote(Venue.BINANCE, "SOL_USDT", 100.10, 100.12, t0)
    sig = Signal(
        recv_ns=t0,
        pair_id="SOL_USDT",
        direction=1,
        impulse_bps=10,
        residual_bps=-10,
        leader=lead,
        mexc=mexc,
        reason="long",
    )
    book.on_signal(sig)
    opened = book.on_quotes("SOL_USDT", lead, mexc, 10.0)
    assert opened and opened[0].action == "open"
    assert book.position is not None
    # price -1.2% against long
    mexc2 = _quote(Venue.MEXC, "SOL_USDT", 98.7, 98.8, t0 + 1_000_000)
    lead2 = _quote(Venue.BINANCE, "SOL_USDT", 98.7, 98.8, t0 + 1_000_000)
    fills = book.on_quotes("SOL_USDT", lead2, mexc2, 0.0)
    assert fills and fills[0].reason == "liq"
    assert book.n_liq == 1
    # 50x * 1.2% ≈ 60% equity hit, still alive
    assert book.equity > 0
    assert not book.dead


def test_fade_skips_when_residual_gone() -> None:
    book = PaperPortfolio(_th(fill_delay_ms=100, fill_delay_jitter_ms=0, fill_keep_frac=0.5))
    t0 = 1_000_000_000
    mexc = _quote(Venue.MEXC, "SOL_USDT", 100.0, 100.02, t0)
    lead = _quote(Venue.BINANCE, "SOL_USDT", 100.10, 100.12, t0)
    sig = Signal(
        recv_ns=t0,
        pair_id="SOL_USDT",
        direction=1,
        impulse_bps=10,
        residual_bps=-10,
        leader=lead,
        mexc=mexc,
        reason="long",
        edge_bps=8,
    )
    book.on_signal(sig)
    later = t0 + 100_000_000
    mexc2 = _quote(Venue.MEXC, "SOL_USDT", 100.10, 100.12, later)
    lead2 = _quote(Venue.BINANCE, "SOL_USDT", 100.10, 100.12, later)
    assert book.on_quotes("SOL_USDT", lead2, mexc2, 10.0) == []
    assert book.position is None
    assert book.n_skip_fade == 1


def test_qty_cap_scaled_by_contract_size() -> None:
    # PEPE-style: book sizes are contracts of 10M coins; the cap must not
    # shrink the position to a handful of coins.
    book = PaperPortfolio(_th(), contract_sizes={"PEPE_USDT": 10_000_000})
    t0 = 1_000_000_000
    px = 0.00000265
    mexc = _quote(Venue.MEXC, "PEPE_USDT", px, px, t0)
    mexc.bid_sz = 54
    mexc.ask_sz = 54
    lead = _quote(Venue.BINANCE, "PEPE_USDT", px * 1.001, px * 1.001, t0)
    sig = Signal(
        recv_ns=t0,
        pair_id="PEPE_USDT",
        direction=1,
        impulse_bps=10,
        residual_bps=-10,
        leader=lead,
        mexc=mexc,
        reason="long",
        edge_bps=8,
    )
    book.on_signal(sig)
    opened = book.on_quotes("PEPE_USDT", lead, mexc, 10.0)
    assert opened and opened[0].action == "open"
    # $20 * 50x = $1000 notional; without scaling qty would be capped at 54 coins.
    assert opened[0].qty * px > 900
