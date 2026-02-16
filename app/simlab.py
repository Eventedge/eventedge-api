from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any, Dict, List, Optional

from .db import get_conn


def _hash_id(x: Any) -> str:
    b = str(x).encode("utf-8", "ignore")
    return hashlib.sha1(b).hexdigest()[:10]


def _fmt_usdt(x: Optional[float]) -> str:
    if x is None:
        return "\u2014"
    try:
        v = float(x)
    except Exception:
        return "\u2014"
    s = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{s}${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{s}${v/1_000:.1f}K"
    return f"{s}${v:.2f}"


def _fmt_pct(x: Optional[float], digits: int = 0) -> str:
    if x is None:
        return "\u2014"
    try:
        return f"{float(x):.{digits}f}%"
    except Exception:
        return "\u2014"


def _downsample(points: List[Dict[str, float]], max_points: int = 60) -> List[Dict[str, float]]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    out = points[::step]
    if out and points[-1] != out[-1]:
        out[-1] = points[-1]
    return out[:max_points]


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


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "select 1 from information_schema.tables where table_schema='public' and table_name=%s limit 1",
        (name,),
    )
    return cur.fetchone() is not None


def _get_admin_account_ids(cur, tg_id: int) -> List[Any]:
    """Get account IDs for the admin user. paper_accounts_v3.user_id is the TG user id."""
    if not _table_exists(cur, "paper_accounts_v3"):
        return []
    try:
        cur.execute("select account_id from paper_accounts_v3 where user_id = %s", (tg_id,))
        rows = cur.fetchall()
        return [r[0] for r in rows if r and r[0] is not None]
    except Exception:
        return []


