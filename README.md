# hftv2

Lead-lag research stack for **MEXC USDT perpetuals**.

Public books from faster venues (Binance, Bitget, optionally Hyperliquid) are treated as the **leader**. MEXC is treated as the **laggard**. When the leader prints an impulse and MEXC has not caught up yet, the engine logs the event, measures follow-through lag, and simulates a paper fill on the MEXC book.

Recording is paper-only. Live MEXC orders are a separate, triple-locked manual client and are **not** wired into `record`.

[![CI](https://github.com/sssaturX/hft-mexc/actions/workflows/ci.yml/badge.svg)](https://github.com/sssaturX/hft-mexc/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Not financial advice. Paper P&L is a model, not an exchange fill. Live trading can lose the entire account. See [Disclaimer](#disclaimer).

## What it does

| Capability | Command | Sends MEXC orders? |
|---|---|---|
| Stream public books, detect impulses, paper-trade the lag | `record` | Never |
| Rebuild a stats table from a finished run | `report` | Never |
| Replay recorded quotes across threshold sets | `replay` | Never |
| Read USDT balance and open positions | `live-status` | No (read-only) |
| Measure cold vs keep-alive RTT to MEXC | `live-ping` | No |
| Preview or place one order (dry-run by default) | `live-order` | Only with the triple lock |
| Market-close a position (like the web Close button) | `live-close` | Only with the triple lock |

### Signal

For each configured pair:

1. Keep a trailing mid window on the **primary leader** (default lookback 150 ms).
2. **Impulse** = leader mid move over that window, in basis points.
3. **Residual** = `(MEXC mid − leader mid) / leader mid`, in basis points.
4. Fire a follow-through stopwatch on every impulse above the pair threshold.
5. Open a paper trade only if MEXC is still on the other side of the move (the edge is still there) and the book is not too wide.

Long example: leader jumped **up** (`impulse ≥ impulse_bps`) while MEXC is still **cheap** (`residual ≤ −edge_bps`) and the residual is at least `residual_frac` of the impulse.

### Paper portfolio

- One account, one position at a time.
- Entry and exit are delayed (`fill_delay_ms` ± jitter) to approximate order latency.
- Fill is skipped if the residual has already faded (`fill_keep_frac`).
- Size = equity × leverage, capped by displayed MEXC size × `contract_size`.
- Exit on convergence, impulse reversal, timeout, or a 1% adverse move (liquidation stop at 50×).
- `record` writes quotes, events, and a summary. It never calls the live client.

### Follow-through stats

After an impulse, MEXC is watched until it moves `follow_frac` of the impulse (counted as followed) or `follow_timeout_ms` elapses. Per pair the report shows follow rate and lag percentiles (p50 / p90).

## Architecture

```
Binance / Bitget / Hyperliquid WS          MEXC contract WS
              │                                    │
              └────────────┬───────────────────────┘
                           ▼
                     Runtime queue
                           │
           ┌───────────────┼────────────────┐
           ▼               ▼                ▼
     SignalEngine    FollowThrough    PaperPortfolio
           │               │                │
           └───────────────┴────────────────┘
                           ▼
              data/run-YYYYMMDD-HHMMSS/
                quotes.jsonl  events.jsonl
                meta.json     summary.json
```

| Path | Role |
|---|---|
| `hftv2/feeds/` | Websocket clients (Binance bookTicker, Bitget ticker, Hyperliquid L2, MEXC depth+ticker) |
| `hftv2/engine/signal.py` | Impulse + residual gates |
| `hftv2/engine/follow.py` | Lag / follow-through timer |
| `hftv2/engine/paper.py` | Delayed paper fills, liq, fade skip |
| `hftv2/runtime.py` | Wiring, JSONL writers, status line |
| `hftv2/report.py` | Tables from a run directory |
| `hftv2/replay.py` | Offline threshold sweep |
| `hftv2/live/mexc_web.py` | Optional unofficial web-token REST client |

## Install

Python 3.11 or newer.

```bash
git clone https://github.com/sssaturX/hft-mexc.git
cd hft-mexc
python -m pip install -e ".[dev]"
python -m pytest tests -q
```

Dependencies are `websockets` and `PyYAML` only. There is no native code.

## Quick start

Paper session (no orders):

```bash
python run.py record --hours 1.5
```

Equivalent: `python -m hftv2 record --hours 1.5`. Stop early with Ctrl+C. Output lands in `data/run-YYYYMMDD-HHMMSS/`.

Tokenized US equity perps on MEXC are most active around the NYSE cash session. Crypto pairs run 24h.

Smoke-test that feeds connect (skips raw quote logging, so the run cannot be replayed):

```bash
python run.py record --seconds 10 --skip-quotes
```

### Report and replay

```bash
python run.py report data/run-YYYYMMDD-HHMMSS
python run.py replay data/run-YYYYMMDD-HHMMSS
```

`replay` needs `quotes.jsonl`. It re-runs the paper engine offline across a built-in grid of impulse/edge thresholds and writes `replay.json`. Quotes are downsampled at `quote_min_interval_ms` (10 ms by default), so replay is for relative comparison, not tick-perfect identity with the live recorder.

## Configuration

Edit `config.yaml`. Pair list, thresholds, and paper account size live there.

| Key | Default in this repo | Meaning |
|---|---|---|
| `thresholds.start_equity_usd` | 20 | Paper starting cash |
| `thresholds.leverage` | 50 | Paper notional / equity |
| `thresholds.fill_delay_ms` | 100 | Simulated fill latency |
| `thresholds.fill_delay_jitter_ms` | 50 | ± jitter on that delay |
| `thresholds.liq_price_bps` | 100 | Close if adverse 1% |
| `thresholds.fill_keep_frac` | 0.5 | Skip fill if residual faded |
| `thresholds.lookback_ms` | 150 | Impulse window |
| `live.enabled` / `live.dry_run` | `false` / `true` | Live client stays off |

Each pair has:

- `kind`: `stock` or `crypto` (picks default impulse/edge if not overridden)
- `mode`: `frequent` (looser) or `strict`
- `mexc`: MEXC contract symbol
- `contract_size`: MEXC contract multiplier (book sizes are in contracts)
- `leaders`: venues + native symbols; set `primary: true` on one. `px_mult` rescales a leader (e.g. Binance `1000PEPEUSDT` → PEPE)

Shipped pairs: SK Hynix, Sandisk (SNDK), SpaceX tokenized, SOXL, AMD, SUI, PEPE.

## Run output

| File | Contents |
|---|---|
| `quotes.jsonl` | Compact L1 ticks (`t,r,e,v,p,b,a,B,A`) |
| `events.jsonl` | `impulse`, `follow`, `open`, `close` |
| `meta.json` | Start time, thresholds, pair ids |
| `summary.json` | End-of-run aggregates |
| `report.json` | Written by `report` |
| `replay.json` | Written by `replay` |

Status line while recording:

```
quotes=… (n/s) eq=$… 50x liq=… fade=… | SNDKSTOCK res=… imp=… ft=a/b paper=…bps
```

`ft` is follow-through (`followed / completed`). `fade` is fills skipped because the residual was gone by the time the delay elapsed.

## Live client (manual, off by default)

This talks to `futures.mexc.com` with a **browser web token**, the same unofficial scheme as [mexc-futures-sdk](https://github.com/oboshto/mexc-futures-sdk). It is not the official API-key REST API. Using a session token may violate the exchange terms of service; you are on your own.

Keep the token out of YAML. In `.env`:

```
MEXC_WEB_TOKEN=WEB_paste_here
```

How to copy it: log into [MEXC futures](https://www.mexc.com) → DevTools → Network → any `futures.mexc.com` request → `authorization` header (starts with `WEB`).

```bash
python run.py live-status
python run.py live-ping
python run.py live-order --symbol SNDKSTOCK_USDT --side long --vol 1 --market --confirm
python run.py live-close --symbol SNDKSTOCK_USDT --confirm
```

With the shipped config those order commands **print the payload and do not send** (`dry_run: true`, `enabled: false`).

To actually send, all three must be set:

```yaml
live:
  enabled: true
  dry_run: false
```

…and the command must include `--confirm`.

The client keeps one TLS connection. The first call pays the handshake; later orders are a single warm round-trip. Contract tick/volume specs are fetched once per symbol. Open orders can attach take-profit / stop-loss (`live.tp_bps` / `live.sl_bps`, default 3 bps / 100 bps). Closes do not attach TP/SL.

`vol` is **MEXC contracts**, not dollars. SNDK uses `contractSize=0.001`, so 1 contract ≈ price × 0.001 USDT.

## CLI

```bash
python -m pytest tests -q
python run.py record --hours 1.5
python run.py record --seconds 10 --skip-quotes
python run.py report data/run-YYYYMMDD-HHMMSS
python run.py replay data/run-YYYYMMDD-HHMMSS
python run.py live-status
python run.py live-ping --symbol SNDKSTOCK_USDT
python run.py live-order --symbol SNDKSTOCK_USDT --side long --vol 1 --market --confirm
python run.py live-close --symbol SNDKSTOCK_USDT --confirm
```

`live-order --side` is one of `long`, `short`, `close_long`, `close_short`. `--limit` sends a limit instead of a market. `--vol N` on `live-close` is a partial close.

## Tests

```bash
python -m pytest tests -q
```

Coverage includes residual/impulse math, delayed paper fills, fade skip, liquidation, contract-size quantity caps, MEXC request signing, TP/SL ticks, and the dry-run / confirm guards.

## Disclaimer

This is research software. Simulated fills use the displayed MEXC top of book plus a delay; they ignore queue position, partial fills, fees (even on “zero taker” contracts), funding, disconnects, and exchange throttles. Past follow-through is not a guarantee of future lag. Live use can liquidate you. Do not run this with money you cannot lose.

## License

[MIT](LICENSE)
