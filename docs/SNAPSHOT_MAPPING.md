# Snapshot Mapping — EventEdge API -> DB tables

## Database
- Host: localhost:5432
- Database: eventedge
- User: eventedge_bot
- Table: `edge_dataset_registry`
- Schema: `(dataset_key TEXT PK, payload JSONB, updated_at TIMESTAMPTZ)`

## Snapshot key format
`{provider}:{dataset}:{scope}` — SSOT in `edgecore/snapshots/keys.py`

---

## Endpoint Mapping

### /api/v1/market/overview
Aggregates multiple snapshots into a KPI tile array.

| KPI | Snapshot Key | Payload Path | Notes |
|---|---|---|---|
| BTC Price | `coingecko:price_simple:usd:bitcoin` | `.data.price`, `.data.change_24h` | TTL 360s |
| Funding (OI-weighted) | `coinglass:oi_weighted_funding:BTC` | `.data.rate` | TTL 1800s, rate is decimal (0.001178 = 0.12%) |
| Open Interest | `coinglass:open_interest:BTC` | `.data.oi_usd`, `.data.oi_change_24h` | TTL 300s |
| Liquidations (24h) | `coinglass:liquidations:BTC` | `.raw[0]` (exchange="All") → `.liquidation_usd`, `.longLiquidation_usd`, `.shortLiquidation_usd` | TTL 300s |
| BTC Dominance | `coingecko:global` | `.data.btc_dominance` | TTL 360s |
| Total Market Cap | `coingecko:global` | `.data.total_market_cap_usd` | TTL 360s |
| 24h Volume | `coingecko:global` | `.data.total_volume_usd` | TTL 360s |

### /api/v1/assets/{symbol}/card
Full card for a single asset. BTC is primary; ETH supported.

| Field | Snapshot Key (BTC) | Payload Path |
|---|---|---|
| price | `coingecko:price_simple:usd:bitcoin` | `.data.price` |
| change_24h | `coingecko:price_simple:usd:bitcoin` | `.data.change_24h` |
| dominance | `coingecko:global` | `.data.btc_dominance` |
| vol_24h | `coingecko:global` | `.data.total_volume_usd` |
| funding | `coinglass:oi_weighted_funding:BTC` | `.data.rate` |
| open_interest | `coinglass:open_interest:BTC` | `.data.oi_usd` |
| liquidations_24h | `coinglass:liquidations:BTC` | `.raw[0].liquidation_usd` (All) |

For ETH: swap `bitcoin`→`ethereum`, `BTC`→`ETH`. Dominance from `.data.eth_dominance`.

### /api/v1/sentiment/fear-greed
**No snapshot exists yet.** No fear/greed provider is wired into EdgeCore shadow jobs.

Options:
1. Add a new shadow job pulling from Alternative.me Fear & Greed API
2. Keep placeholder until a provider is added
3. Derive a simple sentiment composite from existing data (funding + liquidation bias + premium)

Recommended: keep placeholder for now, add provider in a future ticket.

---

## Additional data available (not yet exposed)
- Coinbase Premium: `coinglass:coinbase_premium`
- ETF flows: `sosovalue:etf_flow:btc`, `sosovalue:etf_flow:eth`, `sosovalue:etf_flow:sol`
- DeFi TVL: `defillama:global_tvl`, `defillama:chains`
- Gas: `etherscan:gas_oracle:eth`
- Prediction markets: `kalshi:crypto_markets`, `polymarket:active_markets`
- Bubble/cycle indicators: `coinglass:bubble_index`, `coinglass:pi_cycle`, `coinglass:bull_market_peak`
