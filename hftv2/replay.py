from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from hftv2.config import load_config
from hftv2.report import summarize_runtime
from hftv2.runtime import Runtime
from hftv2.types import AppConfig, Quote, Venue


def load_quotes(path: Path) -> list[Quote]:
    quotes: list[Quote] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            quotes.append(
                Quote(
                    recv_ns=int(row["r"]),
                    exch_ts_ms=int(row["e"]),
                    venue=Venue(int(row["v"])),
                    pair_id=str(row["p"]),
                    native_symbol=str(row["p"]),
                    bid=float(row["b"]),
                    ask=float(row["a"]),
                    bid_sz=float(row.get("B") or 0),
                    ask_sz=float(row.get("A") or 0),
                )
            )
    return quotes


def tweak_config(
    cfg: AppConfig,
    *,
    frequent: tuple[float, float] | None = None,
    stock: tuple[float, float] | None = None,
    crypto: tuple[float, float] | None = None,
    fill_keep_frac: float | None = None,
    fill_delay_jitter_ms: int | None = None,
) -> AppConfig:
    th = cfg.thresholds
    if fill_keep_frac is not None or fill_delay_jitter_ms is not None:
        th = replace(
            th,
            fill_keep_frac=th.fill_keep_frac if fill_keep_frac is None else fill_keep_frac,
            fill_delay_jitter_ms=(
                th.fill_delay_jitter_ms if fill_delay_jitter_ms is None else fill_delay_jitter_ms
            ),
        )
    pairs = []
    for pair in cfg.pairs:
        imp, edge = pair.impulse_bps, pair.edge_bps
        if pair.mode == "frequent" and frequent is not None:
            imp, edge = frequent
        elif pair.mode != "frequent" and pair.kind == "stock" and stock is not None:
            imp, edge = stock
        elif pair.mode != "frequent" and pair.kind == "crypto" and crypto is not None:
            imp, edge = crypto
        pairs.append(replace(pair, impulse_bps=imp, edge_bps=edge))
    return replace(cfg, thresholds=th, pairs=pairs)


def run_quotes(cfg: AppConfig, quotes: Iterable[Quote], seed: int = 1) -> dict[str, Any]:
    rt = Runtime(
        cfg,
        Path("."),
        skip_quotes=True,
        write_events=False,
        rng=random.Random(seed),
    )
    for quote in quotes:
        rt.handle(quote)
    return summarize_runtime(rt)


DEFAULT_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "baseline_no_fade",
        "label": "live 3.0/3.5  8/10  6/8  no fill-check",
        "frequent": (3.0, 3.5),
        "stock": (8.0, 10.0),
        "crypto": (6.0, 8.0),
        "fill_keep_frac": 0.0,
    },
    {
        "id": "baseline_fade",
        "label": "live 3.0/3.5  8/10  6/8  fill-check 0.5",
        "frequent": (3.0, 3.5),
        "stock": (8.0, 10.0),
        "crypto": (6.0, 8.0),
        "fill_keep_frac": 0.5,
    },
    {
        "id": "freq_3_3",
        "label": "frequent 3.0/3.0  8/10  6/8  fill-check 0.5",
        "frequent": (3.0, 3.0),
        "stock": (8.0, 10.0),
        "crypto": (6.0, 8.0),
        "fill_keep_frac": 0.5,
    },
    {
        "id": "candidate",
        "label": "frequent 3.0/3.0  8/8  6/6  fill-check 0.5",
        "frequent": (3.0, 3.0),
        "stock": (8.0, 8.0),
        "crypto": (6.0, 6.0),
        "fill_keep_frac": 0.5,
    },
    {
        "id": "freq_2_5",
        "label": "frequent 2.5/2.5  8/8  6/6  fill-check 0.5",
        "frequent": (2.5, 2.5),
        "stock": (8.0, 8.0),
        "crypto": (6.0, 6.0),
        "fill_keep_frac": 0.5,
    },
    {
        "id": "crypto_5_5",
        "label": "frequent 3.0/3.0  8/8  crypto 5/5  fill-check 0.5",
        "frequent": (3.0, 3.0),
        "stock": (8.0, 8.0),
        "crypto": (5.0, 5.0),
        "fill_keep_frac": 0.5,
    },
    {
        "id": "crypto_4_4",
        "label": "frequent 3.0/3.0  8/8  crypto 4/4  fill-check 0.5",
        "frequent": (3.0, 3.0),
        "stock": (8.0, 8.0),
        "crypto": (4.0, 4.0),
        "fill_keep_frac": 0.5,
    },
]


