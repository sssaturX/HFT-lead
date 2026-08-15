from __future__ import annotations

from pathlib import Path

import yaml

from hftv2.types import AppConfig, Leader, LiveConfig, PairConfig, Thresholds, Venue


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    run = raw.get("run") or {}
    t = raw.get("thresholds") or {}
    thresholds = Thresholds(
        lookback_ms=int(t.get("lookback_ms", 150)),
        impulse_bps=float(t.get("impulse_bps", 6.0)),
        edge_bps=float(t.get("edge_bps", 8.0)),
        residual_frac=float(t.get("residual_frac", 0.5)),
        follow_timeout_ms=int(t.get("follow_timeout_ms", 2000)),
        follow_frac=float(t.get("follow_frac", 0.5)),
        impulse_cooldown_ms=int(t.get("impulse_cooldown_ms", 400)),
        paper_timeout_ms=int(t.get("paper_timeout_ms", 2500)),
        paper_exit_bps=float(t.get("paper_exit_bps", 1.5)),
        max_spread_bps=float(t.get("max_spread_bps", 8.0)),
        max_notional_usd=float(t.get("max_notional_usd", 200)),
        crypto_impulse_bps=float(t.get("crypto_impulse_bps", t.get("impulse_bps", 6.0))),
        crypto_edge_bps=float(t.get("crypto_edge_bps", t.get("edge_bps", 8.0))),
        stock_impulse_bps=float(t.get("stock_impulse_bps", 8.0)),
        stock_edge_bps=float(t.get("stock_edge_bps", 10.0)),
        fill_delay_ms=int(t.get("fill_delay_ms", 100)),
        fill_delay_jitter_ms=int(t.get("fill_delay_jitter_ms", 50)),
        leverage=float(t.get("leverage", 50)),
        start_equity_usd=float(t.get("start_equity_usd", 20)),
        liq_price_bps=float(t.get("liq_price_bps", 100)),
        quote_min_interval_ms=int(
            run.get("quote_min_interval_ms", t.get("quote_min_interval_ms", 10))
        ),
        fill_keep_frac=float(t.get("fill_keep_frac", 0.5)),
    )

    pairs: list[PairConfig] = []
    for item in raw.get("pairs") or []:
        kind = item.get("kind", "crypto")
        impulse = (
            thresholds.stock_impulse_bps if kind == "stock" else thresholds.crypto_impulse_bps
        )
        edge = thresholds.stock_edge_bps if kind == "stock" else thresholds.crypto_edge_bps
        if item.get("impulse_bps") is not None:
            impulse = float(item["impulse_bps"])
        if item.get("edge_bps") is not None:
            edge = float(item["edge_bps"])
        cooldown = int(item.get("impulse_cooldown_ms", thresholds.impulse_cooldown_ms))
        leaders = [
            Leader(
                venue=Venue.from_key(str(row["venue"])),
                symbol=str(row["symbol"]),
                primary=bool(row.get("primary", False)),
                px_mult=float(row.get("px_mult", 1.0)),
            )
            for row in item.get("leaders") or []
        ]
        if not leaders:
            raise ValueError(f"pair {item.get('id')} has no leaders")
        if not any(leader.primary for leader in leaders):
            leaders[0].primary = True
        pairs.append(
            PairConfig(
                id=str(item["id"]),
                kind=kind,
                mexc=str(item.get("mexc") or item["id"]),
                zero_taker=bool(item.get("zero_taker", True)),
                leaders=leaders,
                impulse_bps=impulse,
                edge_bps=edge,
                impulse_cooldown_ms=cooldown,
                mode=str(item.get("mode", "strict")),
                contract_size=float(item.get("contract_size", 1.0)),
            )
        )
    if not pairs:
        raise ValueError("config has no pairs")

    lv = raw.get("live") or {}
    live = LiveConfig(
        enabled=bool(lv.get("enabled", False)),
        dry_run=bool(lv.get("dry_run", True)),
        leverage=float(lv.get("leverage", thresholds.leverage)),
        open_type=int(lv.get("open_type", 1)),
        timeout_sec=float(lv.get("timeout_sec", 10)),
        base_url=str(lv.get("base_url", "https://futures.mexc.com/api/v1")),
        tp_bps=float(lv.get("tp_bps", 3.0)),
        sl_bps=float(lv.get("sl_bps", thresholds.liq_price_bps)),
        attach_tpsl=bool(lv.get("attach_tpsl", True)),
    )

    return AppConfig(
        hours=float(run.get("hours", 2)),
        data_dir=str(run.get("data_dir", "data")),
        status_every_sec=float(run.get("status_every_sec", 5)),
        thresholds=thresholds,
        pairs=pairs,
        live=live,
    )
