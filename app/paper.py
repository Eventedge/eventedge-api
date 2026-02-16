"""Paper trader summary — best-effort rollups from bot paper tables."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

from .db import get_conn

logger = logging.getLogger(__name__)


def build_paper_summary() -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    since_30d = now - dt.timedelta(days=30)

    payload: Dict[str, Any] = {
        "ts": now.isoformat(),
        "version": "v0.2-live",
        "accounts": {"active": 0, "tracked": 0},
        "kpis": {
            "equity_30d": "\u2014",
            "win_rate": "\u2014",
            "max_drawdown": "\u2014",
            "active_positions": "\u2014",
        },
        "sample": {"name": "\u2014", "equity_curve": []},
        "disclaimer": "Best-effort rollups from bot paper tables.",
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            # --- accounts ---
            cur.execute(
                "SELECT count(*) FILTER (WHERE is_active), count(*) FROM paper_accounts_v3"
            )
            active_accts, total_accts = cur.fetchone()
            payload["accounts"]["active"] = int(active_accts or 0)
            payload["accounts"]["tracked"] = int(total_accts or 0)

            # --- active positions ---
            cur.execute(
                "SELECT count(*) FROM paper_positions WHERE status = 'open'"
            )
            active_pos = cur.fetchone()[0]
            payload["kpis"]["active_positions"] = str(int(active_pos or 0))

            # --- win rate (last 30d closed trades) ---
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE net_pnl_usdt > 0) AS wins,
                    count(*) FILTER (WHERE net_pnl_usdt < 0) AS losses,
                    count(*) AS total
                FROM paper_trades
                WHERE created_at >= %s
                """,
                (since_30d,),
            )
            wins, losses, total_trades = cur.fetchone()
            wins = int(wins or 0)
            losses = int(losses or 0)
            total_trades = int(total_trades or 0)
            if total_trades > 0:
                wr = (wins / total_trades) * 100.0
                payload["kpis"]["win_rate"] = f"{wr:.0f}% ({wins}W / {losses}L / {total_trades})"

    return payload