def sweep(
    quotes: list[Quote],
    cfg: AppConfig,
    scenarios: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in scenarios or DEFAULT_SCENARIOS:
        scenario_cfg = tweak_config(
            cfg,
            frequent=spec.get("frequent"),
            stock=spec.get("stock"),
            crypto=spec.get("crypto"),
            fill_keep_frac=spec.get("fill_keep_frac"),
            fill_delay_jitter_ms=0,
        )
        summary = run_quotes(scenario_cfg, quotes)
        pair_rows = {
            row["pair"]: {
                "trades": row["paper_closes"],
                "sum_bps": row["paper_sum_bps"],
                "sum_usd": row["paper_sum_usd"],
                "win": row["paper_win_rate"],
                "impulses": row["impulses"],
            }
            for row in summary["pairs"]
        }
        rows.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "equity_end_usd": summary["equity_end_usd"],
                "trades": summary["paper_trades"],
                "impulses": summary["impulses"],
                "sum_bps": summary["paper_sum_bps"],
                "n_liq": summary["n_liq"],
                "n_skip_fade": summary.get("n_skip_fade", 0),
                "n_skip_busy": summary.get("n_skip_busy", 0),
                "win_rate": _overall_win(summary),
                "pairs": pair_rows,
            }
        )
    return rows


def _overall_win(summary: dict[str, Any]) -> float | None:
    wins = 0
    n = 0
    for row in summary["pairs"]:
        wr = row.get("paper_win_rate")
        closes = row.get("paper_closes") or 0
        if wr is None or not closes:
            continue
        wins += wr * closes
        n += closes
    if not n:
        return None
    return round(wins / n, 4)


def format_sweep(rows: list[dict[str, Any]], n_quotes: int) -> str:
    lines = [
        f"replay quotes={n_quotes}  delay=100ms jitter=0  start=$20 50x",
        "",
        f"{'id':16} {'eq':>8} {'n':>5} {'bps':>8} {'win':>6} {'fade':>5} {'liq':>4}  note",
    ]
    for row in rows:
        win = row.get("win_rate")
        win_s = "" if win is None else f"{win:.0%}"
        lines.append(
            f"{row['id']:16} {row['equity_end_usd']:8.2f} {row['trades']:5d} "
            f"{row['sum_bps']:8.2f} {win_s:>6} {row['n_skip_fade']:5d} {row['n_liq']:4d}  "
            f"{row['label']}"
        )
    lines.append("")
    for row in rows:
        bits = []
        for pair, st in row["pairs"].items():
            if st["trades"] or st["impulses"]:
                short = pair.replace("STOCK_USDT", "").replace("_USDT", "")
                bits.append(f"{short} n={st['trades']} {st['sum_bps']:+.1f}bps")
        if bits:
            lines.append(f"  {row['id']}: " + " | ".join(bits))
    return "\n".join(lines)


def replay_dir(run_dir: Path, config_path: str | Path = "config.yaml") -> dict[str, Any]:
    quotes_path = run_dir / "quotes.jsonl"
    if not quotes_path.exists():
        raise FileNotFoundError(quotes_path)
    cfg = load_config(config_path)
    quotes = load_quotes(quotes_path)
    rows = sweep(quotes, cfg)
    out = {
        "run": str(run_dir),
        "n_quotes": len(quotes),
        "note": "10ms downsampled quotes; relative comparison, not tick-perfect vs live",
        "scenarios": rows,
    }
    (run_dir / "replay.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out
