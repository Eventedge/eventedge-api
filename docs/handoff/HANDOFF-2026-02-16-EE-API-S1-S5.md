# HANDOFF — EventEdge API (S1–S5) — 2026-02-16

## TL;DR
We created a separate FastAPI "read layer" service and deployed it on the server with systemd + Caddy. It serves real snapshot-driven market data from Postgres (`edge_dataset_registry`) and powers the website dashboard.

## Live API
- Base: https://api.edgeblocks.io
- Health: https://api.edgeblocks.io/api/v1/health
- Market overview: https://api.edgeblocks.io/api/v1/market/overview
- Asset card:
  - https://api.edgeblocks.io/api/v1/assets/BTC/card
  - https://api.edgeblocks.io/api/v1/assets/ETH/card
- Fear & Greed: https://api.edgeblocks.io/api/v1/sentiment/fear-greed (still placeholder)

## Repo + deploy
- Repo: https://github.com/Eventedge/eventedge-api
- Install dir: `/opt/eventedge-api`
- Service: `eventedge-api.service` (uvicorn on 127.0.0.1:8080)
- Caddy: `api.edgeblocks.io -> reverse_proxy 127.0.0.1:8080`
- DNS: `api.edgeblocks.io A 88.223.95.211`
- TLS: Let's Encrypt via Caddy (auto)

## Data source (critical)
- DB: Postgres on `localhost:5432`
- Database: `eventedge`
- User: `eventedge_bot`
- Snapshot table: `edge_dataset_registry (dataset_key, payload jsonb, updated_at)`
- ~36 active snapshots; EdgeCore shadow jobs refresh every ~3-6 minutes

## Snapshot mapping (wired)
### /api/v1/market/overview
Uses:
- `coingecko:price_simple:usd:bitcoin` -> BTC price + 24h change
- `coinglass:oi_weighted_funding:BTC` -> OI-weighted funding rate
- `coinglass:open_interest:BTC` -> total OI + 24h change
- `coinglass:liquidations:BTC` -> 24h liquidations + long/short split
- `coingecko:global` -> BTC dominance, total mcap, 24h volume

### /api/v1/assets/{symbol}/card
- BTC and ETH supported (same dataset family per-asset)
- Composites: price, change, dominance, volume, funding, OI, liquidations

### /api/v1/sentiment/fear-greed
- No provider snapshot yet; remains placeholder

## Code structure
- `app/db.py`: psycopg2 connection helper (reads PGPASSWORD from env; supports PG* vars)
- `app/snapshots.py`: snapshot reader + extractors aligned to real payload shapes
- `app/main.py`: formats real KPI values (usd + pct) and includes source timestamps; never 500s if missing snapshots
- `docs/SNAPSHOT_MAPPING.md`: full discovery output with all available snapshot keys

## Deploy workflow
```bash
# 1. Edit code in ~/projects/eventedge-api
# 2. Test locally
# 3. git commit + push
# 4. Sync to /opt:
sudo rsync -a --delete --exclude .venv --exclude .git ~/projects/eventedge-api/ /opt/eventedge-api/
sudo chown -R eventedge:eventedge /opt/eventedge-api
/opt/eventedge-api/.venv/bin/pip install -e /opt/eventedge-api
# 5. Restart:
sudo systemctl restart eventedge-api.service
```

## Next ticket suggestions
- EE-API-006: Add endpoints for EdgeBlocks-unique widgets (placeholders first):
  - /api/v1/edge/supercard?symbol=BTC
  - /api/v1/edge/regime
  - /api/v1/paper/summary
- EE-API-007: Wire fear&greed to a real provider snapshot in `edge_dataset_registry`
- EE-API-008: Hardening (rate limits, auth toggle, allowlist tightening, caching keys)
