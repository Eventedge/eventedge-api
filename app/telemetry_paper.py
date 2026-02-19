"""GET /api/v1/admin/telemetry/paper — paper trading stats.

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


def _accounts(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "paper_accounts_v3"):
        return _unavailable()

    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE is_active), COUNT(*) FROM paper_accounts_v3"
    )
    active, total = cur.fetchone()
    return {
        "available": True,
        "active": int(active or 0),
        "total": int(total or 0),
    }


def _positions(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "paper_positions"):
        return _unavailable()

    cur.execute("SELECT COUNT(*) FROM paper_positions WHERE status = 'open'")
    open_pos = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM paper_positions")
    total = cur.fetchone()[0]

    return {
        "available": True,
        "open": int(open_pos or 0),
        "total": int(total or 0),
    }


def _trades(cur: Any) -> dict[str, Any]:
    if not _table_exists(cur, "paper_trades"):
        return _unavailable()

    cur.execute(
        "SELECT COUNT(*) FROM paper_trades "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    trades_24h = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM paper_trades "
        "WHERE created_at > NOW() - INTERVAL '7 days'"
    )
    trades_7d = cur.fetchone()[0]

    cur.execute(
        "SELECT "
        "  COUNT(*) FILTER (WHERE net_pnl_usdt > 0), "
        "  COUNT(*) FILTER (WHERE net_pnl_usdt < 0), "
        "  COUNT(*), "
        "  COALESCE(SUM(net_pnl_usdt), 0) "
        "FROM paper_trades "
        "WHERE created_at > NOW() - INTERVAL '30 days'"
    )
    wins, losses, total_30d, pnl_30d = cur.fetchone()

    return {
        "available": True,
        "trades_24h": int(trades_24h or 0),
        "trades_7d": int(trades_7d or 0),
        "trades_30d": int(total_30d or 0),
        "wins_30d": int(wins or 0),
        "losses_30d": int(losses or 0),
        "pnl_30d": round(float(pnl_30d or 0), 2),
    }


def _top_accounts(cur: Any) -> list[dict[str, Any]]:
    if not _table_exists(cur, "paper_trades"):
        return []

    has_accounts = _table_exists(cur, "paper_accounts_v3")

    if has_accounts:
        cur.execute(
            "SELECT t.account_id, a.user_id, "
            "  COUNT(*), COALESCE(SUM(t.net_pnl_usdt), 0) "
            "FROM paper_trades t "
            "LEFT JOIN paper_accounts_v3 a ON a.account_id = t.account_id "
            "WHERE t.created_at > NOW() - INTERVAL '30 days' "
            "GROUP BY t.account_id, a.user_id "
            "ORDER BY SUM(t.net_pnl_usdt) DESC LIMIT 10"
        )
    else:
        cur.execute(
            "SELECT account_id, NULL, "
            "  COUNT(*), COALESCE(SUM(net_pnl_usdt), 0) "
            "FROM paper_trades "
            "WHERE created_at > NOW() - INTERVAL '30 days' "
            "GROUP BY account_id "
            "ORDER BY SUM(net_pnl_usdt) DESC LIMIT 10"
        )

    return [
        {
            "account_id": str(row[0]),
            "user_id": row[1],
            "trades": int(row[2]),
            "pnl": round(float(row[3] or 0), 2),
        }
        for row in cur.fetchall()
    ]


def build_telemetry_paper() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    paper: dict[str, Any] = {}
    errors: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                accts = _accounts(cur)
                if accts.get("available"):
                    paper["accounts_active"] = accts["active"]
                    paper["accounts_total"] = accts["total"]
                else:
                    paper["accounts_active"] = None
                    paper["accounts_total"] = None
                    errors.append(f"accounts: {accts.get('reason')}")
            except Exception as exc:
                paper["accounts_active"] = None
                paper["accounts_total"] = None
                errors.append(f"accounts: {type(exc).__name__}")

            try:
                pos = _positions(cur)
                if pos.get("available"):
                    paper["positions_open"] = pos["open"]
                    paper["positions_total"] = pos["total"]
                else:
                    paper["positions_open"] = None
                    paper["positions_total"] = None
                    errors.append(f"positions: {pos.get('reason')}")
            except Exception as exc:
                paper["positions_open"] = None
                paper["positions_total"] = None
                errors.append(f"positions: {type(exc).__name__}")

            try:
                tr = _trades(cur)
                if tr.get("available"):
                    paper["trades_24h"] = tr["trades_24h"]
                    paper["trades_7d"] = tr["trades_7d"]
                    paper["trades_30d"] = tr["trades_30d"]
                    paper["wins_30d"] = tr["wins_30d"]
                    paper["losses_30d"] = tr["losses_30d"]
                    paper["pnl_30d"] = tr["pnl_30d"]
                else:
                    paper["trades_24h"] = None
                    paper["trades_7d"] = None
                    paper["trades_30d"] = None
                    paper["wins_30d"] = None
                    paper["losses_30d"] = None
                    paper["pnl_30d"] = None
                    errors.append(f"trades: {tr.get('reason')}")
            except Exception as exc:
                paper["trades_24h"] = None
                paper["trades_7d"] = None
                paper["trades_30d"] = None
                paper["wins_30d"] = None
                paper["losses_30d"] = None
                paper["pnl_30d"] = None
                errors.append(f"trades: {type(exc).__name__}")

            try:
                paper["top_accounts"] = _top_accounts(cur)
            except Exception as exc:
                paper["top_accounts"] = []
                errors.append(f"top_accounts: {type(exc).__name__}")

    result: dict[str, Any] = {"ok": True, "generated_at": now, "paper": paper}
    if errors:
        result["_errors"] = errors
    return result