def build_simlab_overview(tg_id: int, days: int = 30) -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=max(1, min(int(days or 30), 90)))

    out: Dict[str, Any] = {
        "ts": now.isoformat(),
        "version": "v0.1",
        "admin": {"tg_id": tg_id, "accounts": {"total": 0, "active": 0}},
        "kpis": {"pnl_30d_usdt": "\u2014", "win_rate": "\u2014", "trades_30d": 0, "open_positions": 0, "max_drawdown": "\u2014"},
        "curve": [],
        "per_account": [],
        "disclaimer": "SimLab admin live feed (paper). Identifiers are redacted. Metrics are best-effort based on available tables.",
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            acct_ids = _get_admin_account_ids(cur, tg_id)
            if not acct_ids:
                return out

            out["admin"]["accounts"]["total"] = len(acct_ids)

            # Open positions
            open_positions = 0
            active_accounts = set()
            if _table_exists(cur, "paper_positions"):
                cur.execute(
                    "select account_id, count(*)::int from paper_positions where status='open' and account_id = any(%s) group by 1",
                    (acct_ids,),
                )
                for aid, c in cur.fetchall():
                    c = int(c or 0)
                    if c > 0:
                        active_accounts.add(aid)
                    open_positions += c

            out["admin"]["accounts"]["active"] = len(active_accounts) if active_accounts else len(acct_ids)
            out["kpis"]["open_positions"] = open_positions

            # Trades + pnl + win rate + curve
            wins = losses = 0
            pnl_sum = 0.0
            trades_n = 0
            last_trade_by_acct: Dict[Any, Optional[str]] = {aid: None for aid in acct_ids}

            if _table_exists(cur, "paper_trades"):
                cur.execute(
                    """
                    select account_id, created_at, net_pnl_usdt
                    from paper_trades
                    where created_at >= %s and account_id = any(%s)
                    order by created_at asc
                    """,
                    (since, acct_ids),
                )
                rows = cur.fetchall()
                trades_n = len(rows)
                for aid, created_at, net_pnl in rows:
                    p = float(net_pnl or 0.0)
                    pnl_sum += p
                    if p > 0:
                        wins += 1
                    elif p < 0:
                        losses += 1
                    last_trade_by_acct[aid] = str(created_at)

                out["kpis"]["trades_30d"] = trades_n
                out["kpis"]["pnl_30d_usdt"] = _fmt_usdt(pnl_sum)
                total = wins + losses
                if total > 0:
                    out["kpis"]["win_rate"] = _fmt_pct((wins / total) * 100.0, 0)

                cur.execute(
                    """
                    select created_at::date as d, sum(net_pnl_usdt)::float as pnl
                    from paper_trades
                    where created_at >= %s and account_id = any(%s)
                    group by 1
                    order by 1 asc
                    """,
                    (since, acct_ids),
                )
                cum = 0.0
                curve: List[Dict[str, float]] = []
                for d, pnl in cur.fetchall():
                    cum += float(pnl or 0.0)
                    curve.append({"t": str(d), "v": round(cum, 2)})
                curve = _downsample(curve, 60)
                out["curve"] = curve
                mdd = _max_drawdown([float(p["v"]) for p in curve if isinstance(p.get("v"), (int, float))])
                if mdd is not None:
                    out["kpis"]["max_drawdown"] = _fmt_pct(mdd, 1)

            # Per-account rollups
            per: List[Dict[str, Any]] = []
            for aid in acct_ids:
                per.append(
                    {
                        "id": _hash_id(aid),
                        "name": f"acct-{_hash_id(aid)[:4]}",
                        "pnl_30d_usdt": "\u2014",
                        "win_rate": "\u2014",
                        "open_positions": 0,
                        "last_trade_ts": last_trade_by_acct.get(aid),
                    }
                )

            if _table_exists(cur, "paper_trades"):
                cur.execute(
                    """
                    select account_id,
                           sum(net_pnl_usdt)::float as pnl,
                           sum(case when net_pnl_usdt > 0 then 1 else 0 end)::int as wins,
                           sum(case when net_pnl_usdt < 0 then 1 else 0 end)::int as losses
                    from paper_trades
                    where created_at >= %s and account_id = any(%s)
                    group by 1
                    """,
                    (since, acct_ids),
                )
                agg = {r[0]: (float(r[1] or 0.0), int(r[2] or 0), int(r[3] or 0)) for r in cur.fetchall()}
                for i, aid in enumerate(acct_ids):
                    if aid in agg:
                        pnl, w, l = agg[aid]
                        per[i]["pnl_30d_usdt"] = _fmt_usdt(pnl)
                        tot = w + l
                        if tot > 0:
                            per[i]["win_rate"] = _fmt_pct((w / tot) * 100.0, 0)

            if _table_exists(cur, "paper_positions"):
                cur.execute(
                    "select account_id, count(*)::int from paper_positions where status='open' and account_id = any(%s) group by 1",
                    (acct_ids,),
                )
                pos = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
                for i, aid in enumerate(acct_ids):
                    per[i]["open_positions"] = pos.get(aid, 0)

            out["per_account"] = per

    return out


def build_simlab_trades_live(tg_id: int, limit: int = 50) -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    out: Dict[str, Any] = {
        "ts": now.isoformat(),
        "version": "v0.1",
        "admin": {"tg_id": tg_id},
        "items": [],
        "disclaimer": "SimLab live trades feed (paper). Identifiers redacted.",
    }

    limit = max(1, min(int(limit or 50), 200))

    with get_conn() as conn:
        with conn.cursor() as cur:
            acct_ids = _get_admin_account_ids(cur, tg_id)
            if not acct_ids or not _table_exists(cur, "paper_trades"):
                return out

            cur.execute(
                """
                select created_at,
                       account_id,
                       symbol,
                       side,
                       coalesce(qty, 0)::float as qty,
                       coalesce(entry_price, entry_price_fill, 0)::float as price,
                       coalesce(net_pnl_usdt, 0)::float as pnl
                from paper_trades
                where account_id = any(%s)
                order by created_at desc
                limit %s
                """,
                (acct_ids, limit),
            )
            items: List[Dict[str, Any]] = []
            for created_at, aid, symbol, side, qty, price, pnl in cur.fetchall():
                items.append(
                    {
                        "t": str(created_at),
                        "account": _hash_id(aid),
                        "symbol": symbol or "\u2014",
                        "side": side or "\u2014",
                        "qty": round(float(qty or 0.0), 4),
                        "price": round(float(price or 0.0), 4),
                        "pnl_usdt": round(float(pnl or 0.0), 2),
                    }
                )
            out["items"] = items

    return out
