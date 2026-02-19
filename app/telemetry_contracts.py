"""Typed response contracts for admin telemetry endpoints.

These are *contract definitions only* — no DB access, no side effects.
Each dataclass mirrors the JSON shape documented in docs/ADMIN_TELEMETRY_PLAN.md.

Usage in future endpoint modules:

    from .telemetry_contracts import OverviewPayload
    payload = OverviewPayload(...)
    return dataclasses.asdict(payload)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

@dataclass
class OverviewPayload:
    generated_at: str
    active_users_24h: int = 0
    commands_24h: int = 0
    alerts_fired_24h: int = 0
    paper_trades_24h: int = 0
    edgecore_stale_keys: int = 0
    services_down: int = 0
    abuse_blocks_24h: int = 0


# ---------------------------------------------------------------------------
# 2. Users / Tiers / Invites
# ---------------------------------------------------------------------------

@dataclass
class TierEntry:
    tier: str
    count: int


@dataclass
class LastSeenBuckets:
    h24: int = 0
    d7: int = 0
    gt7d: int = 0
    unknown: int = 0


@dataclass
class RecentUserEntry:
    user_id: int
    tier: str
    created_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    invite_code: Optional[str] = None
    invite_source: Optional[int] = None


@dataclass
class UsersBlock:
    total_users: Optional[int] = None
    new_24h: Optional[int] = None
    new_7d: Optional[int] = None
    active_24h: Optional[int] = None
    active_7d: Optional[int] = None
    tiers: list[TierEntry] = field(default_factory=list)
    last_seen_buckets: Optional[LastSeenBuckets] = None
    recent: list[RecentUserEntry] = field(default_factory=list)


@dataclass
class UsersPayload:
    generated_at: str
    ok: bool = True
    users: UsersBlock = field(default_factory=UsersBlock)


# ---------------------------------------------------------------------------
# 2b. Invites
# ---------------------------------------------------------------------------

@dataclass
class InviteByTierEntry:
    tier: str
    codes: int
    total_uses: int


@dataclass
class InviteCodeDetail:
    code: str
    tier: str
    uses: int
    max_uses: Optional[int]
    is_enabled: bool
    created_at: Optional[str] = None
    created_by: Optional[int] = None
    expires_at: Optional[str] = None
    note: Optional[str] = None


@dataclass
class RecentRedemptionEntry:
    user_id: int
    code: str
    redeemed_at: Optional[str] = None
    tier: Optional[str] = None


@dataclass
class InvitesBlock:
    total_codes: Optional[int] = None
    created_24h: Optional[int] = None
    created_7d: Optional[int] = None
    active: Optional[int] = None
    disabled: Optional[int] = None
    expired: Optional[int] = None
    total_redeemed: Optional[int] = None
    redeemed_24h: Optional[int] = None
    redeemed_7d: Optional[int] = None
    by_tier: list[InviteByTierEntry] = field(default_factory=list)
    top_codes: list[InviteCodeDetail] = field(default_factory=list)
    recent_redemptions: list[RecentRedemptionEntry] = field(default_factory=list)


@dataclass
class InvitesPayload:
    generated_at: str
    ok: bool = True
    invites: InvitesBlock = field(default_factory=InvitesBlock)


# ---------------------------------------------------------------------------
# 3. Menu Toggles
# ---------------------------------------------------------------------------

@dataclass
class FeatureUsageEntry:
    menu_id: str
    label: str
    enabled_count: int
    disabled_count: int
    total_users: int
    last_toggle_at: Optional[str]


@dataclass
class RecentToggleEntry:
    user_id: int
    menu_id: str
    action: str  # "enable" | "disable"
    toggled_at: str


@dataclass
class MenuTogglesPayload:
    generated_at: str
    toggle_events_24h: int = 0
    feature_usage: list[FeatureUsageEntry] = field(default_factory=list)
    recent_toggles: list[RecentToggleEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 4. Provider / API Usage
# ---------------------------------------------------------------------------

@dataclass
class ProviderStats:
    calls: int = 0
    errors: int = 0
    avg_ms: float = 0.0


@dataclass
class CacheStats:
    hit_rate_pct: float = 0.0
    total_hits: int = 0
    total_misses: int = 0


@dataclass
class EdgeCoreWsStats:
    connections_active: int = 0
    messages_24h: int = 0


@dataclass
class ApiUsagePayload:
    generated_at: str
    api_calls_24h: dict[str, ProviderStats] = field(default_factory=dict)
    cache_stats: CacheStats = field(default_factory=CacheStats)
    edgecore_ws: EdgeCoreWsStats = field(default_factory=EdgeCoreWsStats)


# ---------------------------------------------------------------------------
# 5. EdgeCore Freshness
# ---------------------------------------------------------------------------

@dataclass
class SnapshotEntry:
    snapshot_key: str
    updated_at: Optional[str]
    age_s: float
    ttl_s: int
    status: str  # "fresh" | "stale" | "dead"
    payload_bytes: int = 0


@dataclass
class EdgeCorePayload:
    generated_at: str
    total_keys: int = 0
    stale_keys: int = 0
    snapshots: list[SnapshotEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 6. Scanners
# ---------------------------------------------------------------------------

@dataclass
class ScannerEntry:
    scanner_id: str
    last_run_at: Optional[str]
    last_duration_s: float
    last_result_count: int
    cache_age_s: float
    status: str  # "fresh" | "stale" | "unknown"


@dataclass
class ScannersPayload:
    generated_at: str
    scanners: list[ScannerEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 7. Paper Trader
# ---------------------------------------------------------------------------

@dataclass
class TopAccountEntry:
    account_id: str
    user_id: int
    trade_count: int
    realized_pnl: float
    open_positions: int


@dataclass
class PaperPayload:
    generated_at: str
    active_accounts: int = 0
    open_positions: int = 0
    trades_24h: int = 0
    events_24h: int = 0
    total_pnl_usd: float = 0.0
    top_accounts: list[TopAccountEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 8. Alerts / Alertd
# ---------------------------------------------------------------------------

@dataclass
class AlertdJobEntry:
    job_name: str
    last_run_at: Optional[str]
    last_duration_s: float
    ok: bool
    interval_s: int
    sent_24h: int = 0
    errors_24h: int = 0


@dataclass
class AlertsPayload:
    generated_at: str
    fired_24h: int = 0
    dismissed_24h: int = 0
    active_alerts: int = 0
    lifecycle_breakdown: dict[str, int] = field(default_factory=dict)
    alertd_jobs: list[AlertdJobEntry] = field(default_factory=list)
    alertd_status: str = "unknown"


# ---------------------------------------------------------------------------
# 9. DB / Backups
# ---------------------------------------------------------------------------

@dataclass
class TableSizeEntry:
    table_name: str
    size_mb: float
    row_estimate: int
    last_vacuum: Optional[str]
    last_analyze: Optional[str]


@dataclass
class SystemMetrics:
    cpu_pct: float = 0.0
    mem_used_mb: float = 0.0
    disk_used_pct: float = 0.0


@dataclass
class DbPayload:
    generated_at: str
    database_size_mb: float = 0.0
    table_count: int = 0
    largest_tables: list[TableSizeEntry] = field(default_factory=list)
    system_metrics: SystemMetrics = field(default_factory=SystemMetrics)


# ---------------------------------------------------------------------------
# Endpoint registry (used by the stub summary route)
# ---------------------------------------------------------------------------

PLANNED_ENDPOINTS: list[dict[str, str]] = [
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/overview",
        "description": "Top-level KPI counters (users, commands, alerts, trades, stale keys)",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/users",
        "description": "User population, tier distribution, invite codes, recent tier changes",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/menu-toggles",
        "description": "Feature toggle usage and recent toggle events",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/api-usage",
        "description": "External API call volume, errors, latency, cache hit rates",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/edgecore",
        "description": "Snapshot freshness per key, stale/dead detection",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/scanners",
        "description": "Scanner run history, cache freshness, result counts",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/paper",
        "description": "Paper trading volume, PnL summaries, active accounts",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/alerts",
        "description": "Alert lifecycle events, firing rates, alertd job metrics",
        "status": "planned",
    },
    {
        "method": "GET",
        "path": "/api/v1/admin/telemetry/db",
        "description": "Database size, table bloat, system metrics, backup status",
        "status": "planned",
    },
]
