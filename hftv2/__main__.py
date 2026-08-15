from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from hftv2.config import load_config
from hftv2.live.mexc_web import MexcWebClient, MexcWebError, load_dotenv, token_preview
from hftv2.replay import format_sweep, replay_dir
from hftv2.report import format_table, report_dir
from hftv2.runtime import Runtime


def _setup_log() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_record(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if cfg.live.enabled:
        logging.getLogger("hftv2").warning(
            "live.enabled is true, but record is paper-only and will not send MEXC orders"
        )
    seconds = args.seconds if args.seconds is not None else cfg.hours * 3600.0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out or Path(cfg.data_dir) / f"run-{stamp}")
    rt = Runtime(cfg, out_dir, skip_quotes=args.skip_quotes)
    try:
        asyncio.run(rt.run(seconds))
    except KeyboardInterrupt:
        logging.getLogger("hftv2").info("stopped by user")
    print(format_table(report_dir(out_dir)))
    print(f"\nrun dir: {out_dir}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    summary = report_dir(Path(args.run))
    print(format_table(summary))
    print(f"\nwrote {Path(args.run) / 'report.json'}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    out = replay_dir(Path(args.run), args.config)
    print(format_sweep(out["scenarios"], out["n_quotes"]))
    print(f"\nwrote {Path(args.run) / 'replay.json'}")
    return 0


def _client(args: argparse.Namespace) -> MexcWebClient:
    load_dotenv()
    cfg = load_config(args.config)
    return MexcWebClient(cfg.live)


def cmd_live_status(args: argparse.Namespace) -> int:
    try:
        client = _client(args)
        asset = client.account_asset("USDT")
        pos = client.open_positions()
    except MexcWebError as exc:
        print(f"live-status failed: {exc}")
        return 1
    data = asset.get("data") or {}
    print(f"token {token_preview(client.token)}")
    print(f"live.enabled={client.live.enabled}  dry_run={client.live.dry_run}")
    if isinstance(data, dict):
        print("USDT", {k: data.get(k) for k in (
            "currency",
            "availableBalance",
            "frozenBalance",
            "positionMargin",
            "equity",
            "cashBalance",
        ) if k in data} or data)
    else:
        print("USDT", data)
    rows = pos.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("result") or rows.get("data") or [rows]
    if not isinstance(rows, list) or not rows:
        print("open positions: none")
        return 0
    print("open positions:")
    for row in rows:
        if not isinstance(row, dict):
            print(" ", row)
            continue
        print(
            f"  {row.get('symbol')} hold={row.get('holdVol')} "
            f"side={row.get('positionType') or row.get('state')} "
            f"pnl={row.get('unrealisedPnl') or row.get('pnl')} "
            f"id={row.get('positionId')}"
        )
    return 0


def cmd_live_order(args: argparse.Namespace) -> int:
    try:
        client = _client(args)
        warm_ms = client.warmup([args.symbol])
        price = args.price
        if args.market and price is None:
            tick = client.ticker(args.symbol)
            td = tick.get("data") or {}
            last = td.get("lastPrice") or td.get("fairPrice")
            if last is None:
                raise MexcWebError("ticker has no lastPrice; pass --price")
            price = float(last)
        if price is None:
            raise MexcWebError("--price is required for limit orders")
        out = client.submit_order(
            symbol=args.symbol,
            side=args.side,
            vol=args.vol,
            price=float(price),
            leverage=args.leverage,
            market=args.market,
            position_id=args.position_id,
            confirm=args.confirm,
            attach_tpsl=False if args.no_tpsl else None,
            tp_bps=args.tp_bps,
            sl_bps=args.sl_bps,
            fetch_contract=True,
        )
    except MexcWebError as exc:
        print(f"live-order failed: {exc}")
        return 1
    import json as _json

    print(_json.dumps(out, indent=2, default=str))
    print(f"warmup {warm_ms:.0f}ms (TLS + contract spec, paid once per session)")
    if out.get("dry_run"):
        print("not sent:", out.get("reason"))
        print("to send: live.enabled true, dry_run false, and --confirm")
    elif client.last_rtt_ms is not None:
        print(f"order round-trip {client.last_rtt_ms:.0f}ms")
    return 0


def cmd_live_close(args: argparse.Namespace) -> int:
    try:
        client = _client(args)
        client.warmup([args.symbol])
        out = client.close_position(
            args.symbol,
            vol=args.vol,
            price=args.price,
            confirm=args.confirm,
        )
    except MexcWebError as exc:
        print(f"live-close failed: {exc}")
        return 1
    import json as _json

    print(_json.dumps(out, indent=2, default=str))
    if out.get("dry_run"):
        print("not sent:", out.get("reason"))
        print("to send: live.enabled true, dry_run false, and --confirm")
    elif client.last_rtt_ms is not None:
        print(f"close round-trip {client.last_rtt_ms:.0f}ms")
    return 0


def cmd_live_ping(args: argparse.Namespace) -> int:
    load_dotenv()
    cfg = load_config(args.config)
    try:
        client = MexcWebClient(cfg.live)
    except MexcWebError:
        client = MexcWebClient(cfg.live, token="WEB_offline")  # public endpoints only
    symbol = args.symbol
    try:
        client.ticker(symbol)
        cold = client.last_rtt_ms or 0.0
        warm: list[float] = []
        for _ in range(4):
            client.ticker(symbol)
            warm.append(client.last_rtt_ms or 0.0)
        spec_ms = client.warmup([symbol])
    except MexcWebError as exc:
        print(f"live-ping failed: {exc}")
        return 1
    warm_avg = sum(warm) / len(warm)
    print(f"cold request (TLS handshake): {cold:.0f}ms")
    print("warm requests (keep-alive):  " + "  ".join(f"{w:.0f}ms" for w in warm))
    print(f"contract spec fetch:         {spec_ms:.0f}ms (cached afterwards)")
    print(
        f"\na live order costs ~1 warm round-trip (~{warm_avg:.0f}ms) "
        f"when the client is warmed up; cold it would cost ~{cold:.0f}ms"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_log()
    p = argparse.ArgumentParser(prog="hftv2", description="MEXC lead-lag recorder + paper fill")
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="record public books and run paper fills")
    rec.add_argument("--config", default="config.yaml")
    rec.add_argument("--hours", dest="hours", type=float, default=None)
    rec.add_argument("--seconds", type=float, default=None, help="override duration (for smoke tests)")
    rec.add_argument("--out", default=None)
    rec.add_argument("--skip-quotes", action="store_true", help="write events only, not raw quotes")
    rec.set_defaults(func=cmd_record)

    rep = sub.add_parser("report", help="rebuild stats from a run directory")
    rep.add_argument("run", help="path to data/run-...")
    rep.set_defaults(func=cmd_report)

    ply = sub.add_parser("replay", help="offline threshold sweep on recorded quotes")
    ply.add_argument("run", help="path to data/run-...")
    ply.add_argument("--config", default="config.yaml")
    ply.set_defaults(func=cmd_replay)

    st = sub.add_parser("live-status", help="MEXC USDT balance and open positions (read-only)")
    st.add_argument("--config", default="config.yaml")
    st.set_defaults(func=cmd_live_status)

    pg = sub.add_parser("live-ping", help="measure cold vs keep-alive round-trip to MEXC")
    pg.add_argument("--config", default="config.yaml")
    pg.add_argument("--symbol", default="SNDKSTOCK_USDT")
    pg.set_defaults(func=cmd_live_ping)

    lo = sub.add_parser("live-order", help="place one MEXC order; dry-run unless live.enabled")
    lo.add_argument("--config", default="config.yaml")
    lo.add_argument("--symbol", required=True, help="MEXC contract, e.g. SNDKSTOCK_USDT")
    lo.add_argument("--side", required=True, choices=["long", "short", "close_long", "close_short"])
    lo.add_argument("--vol", required=True, type=float, help="contract volume")
    lo.add_argument("--price", type=float, default=None)
    lo.add_argument("--leverage", type=float, default=None)
    lo.add_argument("--market", action="store_true", default=True)
    lo.add_argument("--limit", action="store_true", help="limit order instead of market")
    lo.add_argument("--position-id", dest="position_id", type=int, default=None)
    lo.add_argument("--tp-bps", dest="tp_bps", type=float, default=None)
    lo.add_argument("--sl-bps", dest="sl_bps", type=float, default=None)
    lo.add_argument("--no-tpsl", action="store_true", help="do not attach take-profit / stop-loss")
    lo.add_argument("--confirm", action="store_true", help="required to actually send when live")
    lo.set_defaults(func=cmd_live_order)

    lc = sub.add_parser("live-close", help="market-close the open position on a symbol")
    lc.add_argument("--config", default="config.yaml")
    lc.add_argument("--symbol", required=True, help="MEXC contract, e.g. SNDKSTOCK_USDT")
    lc.add_argument("--vol", type=float, default=None, help="partial close volume; default = full")
    lc.add_argument("--price", type=float, default=None)
    lc.add_argument("--confirm", action="store_true", help="required to actually send when live")
    lc.set_defaults(func=cmd_live_close)

    args = p.parse_args(argv)
    if args.cmd == "record" and args.hours is not None and args.seconds is None:
        args.seconds = args.hours * 3600.0
    if args.cmd == "live-order" and getattr(args, "limit", False):
        args.market = False
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
