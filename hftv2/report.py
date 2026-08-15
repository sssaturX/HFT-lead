from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hftv2.runtime import PairStats, Runtime


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    w = idx - lo
    return ordered[lo] * (1.0 - w) + ordered[hi] * w


def _pair_row(pair_id: str, st: PairStats) -> dict[str, Any]:
    n_done = st.n_follow_done
    ft = (st.n_followed / n_done) if n_done else None
    pnls = st.pnl_bps
    wins = sum(1 for x in pnls if x > 0)
    return {
        "pair": pair_id,
        "impulses": st.n_impulse,
        "follow_done": n_done,
        "followed": st.n_followed,
        "follow_through": None if ft is None else round(ft, 4),
        "lag_p50_ms": None if not st.lags_ms else round(percentile(st.lags_ms, 50) or 0, 1),
        "lag_p90_ms": None if not st.lags_ms else round(percentile(st.lags_ms, 90) or 0, 1),
        "paper_opens": st.n_open,
        "paper_closes": st.n_close,
        "paper_sum_bps": round(sum(pnls), 3),
        "paper_avg_bps": None if not pnls else round(sum(pnls) / len(pnls), 3),
        "paper_win_rate": None if not pnls else round(wins / len(pnls), 4),
        "paper_sum_usd": round(sum(st.pnl_usd), 4),
        "n_liq": st.n_liq,
        "last_residual_bps": None if st.last_residual is None else round(st.last_residual, 3),
        "last_impulse_bps": None if st.last_impulse is None else round(st.last_impulse, 3),
    }


def summarize_runtime(rt: Runtime) -> dict[str, Any]:
    rows = [_pair_row(p.id, rt.stats[p.id]) for p in rt.cfg.pairs]
    t = rt.cfg.thresholds
    return {
        "quotes": rt.n_quotes,
        "pairs": rows,
        "follow_through_all": _all_ft(rows),
        "paper_sum_bps": round(sum(r["paper_sum_bps"] for r in rows), 3),
        "paper_trades": sum(r["paper_closes"] for r in rows),
        "impulses": sum(r["impulses"] for r in rows),
        "leverage": t.leverage,
        "start_equity_usd": t.start_equity_usd,
        "equity_end_usd": round(rt.paper.equity, 4),
        "n_liq": rt.paper.n_liq,
        "n_stale": rt.paper.n_stale,
        "n_skip_busy": rt.paper.n_skip_busy,
        "n_skip_fade": rt.paper.n_skip_fade,
        "dead": rt.paper.dead,
        "fill_delay_ms": t.fill_delay_ms,
    }


def _all_ft(rows: list[dict[str, Any]]) -> float | None:
    done = sum(r["follow_done"] for r in rows)
    followed = sum(r["followed"] for r in rows)
    if not done:
        return None
    return round(followed / done, 4)


def load_events(path: Path) -> tuple[dict[str, PairStats], dict[str, Any]]:
    stats: dict[str, PairStats] = {}
    extra: dict[str, Any] = {"equity_end_usd": None}

    def bucket(pair: str) -> PairStats:
        st = stats.get(pair)
        if st is None:
            st = PairStats()
            stats[pair] = st
        return st

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            t = ev.get("t")
            pair = ev.get("pair")
            if not pair:
                continue
            st = bucket(pair)
            if t == "impulse":
                st.n_impulse += 1
                st.last_impulse = ev.get("imp")
                st.last_residual = ev.get("res")
            elif t == "follow":
                st.n_follow_done += 1
                if ev.get("followed"):
                    st.n_followed += 1
                    st.lags_ms.append(float(ev.get("lag_ms") or 0))
            elif t == "open":
                st.n_open += 1
                if ev.get("equity") is not None:
                    extra["equity_end_usd"] = float(ev["equity"])
            elif t == "close":
                st.n_close += 1
                if ev.get("reason") == "liq":
                    st.n_liq += 1
                if ev.get("pnl_bps") is not None:
                    st.pnl_bps.append(float(ev["pnl_bps"]))
                if ev.get("pnl_usd") is not None:
                    st.pnl_usd.append(float(ev["pnl_usd"]))
                if ev.get("equity") is not None:
                    extra["equity_end_usd"] = float(ev["equity"])
    return stats, extra


def report_dir(run_dir: Path) -> dict[str, Any]:
    events = run_dir / "events.jsonl"
    if not events.exists():
        raise FileNotFoundError(events)
    stats, extra = load_events(events)
    rows = [_pair_row(pair, st) for pair, st in sorted(stats.items())]
    meta: dict[str, Any] = {}
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    th = meta.get("thresholds") or {}
    out = {
        "run": str(run_dir),
        "pairs": rows,
        "follow_through_all": _all_ft(rows),
        "paper_sum_bps": round(sum(r["paper_sum_bps"] for r in rows), 3),
        "paper_trades": sum(r["paper_closes"] for r in rows),
        "impulses": sum(r["impulses"] for r in rows),
        "paper_sum_usd": round(sum(r.get("paper_sum_usd") or 0 for r in rows), 4),
        "n_liq": sum(r.get("n_liq") or 0 for r in rows),
        "leverage": meta.get("leverage", th.get("leverage")),
        "start_equity_usd": meta.get("start_equity_usd", th.get("start_equity_usd")),
        "equity_end_usd": extra.get("equity_end_usd"),
        "fill_delay_ms": th.get("fill_delay_ms"),
    }
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        live = json.loads(summary_path.read_text(encoding="utf-8"))
        for key in (
            "equity_end_usd",
            "n_stale",
            "n_skip_busy",
            "n_skip_fade",
            "dead",
            "leverage",
            "start_equity_usd",
            "fill_delay_ms",
        ):
            if live.get(key) is not None:
                out[key] = live[key]
    (run_dir / "report.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def format_table(summary: dict[str, Any]) -> str:
    lines = [
        f"impulses={summary.get('impulses', 0)}  "
        f"follow-through={summary.get('follow_through_all')}  "
        f"paper trades={summary.get('paper_trades', 0)}  "
        f"paper sum={summary.get('paper_sum_bps', 0)} bps  "
        f"eq ${summary.get('start_equity_usd', '')} -> ${summary.get('equity_end_usd', '')}  "
        f"liq={summary.get('n_liq', 0)}  fade={summary.get('n_skip_fade', 0)}",
        "",
        f"{'pair':22} {'imp':>6} {'ft':>8} {'p50ms':>7} {'p90ms':>7} {'n':>5} {'sum_bps':>8} {'usd':>8} {'win':>6}",
    ]
    for row in summary.get("pairs") or []:
        ft = row.get("follow_through")
        ft_s = "" if ft is None else f"{ft:.0%}"
        win = row.get("paper_win_rate")
        win_s = "" if win is None else f"{win:.0%}"
        usd = row.get("paper_sum_usd") or 0
        lines.append(
            f"{row['pair']:22} {row['impulses']:6d} {ft_s:>8} "
            f"{row.get('lag_p50_ms') or 0:7.0f} {row.get('lag_p90_ms') or 0:7.0f} "
            f"{row['paper_closes']:5d} {row['paper_sum_bps']:8.2f} {usd:8.2f} {win_s:>6}"
        )
    return "\n".join(lines)
