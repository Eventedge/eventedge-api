# HANDOFF — EventEdge API (S8–S10) — 2026-02-16

## Live base
- https://api.edgeblocks.io

## Shipped
### EE-API-008 — SuperCard real (v0.2-live)
- New module: `app/supercard.py`
- Endpoint: GET /api/v1/edge/supercard?symbol=BTC (also ETH)
- Pillars populated from live snapshots + Fear & Greed:
  - Flow: liquidations + global volume
  - Leverage: OI + OI-weighted funding
  - Fragility: liquidation long/short split
  - Momentum: price + 24h change
  - Sentiment: Fear & Greed index
  - Risk: OI change + BTC dominance
- Summary: stance (cautious/neutral/risk-on/crowded-longs) + confidence (low/medium/high) + 3 notes
- Schema stable; try/except fallback to v0.1-placeholder.

### EE-API-008B — Formatting fix
- Unsigned % for liq split (62% not +62%) and BTC dominance (56.5% not +56.5%)
- Kept signed for deltas (OI change, funding, price 24h)

### EE-API-009 — Paper summary real (v0.2-live)
- Updated module: `app/paper.py`
- Endpoint: GET /api/v1/paper/summary
- Real rollups from bot paper tables:
  - accounts.active/tracked from paper_accounts_v3
  - active_positions from paper_positions (status='open')
  - win_rate from paper_trades (net_pnl_usdt, last 30d)
- Key decision: queried actual schema first rather than using cascading try/except (psycopg2 aborts transaction on query error)

### EE-API-009B — Paper equity curve + max drawdown (v0.3-live)
- Primary source: `pt_equity_snapshots` table (daily sum of equity_usd across accounts)
- Fallback: cumulative net_pnl_usdt from paper_trades (if no equity table)
- Downsampled to <= 60 points via `_downsample()`
- Max drawdown computed peak-to-trough
- equity_30d formatted via `fmt_usd` ($1.2M)
- Currently showing: 23 daily points, $1.2M equity, 89.6% max drawdown

### EE-API-010 — Regime real (v0.2-live)
- New module: `app/regime.py`
- Endpoint: GET /api/v1/edge/regime
- Heuristic classifier from live BTC snapshots:
  - Trend axis: 24h price change (Up/Down/Flat)
  - Volatility axis: liquidation total (Calm/Chop/Shock)
  - Leverage axis: funding (Light/Normal/Crowded)
  - Liquidity axis: liquidation skew (Loose/Normal/Tight)
- Label: Risk-Off / Risk-On / Trend / Chop
- 3 drivers with source context (no formula disclosure)
- Schema stable; try/except fallback.

## All live endpoints
| Endpoint | Version | Source |
|---|---|---|
| GET /api/v1/health | — | Static |
| GET /api/v1/market/overview | — | EdgeCore snapshots |
| GET /api/v1/assets/{symbol}/card | — | EdgeCore snapshots |
| GET /api/v1/sentiment/fear-greed | — | Alternative.me + DB cache |
| GET /api/v1/edge/supercard?symbol= | v0.2-live | Live snapshots + F&G |
| GET /api/v1/edge/regime | v0.2-live | Heuristic classifier |
| GET /api/v1/paper/summary | v0.3-live | Bot paper tables + equity snapshots |

## API modules
```
app/
  __init__.py
  db.py              # get_conn() — psycopg2 to eventedge DB
  snapshots.py       # get_snapshot() + extract_* + fmt_* helpers
  fear_greed.py      # Alternative.me fetch + DB cache
  supercard.py       # build_supercard() — 6 pillars from snapshots
  regime.py          # build_regime() — heuristic 4-axis classifier
  paper.py           # build_paper_summary() — accounts/trades/equity
  main.py            # FastAPI app + all endpoints
```

## Deploy workflow
```bash
sudo rsync -a --delete --exclude .venv --exclude .git ~/projects/eventedge-api/ /opt/eventedge-api/
sudo chown -R eventedge:eventedge /opt/eventedge-api
/opt/eventedge-api/.venv/bin/pip install -e /opt/eventedge-api
sudo systemctl restart eventedge-api.service
```
Note: sudo requires password (054310735 used this session for testing).

## Next recommended tickets
- EDGE-WEB-017: Render equity_curve sparkline in Paper Trader dashboard widget
- EE-API-011: Wire regime.since (track when regime label last changed, persist in DB)
- EE-API-012: Add /api/v1/edge/signals endpoint (recent bot signal events, safe subset)
