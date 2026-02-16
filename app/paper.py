"""Paper trader summary — best-effort rollups from bot paper tables."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from .db import get_conn
from .snapshots import fmt_usd

logger = logging.getLogger(__name__)


def _downsample(points: List[Dict[str, Any]], max_pts: int = 60) -> List[Dict[str, Any]]:
    if len(points) <= max_pts:
        return points
    step = max(1, len(points) // max_pts)
    out = points[::step]
    if out and points[-1] != out[-1]:
        out[-1] = points[-1]
    return out[:max_pts]


def _max_drawdown(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd
    return mdd * 100.0


def build_paper_summary() -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    since_30d = now - dt.timedelta(days=30)

    payload: Dict[str, Any] = {
        "ts": now.isoformat(),
        "version": "v0.3-live",
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

            # --- equity curve + drawdown ---
            curve: List[Dict[str, Any]] = []

            # Primary: pt_equity_snapshots (daily total equity across accounts)
            cur.execute(
                """
                SELECT bucket_ts::date AS d, sum(equity_usd)::float AS eq
                FROM pt_equity_snapshots
                WHERE bucket_ts >= %s
                GROUP BY 1
                ORDER BY 1 ASC
                """,
                (since_30d,),
            )
            rows = cur.fetchall()
            if rows:
                curve = [{"t": str(d), "v": round(eq, 2)} for d, eq in rows]

            # Fallback: cumulative PnL from paper_trades
            if not curve:
                cur.execute(
                    """
                    SELECT created_at::date AS d, sum(net_pnl_usdt)::float AS pnl
                    FROM paper_trades
                    WHERE created_at >= %s
                    GROUP BY 1
                    ORDER BY 1 ASC
                    """,
                    (since_30d,),
                )
                rows = cur.fetchall()
                if rows:
                    cum = 0.0
                    for d, pnl in rows:
                        cum += float(pnl or 0.0)
                        curve.append({"t": str(d), "v": round(cum, 2)})

            if curve:
                curve = _downsample(curve)
                payload["sample"]["name"] = "30d equity"
                payload["sample"]["equity_curve"] = curve
                payload["kpis"]["equity_30d"] = fmt_usd(curve[-1]["v"])
                eq_vals = [float(p["v"]) for p in curve]
                mdd = _max_drawdown(eq_vals)
                if mdd is not None:
                    payload["kpis"]["max_drawdown"] = f"{mdd:.1f}%"

    return payload
