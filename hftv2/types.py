from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Literal


class Venue(IntEnum):
    BINANCE = 0
    BITGET = 1
    HYPERLIQUID = 2
    MEXC = 3

    @property
    def key(self) -> str:
        return {
            Venue.BINANCE: "binance",
            Venue.BITGET: "bitget",
            Venue.HYPERLIQUID: "hyperliquid",
            Venue.MEXC: "mexc",
        }[self]

    @classmethod
    def from_key(cls, key: str) -> "Venue":
        mapping = {
            "binance": cls.BINANCE,
            "bitget": cls.BITGET,
            "hyperliquid": cls.HYPERLIQUID,
            "hl": cls.HYPERLIQUID,
            "mexc": cls.MEXC,
        }
        try:
            return mapping[key.lower()]
        except KeyError as exc:
            raise ValueError(f"unknown venue: {key}") from exc


@dataclass(slots=True)
class Quote:
    recv_ns: int
    exch_ts_ms: int
    venue: Venue
    pair_id: str
    native_symbol: str
    bid: float
    ask: float
    bid_sz: float
    ask_sz: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) * 0.5

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0:
            return 0.0
        return (self.ask - self.bid) / mid * 10_000.0


@dataclass(slots=True)
class Leader:
    venue: Venue
    symbol: str
    primary: bool
    px_mult: float = 1.0


@dataclass(slots=True)
class PairConfig:
    id: str
    kind: Literal["crypto", "stock"]
    mexc: str
    zero_taker: bool
    leaders: list[Leader]
    impulse_bps: float
    edge_bps: float
    impulse_cooldown_ms: int
    mode: str = "strict"
    contract_size: float = 1.0

    @property
    def primary(self) -> Leader:
        for leader in self.leaders:
            if leader.primary:
                return leader
        return self.leaders[0]


@dataclass(slots=True)
class Thresholds:
    lookback_ms: int
    impulse_bps: float
    edge_bps: float
    residual_frac: float
    follow_timeout_ms: int
    follow_frac: float
    impulse_cooldown_ms: int
    paper_timeout_ms: int
    paper_exit_bps: float
    max_spread_bps: float
    max_notional_usd: float
    crypto_impulse_bps: float
    crypto_edge_bps: float
    stock_impulse_bps: float
    stock_edge_bps: float
    fill_delay_ms: int
    fill_delay_jitter_ms: int
    leverage: float
    start_equity_usd: float
    liq_price_bps: float
    quote_min_interval_ms: int
    fill_keep_frac: float


@dataclass(slots=True)
class LiveConfig:
    """Web-token MEXC futures. Orders stay off unless enabled and dry_run is false."""

    enabled: bool = False
    dry_run: bool = True
    leverage: float = 50.0
    open_type: int = 1
    timeout_sec: float = 10.0
    base_url: str = "https://futures.mexc.com/api/v1"
    tp_bps: float = 3.0
    sl_bps: float = 100.0
    attach_tpsl: bool = True


@dataclass(slots=True)
class AppConfig:
    hours: float
    data_dir: str
    status_every_sec: float
    thresholds: Thresholds
    pairs: list[PairConfig]
    live: LiveConfig
