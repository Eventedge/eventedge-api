# EventEdge API (read layer)

This service powers EdgeBlocks website widgets via stable endpoints:

- `GET /api/v1/health`
- `GET /api/v1/market/overview`
- `GET /api/v1/assets/{symbol}/card`
- `GET /api/v1/sentiment/fear-greed`

## Run (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Smoke checks

```bash
curl -sS http://127.0.0.1:8080/api/v1/health
curl -sS http://127.0.0.1:8080/api/v1/market/overview
curl -sS http://127.0.0.1:8080/api/v1/assets/BTC/card
curl -sS http://127.0.0.1:8080/api/v1/sentiment/fear-greed
```

## Next

Replace placeholders with EdgeCore snapshot reads + caching.
