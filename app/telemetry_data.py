"""GET /api/v1/admin/telemetry/data — EdgeCore freshness + DB stats.

Each sub-block is independently fault-tolerant.  No migrations required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import get_conn


def _table_exists(cur: Any, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (name,),
    )
    return cur.fetchone() is not None


def _unavailable(reason: str = "table not found") -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)



# TTL lookup (seconds) — synced with edgecore/snapshots/keys.py _REGISTRY.
# key prefix → expected TTL.  Longest prefix match wins.
_TTL_MAP: dict[str, int] = {
    # CoinGecko
    "coingecko:global": 360,
    "coingecko:price_simple": 360,
    "coingecko:trending": 900,
    "coingecko:markets_top": 600,
    "coingecko:treasury_btc": 21600,
    # CoinGlass
    "coinglass:liquidations": 300,
    "coinglass:funding_rate": 300,
    "coinglass:open_interest": 300,
    "coinglass:long_short_ratio": 300,
    "coinglass:top_trader_sentiment": 600,
    "coinglass:oi_weighted_funding": 1800,
    "coinglass:coinbase_premium": 300,
    "coinglass:exchange_rank": 900,
    "coinglass:bubble_index": 3600,
    "coinglass:bull_market_peak": 3600,
    "coinglass:pi_cycle": 3600,
    # SoSoValue
    "sosovalue:etf_flow": 3600,
    # DefiLlama
    "defillama:chains": 900,
    "defillama:global_tvl": 300,
    "defillama:protocol": 900,
    "defillama:dex_volume": 900,
    "defillama:bridge_volume": 900,
    "defillama:bridge_flows": 1800,
    "defillama:lending_tvl": 1800,
    "defillama:l2_comparison": 900,
    "defillama:stablecoin_mcap": 1800,
    "defillama:stablecoins_chain": 1800,
    "defillama:chain_dex": 900,
    "defillama:chain_fees": 900,
    "defillama:chain_perps": 900,
    # Polymarket / Kalshi / PM3
    "polymarket:active_markets": 900,
    "kalshi:macro_markets": 900,
    "kalshi:crypto_markets": 900,
    "pm3:quotes_poly": 120,
    "pm3:quotes_kalshi": 120,
    # Etherscan
    "etherscan:gas_oracle": 120,
    "etherscan:balance": 600,
    "etherscan:contract_verified": 3600,
    # DEXTools
    "dextools:hot_pools": 900,
    "dextools:gainers": 900,
    "dextools:losers": 900,
    "dextools:new_pools": 1800,
    # News
    "news:feed": 900,
    # Wallet
    "wallet:evm_holdings": 300,
    "wallet:sol_holdings": 300,
    # Alternative.me
    "altme:fear_greed": 1800,
    # EdgeCore internal (regime/sentiment refresh ~3 min)
    "edge:regime": 300,
    "edge:sentiment": 300,
    # EdgeBank feature vectors (hourly cadence)
    "edgebank:ta_core": 3600,
    "edgebank:deriv_core": 3600,
    "edgebank:pm_core": 3600,
    "edgebank:macro_core": 3600,
    "edgebank:quality": 3600,
    # EdgeMind
    "edgemind:regime": 3600,
    "edgemind:router_top_features": 3600,
}

# Sorted by descending prefix length for longest-prefix match
_TTL_PREFIXES = sorted(_TTL_MAP.keys(), key=len, reverse=True)

# Active scopes — synced with edgecore/snapshots/keys.py _ACTIVE_SCOPES.
# Keys not listed here: all scopes active.
_ACTIVE_SCOPES: dict[str, tuple[str, ...]] = {
    "coinglass:funding_rate": ("BTC", "ETH"),
    "coinglass:open_interest": ("BTC", "ETH"),
    "coinglass:liquidations": ("BTC", "ETH"),
    "coinglass:oi_weighted_funding": ("BTC", "ETH"),
    "coinglass:long_short_ratio": ("BTC", "ETH"),
    "coinglass:top_trader_sentiment": ("BTC", "ETH"),
    "coingecko:price_simple": ("usd:bitcoin", "usd:ethereum"),
    "sosovalue:etf_flow": ("btc", "eth", "sol"),
}


def _ttl_for_key(key: str) -> int | None:
    """Return expected TTL in seconds, or None if unknown."""
    for prefix in _TTL_PREFIXES:
        if key.startswith(prefix):
            return _TTL_MAP[prefix]
    return None


def _is_scope_active(key: str) -> bool:
    """Return whether a scoped key is actively fetched.

    Matches the longest prefix in _ACTIVE_SCOPES and checks whether the
    scope suffix is in the active set.  Keys without a scope restriction
    return True.
    """
    for prefix, scopes in _ACTIVE_SCOPES.items():
        if key.startswith(prefix + ":"):
            scope = key[len(prefix) + 1:]
            return scope in scopes
    return True


def _classify(age_s: float, ttl_s: int | None, active: bool = True) -> str:
    """Classify freshness: fresh / stale / dead / disabled / unknown."""
    if not active:
        return "disabled"
    if ttl_s is None:
        return "unknown"
    if age_s <= ttl_s:
        return "fresh"
    if age_s <= ttl_s * 2:
        return "stale"
    return "dead"


def _edgecore_snapshots(cur: Any) -> dict[str, Any]:
    """Snapshot freshness from edge_dataset_registry."""
    # Prefer edge_dataset_registry (EdgeCore SSOT), fall back to api_snapshots
    table = None
    for t in ("edge_dataset_registry", "api_snapshots"):
        if _table_exists(cur, t):
            table = t
            break
    if table is None:
        return _unavailable()

    if table == "edge_dataset_registry":
        key_col = "dataset_key"
        ts_col = "updated_at"
    else:
        key_col = "data_type"
        ts_col = "created_at"

    cur.execute(
        f"SELECT {key_col}, {ts_col}, "
        f"  EXTRACT(EPOCH FROM NOW() - {ts_col}) AS age_s "
        f"FROM {table} ORDER BY {key_col}"
    )
    rows = cur.fetchall()

    # Skip internal cooldown keys
    rows = [(k, ts, age) for k, ts, age in rows if not k.startswith("_cooldown:")]

    total = len(rows)
    counts: dict[str, int] = {"fresh": 0, "stale": 0, "dead": 0, "disabled": 0, "unknown": 0}
    snapshots = []
    for key, updated_at, age_s in rows:
        age = float(age_s or 0)
        ttl = _ttl_for_key(key)
        active = _is_scope_active(key)
        status = _classify(age, ttl, active)
        counts[status] += 1
        snapshots.append({
            "key": key,
            "updated_at": _iso(updated_at),
            "age_s": round(age, 1),
            "ttl_s": ttl,
            "status": status,
        })

    return {
        "available": True,
        "total_keys": total,
        "fresh": counts["fresh"],
        "stale": counts["stale"],
        "dead": counts["dead"],
        "disabled": counts["disabled"],
        "unknown": counts["unknown"],
        "snapshots": snapshots,
    }


def _db_stats(cur: Any) -> dict[str, Any]:
    """Database size + largest tables from pg_stat."""
    try:
        cur.execute("SELECT pg_database_size(current_database())")
        db_bytes = cur.fetchone()[0]
        db_mb = round(db_bytes / (1024 * 1024), 1) if db_bytes else 0

        cur.execute(
            "SELECT schemaname || '.' || relname, "
            "  pg_total_relation_size(relid), "
            "  n_live_tup, "
            "  last_vacuum, "
            "  last_analyze "
            "FROM pg_stat_user_tables "
            "ORDER BY pg_total_relation_size(relid) DESC "
            "LIMIT 15"
        )
        tables = []
        for name, size_bytes, rows, vacuum, analyze in cur.fetchall():
            tables.append({
                "table": name,
                "size_mb": round((size_bytes or 0) / (1024 * 1024), 1),
                "rows": int(rows or 0),
                "last_vacuum": _iso(vacuum),
                "last_analyze": _iso(analyze),
            })

        cur.execute(
            "SELECT COUNT(*) FROM pg_stat_user_tables"
        )
        table_count = cur.fetchone()[0]

        return {
            "available": True,
            "database_mb": db_mb,
            "table_count": table_count,
            "largest_tables": tables,
        }
    except Exception:
        return _unavailable("pg_stat query failed")


def build_telemetry_data() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    data: dict[str, Any] = {}
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                ec = _edgecore_snapshots(cur)
                if ec.get("available"):
                    data["edgecore"] = {
                        "total_keys": ec["total_keys"],
                        "fresh": ec["fresh"],
                        "stale": ec["stale"],
                        "dead": ec["dead"],
                        "disabled": ec["disabled"],
                        "unknown": ec["unknown"],
                        "snapshots": ec["snapshots"],
                    }
                else:
                    data["edgecore"] = None
                    errors.append(f"edgecore: {ec.get('reason')}")
            except Exception as exc:
                conn.rollback()
                data["edgecore"] = None
                errors.append(f"edgecore: {type(exc).__name__}")

            try:
                db = _db_stats(cur)
                if db.get("available"):
                    data["database"] = {
                        "database_mb": db["database_mb"],
                        "table_count": db["table_count"],
                        "largest_tables": db["largest_tables"],
                    }
                else:
                    data["database"] = None
                    errors.append(f"database: {db.get('reason')}")
            except Exception as exc:
                conn.rollback()
                data["database"] = None
                errors.append(f"database: {type(exc).__name__}")

    result: dict[str, Any] = {"ok": True, "generated_at": now, "data": data}
    if errors:
        result["_errors"] = errors
    return result
