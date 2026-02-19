# Admin Telemetry Plan (SSOT)

> Ticket: ADMIN-TELEMETRY-PLAN-001
> Status: Draft
> Last updated: 2026-02-19

This document defines every admin dashboard page, its data sources,
required API endpoints, payload shapes, refresh cadence, and auth notes.

Typed response contracts live in `app/telemetry_contracts.py`.

---

## Endpoint Naming Convention

```
GET /api/v1/admin/telemetry/<page>          — page-level summary
GET /api/v1/admin/telemetry/<page>/<detail> — drill-down
```

All endpoints return JSON with `Cache-Control: no-store`.
All endpoints return HTTP 200 on success (even if data is partial).

---

## 1. Overview

**Purpose:** Single-glance system status for ops.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/health/services` | `service_heartbeats` | Live (existing) |
| `GET /api/v1/admin/telemetry/overview` | Aggregates from below pages | 60s TTL |

### Payload: `/admin/telemetry/overview`

```json
{
  "generated_at": "ISO-8601",
  "active_users_24h": 42,
  "commands_24h": 1200,
  "alerts_fired_24h": 85,
  "paper_trades_24h": 320,
  "edgecore_stale_keys": 2,
  "services_down": 0,
  "abuse_blocks_24h": 7
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `active_users_24h` | `telemetry_events` | `COUNT(DISTINCT user_id) WHERE ts > now() - '24h'` |
| `commands_24h` | `telemetry_events` | `COUNT(*) WHERE event_type = 'command_used' AND ts > now() - '24h'` |
| `alerts_fired_24h` | `alert_lifecycle` | `COUNT(*) WHERE event = 'fired' AND ts > now() - '24h'` |
| `paper_trades_24h` | `paper_trade_events` | `COUNT(*) WHERE ts > now() - '24h'` |
| `edgecore_stale_keys` | `api_snapshots` | Count rows where `updated_at < now() - interval '10 min'` |
| `services_down` | `service_heartbeats` | Count rows where `last_seen_at < now() - '1800s'` |
| `abuse_blocks_24h` | `abuse_rollup_hourly` | `SUM(deny_count) WHERE hour > now() - '24h'` |

---

## 2. Users / Tiers / Invites

**Purpose:** User population, tier distribution, invite code usage.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/users` | `user_tiers`, `invite_codes`, `invite_redemptions`, `user_sessions`, `tier_history` | 5min TTL |

### Payload: `/admin/telemetry/users`

```json
{
  "generated_at": "ISO-8601",
  "total_users": 245,
  "tier_distribution": {
    "free": 120,
    "pro": 80,
    "power": 30,
    "admin": 15
  },
  "active_sessions_24h": 68,
  "invite_codes": {
    "total_generated": 50,
    "total_redeemed": 38,
    "codes": [
      {
        "code": "EDGE2026",
        "created_at": "ISO-8601",
        "redeemed_count": 12,
        "max_uses": 20,
        "tier": "pro"
      }
    ]
  },
  "recent_tier_changes": [
    {
      "user_id": 123456,
      "old_tier": "free",
      "new_tier": "pro",
      "changed_at": "ISO-8601",
      "reason": "invite_code"
    }
  ]
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `total_users` | `user_tiers` | `COUNT(*)` |
| `tier_distribution` | `user_tiers` | `GROUP BY tier` |
| `active_sessions_24h` | `user_sessions` | `COUNT(DISTINCT user_id) WHERE last_active > now() - '24h'` |
| `invite_codes` | `invite_codes` + `invite_redemptions` | JOIN on code |
| `recent_tier_changes` | `tier_history` | `ORDER BY changed_at DESC LIMIT 20` |

---

## 3. Menu Toggles

**Purpose:** Track which power-tester menu features are enabled/disabled.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/menu-toggles` | `menu_toggle_events`, `user_menu_prefs` | 5min TTL |

### Payload: `/admin/telemetry/menu-toggles`

```json
{
  "generated_at": "ISO-8601",
  "toggle_events_24h": 15,
  "feature_usage": [
    {
      "menu_id": "prolab_derivs",
      "label": "Pro Lab Derivs",
      "enabled_count": 25,
      "disabled_count": 3,
      "total_users": 28,
      "last_toggle_at": "ISO-8601"
    }
  ],
  "recent_toggles": [
    {
      "user_id": 123456,
      "menu_id": "intelhub",
      "action": "enable",
      "toggled_at": "ISO-8601"
    }
  ]
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `toggle_events_24h` | `menu_toggle_events` | `COUNT(*) WHERE ts > now() - '24h'` |
| `feature_usage` | `user_menu_prefs` | `GROUP BY menu_id` |
| `recent_toggles` | `menu_toggle_events` | `ORDER BY ts DESC LIMIT 20` |

---

## 4. Provider / API Usage

**Purpose:** Track external API call volume, errors, latency, and cache hit rates.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/api-usage` | `metrics_api_calls`, `metrics_cache`, `edge_api_usage`, `edge_ws_usage`, `edge_cache_stats` | 5min TTL |

### Payload: `/admin/telemetry/api-usage`

```json
{
  "generated_at": "ISO-8601",
  "api_calls_24h": {
    "total": 5400,
    "by_provider": {
      "coingecko": { "calls": 1200, "errors": 3, "avg_ms": 220 },
      "coinglass": { "calls": 800, "errors": 1, "avg_ms": 340 },
      "binance": { "calls": 2400, "errors": 0, "avg_ms": 85 },
      "bybit": { "calls": 1000, "errors": 5, "avg_ms": 120 }
    }
  },
  "cache_stats": {
    "hit_rate_pct": 87.3,
    "total_hits": 12000,
    "total_misses": 1750
  },
  "edgecore_ws": {
    "connections_active": 3,
    "messages_24h": 48000
  }
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `api_calls_24h` | `metrics_api_calls` | `GROUP BY provider WHERE ts > now() - '24h'` |
| `cache_stats` | `metrics_cache` / `edge_cache_stats` | Latest row or 24h aggregate |
| `edgecore_ws` | `edge_ws_usage` | Latest row |

---

## 5. EdgeCore Freshness

**Purpose:** Show snapshot freshness per key, flag stale data.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/edgecore` | `api_snapshots` | 60s TTL |

### Payload: `/admin/telemetry/edgecore`

```json
{
  "generated_at": "ISO-8601",
  "total_keys": 24,
  "stale_keys": 2,
  "snapshots": [
    {
      "snapshot_key": "coingecko:price_simple:usd:bitcoin",
      "updated_at": "ISO-8601",
      "age_s": 45,
      "ttl_s": 300,
      "status": "fresh",
      "payload_bytes": 1024
    }
  ]
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `snapshots` | `api_snapshots` | `SELECT snapshot_key, updated_at, octet_length(payload::text)` |
| `status` | Computed | `fresh` if `age < ttl`, `stale` if `age < 2*ttl`, else `dead` |

**Note:** TTLs are defined in `edgecore/snapshots/keys.py` `_REGISTRY` dict in the bot repo.
If that file is missing, fallback TTLs in bot.py else clauses apply (>= 300s).

---

## 6. Scanners

**Purpose:** Scanner run history, cache freshness, hit rates.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/scanners` | `scanner_run_meta`, `scanner_cache` | 5min TTL |

### Payload: `/admin/telemetry/scanners`

```json
{
  "generated_at": "ISO-8601",
  "scanners": [
    {
      "scanner_id": "whale_flow",
      "last_run_at": "ISO-8601",
      "last_duration_s": 12.3,
      "last_result_count": 45,
      "cache_age_s": 180,
      "status": "fresh"
    }
  ]
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `scanners` | `scanner_run_meta` | `DISTINCT ON (scanner_id) ORDER BY run_at DESC` |
| `cache_age_s` | `scanner_cache` | `EXTRACT(EPOCH FROM now() - updated_at)` |

**Constraint:** No changes to Binance/Bybit scanner internals.

---

## 7. Paper Trader

**Purpose:** Paper trading volume, PnL summaries, active accounts.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/paper` | `paper_trades`, `paper_trade_events`, `paper_accounts`, `paper_positions` | 5min TTL |

### Payload: `/admin/telemetry/paper`

```json
{
  "generated_at": "ISO-8601",
  "active_accounts": 18,
  "open_positions": 32,
  "trades_24h": 120,
  "events_24h": 340,
  "total_pnl_usd": 1250.50,
  "top_accounts": [
    {
      "account_id": "uuid",
      "user_id": 123456,
      "trade_count": 45,
      "realized_pnl": 320.0,
      "open_positions": 3
    }
  ]
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `active_accounts` | `paper_accounts` | `COUNT(*) WHERE active = true` |
| `open_positions` | `paper_positions` | `COUNT(*) WHERE closed_at IS NULL` |
| `trades_24h` | `paper_trades` | `COUNT(*) WHERE created_at > now() - '24h'` |
| `events_24h` | `paper_trade_events` | `COUNT(*) WHERE ts > now() - '24h'` |
| `top_accounts` | `paper_trades` | `GROUP BY account_id ORDER BY SUM(pnl) DESC LIMIT 10` |

---

## 8. Alerts / Alertd

**Purpose:** Alert firing rates, lifecycle events, daemon job metrics.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/alerts` | `alert_lifecycle`, `alert_fire_log`, `alertd_job_metrics`, `service_heartbeats` | 5min TTL |

### Payload: `/admin/telemetry/alerts`

```json
{
  "generated_at": "ISO-8601",
  "fired_24h": 85,
  "dismissed_24h": 40,
  "active_alerts": 22,
  "lifecycle_breakdown": {
    "created": 90,
    "fired": 85,
    "dismissed": 40,
    "expired": 10
  },
  "alertd_jobs": [
    {
      "job_name": "v2",
      "last_run_at": "ISO-8601",
      "last_duration_s": 39.5,
      "ok": true,
      "interval_s": 300,
      "sent_24h": 12,
      "errors_24h": 0
    }
  ],
  "alertd_status": "up"
}
```

### Data Sources

| Field | Table | Query |
|-------|-------|-------|
| `fired_24h` | `alert_lifecycle` | `COUNT(*) WHERE event = 'fired' AND ts > now() - '24h'` |
| `lifecycle_breakdown` | `alert_lifecycle` | `GROUP BY event WHERE ts > now() - '24h'` |
| `alertd_jobs` | `alertd_job_metrics` | Latest row per job |
| `alertd_status` | `service_heartbeats` | `WHERE service_name = 'eventedge-alertd'` |

---

## 9. DB / Backups

**Purpose:** Database size, table bloat, backup status.

| Endpoint | Data Sources | Cadence |
|----------|-------------|---------|
| `GET /api/v1/admin/telemetry/db` | `pg_stat_user_tables`, `pg_database_size()`, `metrics_system` | 10min TTL |

### Payload: `/admin/telemetry/db`

```json
{
  "generated_at": "ISO-8601",
  "database_size_mb": 2400,
  "table_count": 151,
  "largest_tables": [
    {
      "table_name": "telemetry_events",
      "size_mb": 450,
      "row_estimate": 2400000,
      "last_vacuum": "ISO-8601",
      "last_analyze": "ISO-8601"
    }
  ],
  "system_metrics": {
    "cpu_pct": 12.5,
    "mem_used_mb": 3200,
    "disk_used_pct": 45.2
  }
}
```

### Data Sources

| Field | Table / Function | Query |
|-------|-----------------|-------|
| `database_size_mb` | `pg_database_size()` | `pg_database_size('eventedge') / 1048576` |
| `largest_tables` | `pg_stat_user_tables` + `pg_total_relation_size()` | Top 20 by size |
| `system_metrics` | `metrics_system` | Latest row |

---

## Auth Notes

- **Phase 1 (now):** No endpoint auth — Cloudflare Access protects admin.edgeblocks.io.
- **Phase 2 (planned):** Add `X-Admin-Token` header check or JWT middleware.
- All `/admin/` endpoints should be excluded from public API docs.

## Implementation Priority

| Priority | Page | Complexity | Depends On |
|----------|------|-----------|------------|
| P0 | Overview | Low | All others (aggregates) |
| P1 | Alerts/Alertd | Medium | alert_lifecycle, alertd_job_metrics |
| P1 | EdgeCore Freshness | Low | api_snapshots |
| P1 | Users/Tiers/Invites | Medium | user_tiers, invite_codes |
| P2 | Paper Trader | Medium | paper_trades (existing endpoint extends) |
| P2 | API Usage | Medium | metrics_api_calls |
| P2 | Menu Toggles | Low | menu_toggle_events |
| P3 | Scanners | Low | scanner_run_meta |
| P3 | DB/Backups | Medium | pg_stat + metrics_system |
