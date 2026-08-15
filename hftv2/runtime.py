from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from hftv2.engine import Books
from hftv2.engine.follow import FollowThrough
from hftv2.engine.paper import Fill, PaperPortfolio
from hftv2.engine.signal import SignalEngine
from hftv2.feeds.binance import BinanceFeed
from hftv2.feeds.bitget import BitgetFeed
from hftv2.feeds.hyperliquid import HyperliquidFeed
from hftv2.feeds.mexc import MexcFeed
from hftv2.jsonl import JsonlWriter
from hftv2.maths import residual_bps
from hftv2.types import AppConfig, Quote, Venue

log = logging.getLogger("hftv2.runtime")


class PairStats:
    def __init__(self) -> None:
        self.n_impulse = 0
        self.n_followed = 0
        self.n_follow_done = 0
        self.lags_ms: list[float] = []
        self.n_open = 0
        self.n_close = 0
        self.pnl_bps: list[float] = []
        self.pnl_usd: list[float] = []
        self.n_liq = 0
        self.last_residual: float | None = None
        self.last_impulse: float | None = None


class Runtime:
    def __init__(
        self,
        cfg: AppConfig,
        out_dir: Path,
        skip_quotes: bool = False,
        write_events: bool = True,
        rng=None,
    ) -> None:
        self.cfg = cfg
        self.out_dir = out_dir
        self.skip_quotes = skip_quotes
        self.books = Books()
        self.follow = FollowThrough(cfg.thresholds)
        self.signals = {p.id: SignalEngine(p, cfg.thresholds) for p in cfg.pairs}
        self.paper = PaperPortfolio(
            cfg.thresholds,
            rng=rng,
            contract_sizes={p.id: p.contract_size for p in cfg.pairs},
        )
        self.primary = {p.id: p.primary.venue for p in cfg.pairs}
        self.pair_by_id = {p.id: p for p in cfg.pairs}
        self.last_impulse: dict[str, float] = {}
        self.stats = {p.id: PairStats() for p in cfg.pairs}
        self.n_quotes = 0
        self._last_quote_write: dict[tuple[int, str], int] = {}
        self._quotes: JsonlWriter | None = None
        self._events: JsonlWriter | None = None
        if write_events or not skip_quotes:
            out_dir.mkdir(parents=True, exist_ok=True)
        if write_events:
            self._events = JsonlWriter(out_dir / "events.jsonl")
        if not skip_quotes:
            self._quotes = JsonlWriter(out_dir / "quotes.jsonl", flush_every=500)
        self._queue: asyncio.Queue[Quote | None] = asyncio.Queue(maxsize=20_000)
        self._feeds: list = []

    def _ev(self, obj: dict) -> None:
        if self._events is not None:
            self._events.write(obj)

    async def emit(self, quote: Quote) -> None:
        try:
            self._queue.put_nowait(quote)
        except asyncio.QueueFull:
            log.warning("quote queue full, dropping %s %s", quote.venue.key, quote.pair_id)

    def _write_quote(self, quote: Quote) -> None:
        if self._quotes is None:
            return
        key = (int(quote.venue), quote.pair_id)
        min_ns = self.cfg.thresholds.quote_min_interval_ms * 1_000_000
        last = self._last_quote_write.get(key, 0)
        if quote.recv_ns - last < min_ns:
            return
        self._last_quote_write[key] = quote.recv_ns
        self._quotes.write(
            {
                "t": "q",
                "r": quote.recv_ns,
                "e": quote.exch_ts_ms,
                "v": int(quote.venue),
                "p": quote.pair_id,
                "b": quote.bid,
                "a": quote.ask,
                "B": quote.bid_sz,
                "A": quote.ask_sz,
            }
        )

    def _log_fill(self, fill: Fill) -> None:
        st = self.stats[fill.pair_id]
        if fill.action == "open":
            st.n_open += 1
            self._ev(
                {
                    "t": "open",
                    "pair": fill.pair_id,
                    "side": fill.side,
                    "px": fill.price,
                    "qty": fill.qty,
                    "imp": round(fill.impulse_bps, 4),
                    "res": round(fill.residual_bps, 4),
                    "recv_ns": fill.recv_ns,
                    "equity": fill.equity,
                }
            )
            return
        st.n_close += 1
        if fill.reason == "liq":
            st.n_liq += 1
        if fill.pnl_bps is not None:
            st.pnl_bps.append(fill.pnl_bps)
        if fill.pnl_usd is not None:
            st.pnl_usd.append(fill.pnl_usd)
        self._ev(
            {
                "t": "close",
                "pair": fill.pair_id,
                "side": fill.side,
                "px": fill.price,
                "pnl_bps": None if fill.pnl_bps is None else round(fill.pnl_bps, 4),
                "pnl_usd": None if fill.pnl_usd is None else round(fill.pnl_usd, 4),
                "hold_ms": None if fill.hold_ms is None else round(fill.hold_ms, 2),
                "reason": fill.reason,
                "recv_ns": fill.recv_ns,
                "equity": fill.equity,
            }
        )

    def handle(self, quote: Quote) -> None:
        self.n_quotes += 1
        self.books.update(quote)
        self._write_quote(quote)

        pair = self.pair_by_id.get(quote.pair_id)
        if pair is None:
            return
        st = self.stats[pair.id]
        mexc = self.books.get(Venue.MEXC, pair.id)
        leader = self.books.get(self.primary[pair.id], pair.id)
        if mexc and leader:
            st.last_residual = residual_bps(mexc, leader)

        if quote.venue == self.primary[pair.id]:
            impulse, follow_sig = self.signals[pair.id].on_leader(quote, mexc)
            if impulse is not None:
                self.last_impulse[pair.id] = impulse
                st.last_impulse = impulse
            if follow_sig is not None:
                st.n_impulse += 1
                self.follow.on_impulse(follow_sig)
                self._ev(
                    {
                        "t": "impulse",
                        "pair": pair.id,
                        "dir": follow_sig.direction,
                        "imp": round(follow_sig.impulse_bps, 4),
                        "res": round(follow_sig.residual_bps, 4),
                        "recv_ns": follow_sig.recv_ns,
                        "mode": pair.mode,
                    }
                )
                trade = self.signals[pair.id].trade_signal(quote, mexc, impulse)
                if trade is not None:
                    self.paper.on_signal(trade)

        if quote.venue == Venue.MEXC:
            result = self.follow.on_mexc(quote)
            if result is not None:
                st.n_follow_done += 1
                if result.followed:
                    st.n_followed += 1
                    st.lags_ms.append(result.lag_ms)
                self._ev(
                    {
                        "t": "follow",
                        "pair": result.pair_id,
                        "followed": result.followed,
                        "lag_ms": round(result.lag_ms, 2),
                        "imp": round(result.impulse_bps, 4),
                        "move": round(result.mexc_move_bps, 4),
                    }
                )

        if mexc and leader:
            for fill in self.paper.on_quotes(
                pair.id, leader, mexc, self.last_impulse.get(pair.id)
            ):
                self._log_fill(fill)

    def _build_feeds(self) -> list:
        binance_map: dict[str, str] = {}
        bitget_map: dict[str, str] = {}
        hl_map: dict[str, str] = {}
        binance_mult: dict[str, float] = {}
        bitget_mult: dict[str, float] = {}
        hl_mult: dict[str, float] = {}
        mexc_symbols: list[str] = []
        for pair in self.cfg.pairs:
            mexc_symbols.append(pair.mexc)
            for leader in pair.leaders:
                if leader.venue == Venue.BINANCE:
                    binance_map[leader.symbol] = pair.id
                    binance_mult[leader.symbol] = leader.px_mult
                elif leader.venue == Venue.BITGET:
                    bitget_map[leader.symbol] = pair.id
                    bitget_mult[leader.symbol] = leader.px_mult
                elif leader.venue == Venue.HYPERLIQUID:
                    hl_map[leader.symbol] = pair.id
                    hl_mult[leader.symbol] = leader.px_mult
        feeds = []
        if binance_map:
            feeds.append(BinanceFeed(self.emit, binance_map, binance_mult))
        if bitget_map:
            feeds.append(BitgetFeed(self.emit, bitget_map, bitget_mult))
        if hl_map:
            feeds.append(HyperliquidFeed(self.emit, hl_map, hl_mult))
        feeds.append(MexcFeed(self.emit, mexc_symbols))
        return feeds

    async def _consumer(self) -> None:
        while True:
            quote = await self._queue.get()
            if quote is None:
                return
            try:
                self.handle(quote)
            except Exception:
                log.exception("handle failed")

    async def _status_loop(self) -> None:
        every = self.cfg.status_every_sec
        last_n = 0
        last_t = time.time()
        while True:
            await asyncio.sleep(every)
            now = time.time()
            dt = max(now - last_t, 1e-6)
            rate = (self.n_quotes - last_n) / dt
            last_n = self.n_quotes
            last_t = now
            eq = self.paper.equity
            dead = " DEAD" if self.paper.dead else ""
            lev = self.cfg.thresholds.leverage
            parts = [
                f"quotes={self.n_quotes} ({rate:.0f}/s) q={self._queue.qsize()} "
                f"eq=${eq:.2f} {lev:.0f}x liq={self.paper.n_liq} stale={self.paper.n_stale} "
                f"fade={self.paper.n_skip_fade}{dead}"
            ]
            for pair in self.cfg.pairs:
                st = self.stats[pair.id]
                ft = (
                    f"{st.n_followed}/{st.n_follow_done}"
                    if st.n_follow_done
                    else "0/0"
                )
                pnl = sum(st.pnl_bps)
                res = "n/a" if st.last_residual is None else f"{st.last_residual:+.1f}"
                imp = "n/a" if st.last_impulse is None else f"{st.last_impulse:+.1f}"
                parts.append(
                    f"{pair.id.replace('_USDT','')} res={res} imp={imp} ft={ft} paper={pnl:+.1f}bps n={st.n_close}"
                )
            log.info(" | ".join(parts[:6]) + (" ..." if len(parts) > 6 else ""))
            for extra in parts[6:]:
                log.info("  %s", extra)

    async def run(self, seconds: float) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "seconds": seconds,
            "pairs": [p.id for p in self.cfg.pairs],
            "thresholds": asdict(self.cfg.thresholds),
            "skip_quotes": self.skip_quotes,
            "leverage": self.cfg.thresholds.leverage,
            "start_equity_usd": self.cfg.thresholds.start_equity_usd,
        }
        (self.out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self._feeds = self._build_feeds()
        consumer = asyncio.create_task(self._consumer(), name="consumer")
        status = asyncio.create_task(self._status_loop(), name="status")
        feed_tasks = [
            asyncio.create_task(feed.run(), name=feed.name) for feed in self._feeds
        ]
        log.info(
            "recording %.0fs into %s  leverage=%.0fx equity=$%.0f delay=%dms+/-%d",
            seconds,
            self.out_dir,
            self.cfg.thresholds.leverage,
            self.cfg.thresholds.start_equity_usd,
            self.cfg.thresholds.fill_delay_ms,
            self.cfg.thresholds.fill_delay_jitter_ms,
        )
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise
        finally:
            for feed in self._feeds:
                feed.stop()
            for task in feed_tasks:
                task.cancel()
            await asyncio.gather(*feed_tasks, return_exceptions=True)
            status.cancel()
            await self._queue.put(None)
            await consumer
            if self._quotes:
                self._quotes.close()
            if self._events:
                self._events.close()
            from hftv2.report import summarize_runtime

            summary = summarize_runtime(self)
            (self.out_dir / "summary.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
            log.info("wrote %s equity=$%.2f", self.out_dir / "summary.json", self.paper.equity)
