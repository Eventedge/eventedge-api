# HANDOFF — EventEdge API (S6–S7) — 2026-02-16

## Live API base
- https://api.edgeblocks.io

## Shipped
### EE-API-006 — New placeholder endpoints (stable schema + cache + never 500)
- GET /api/v1/edge/supercard?symbol=BTC
  - summary: stance/confidence/notes
  - 6 pillars: flow/leverage/fragility/momentum/sentiment/risk
- GET /api/v1/edge/regime
  - regime label + confidence + since
  - 4 axes: trend/volatility/leverage/liquidity
  - 3 drivers
- GET /api/v1/paper/summary
  - accounts active/tracked
  - KPIs: equity_30d, win_rate, max_drawdown, active_positions
  - sample equity_curve (empty placeholder)

### EE-API-007 — Real Fear & Greed
- GET /api/v1/sentiment/fear-greed now returns real data
- Provider: Alternative.me public Fear & Greed Index
- New module: app/fear_greed.py
  - Fetches from https://api.alternative.me/fng/?limit=30&format=json
  - Parses current value/label + 7-day history
  - Upserts raw payload into edge_dataset_registry as **altme:fear_greed**
  - Re-fetches if stale (>5 minutes), falls back to DB if provider fails
- Added dependency: httpx==0.27.2
- Fallback: returns old neutral placeholder if both provider and DB fail

## All live endpoints
| Endpoint | Status |
|---|---|
| GET /api/v1/health | Real |
| GET /api/v1/market/overview | Real (EdgeCore snapshots) |
| GET /api/v1/assets/{symbol}/card | Real (BTC, ETH) |
| GET /api/v1/sentiment/fear-greed | Real (Alternative.me + DB cache) |
| GET /api/v1/edge/supercard?symbol= | Placeholder schema |
| GET /api/v1/edge/regime | Placeholder schema |
| GET /api/v1/paper/summary | Placeholder schema |

## Website integration
- edgeblocks.io proxies all 7 endpoints and dashboard consumes them
- Fear & Greed widget is now fully real + sparkline on dashboard

## Deploy workflow (unchanged)
```bash
sudo rsync -a --delete --exclude .venv --exclude .git ~/projects/eventedge-api/ /opt/eventedge-api/
sudo chown -R eventedge:eventedge /opt/eventedge-api
/opt/eventedge-api/.venv/bin/pip install -e /opt/eventedge-api
sudo systemctl restart eventedge-api.service
```

## Next recommended tickets
- EE-API-008: Begin populating supercard pillars from bot sources / HiveMind rollups (keep schema stable)
- EE-API-009: Wire paper summary to bot paper trader tables + downsample equity curve
