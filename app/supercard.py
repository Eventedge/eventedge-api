"""SuperCard builder — populates pillar values from live snapshots.

Uses the same DB snapshots that market/overview consumes, plus the
altme:fear_greed dataset.  Every pillar degrades gracefully to "—" if
its source snapshot is missing.

No formulas or weights are disclosed — only interpretable labels/buckets.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .snapshots import (
    extract_funding,
    extract_global,
    extract_liquidations,
    extract_oi,
    extract_price,
    fmt_pct,
    fmt_usd,
    get_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bucket(x: Optional[float], lo: float, hi: float) -> str:
    """Return 'low' | 'neutral' | 'high' for a scalar."""
    if x is None:
        return "neutral"
    if x <= lo:
        return "low"
    if x >= hi:
        return "high"
    return "neutral"


def _status(bucket: str) -> str:
    return {"high": "positive", "low": "negative"}.get(bucket, "neutral")


def _confidence(parts_ok: int) -> str:
    if parts_ok >= 5:
        return "high"
    if parts_ok >= 3:
        return "medium"
    return "low"


def _stance(
    chg24: Optional[float],
    fg_val: Optional[int],
    funding_pct: Optional[float],
    liq_long_pct: Optional[float],
) -> str:
    """Coarse stance label — interpretation layer, not a disclosed model."""
    if fg_val is not None and fg_val <= 25 and (chg24 is not None and chg24 < 0):
        return "cautious"
    if funding_pct is not None and funding_pct >= 0.10 and (
        liq_long_pct is not None and liq_long_pct >= 70
    ):
        return "crowded-longs"
    if chg24 is not None and chg24 > 1.0:
        return "risk-on"
    return "neutral"


def _read_fear_greed() -> Tuple[Optional[int], Optional[str]]:
    """Read current fear/greed value + label from the DB snapshot."""
    ds = get_snapshot("altme:fear_greed")
    if not ds:
        return None, None
    payload = ds.get("payload") or {}
    data = payload.get("data") or []
    if not data or not isinstance(data[0], dict):
        return None, None
    row0 = data[0]
    v_raw = row0.get("value")
    try:
        v = int(v_raw)
    except (TypeError, ValueError):
        v = None
    label = row0.get("value_classification")
    return v, label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_supercard(symbol: str) -> Dict[str, Any]:
    sym = (symbol or "BTC").upper()
    if sym not in ("BTC", "ETH"):
        sym = "BTC"
    cg_id = "bitcoin" if sym == "BTC" else "ethereum"

    # ---- fetch snapshots ----
    price_snap = get_snapshot(f"coingecko:price_simple:usd:{cg_id}")
    funding_snap = get_snapshot(f"coinglass:oi_weighted_funding:{sym}")
    oi_snap = get_snapshot(f"coinglass:open_interest:{sym}")
    liq_snap = get_snapshot(f"coinglass:liquidations:{sym}")
    global_snap = get_snapshot("coingecko:global")

    # ---- extract signals ----
    price, chg24 = extract_price(price_snap["payload"]) if price_snap else (None, None)
    # extract_funding returns a percentage float (rate * 100)
    funding_pct = extract_funding(funding_snap["payload"]) if funding_snap else None
    oi = extract_oi(oi_snap["payload"]) if oi_snap else {}
    liq = extract_liquidations(liq_snap["payload"]) if liq_snap else {}
    glob = extract_global(global_snap["payload"]) if global_snap else {}

    oi_usd = oi.get("oi_usd")
    oi_chg24 = oi.get("oi_change_24h")
    liq_total = liq.get("total_usd")
    liq_long_pct = liq.get("long_pct")
    liq_short_pct = liq.get("short_pct")
    btc_dom = glob.get("btc_dominance")
    total_vol = glob.get("total_vol_usd")

    fg_val, fg_label = _read_fear_greed()

    # ---- build pillars ----
    parts_ok = 0

    # Flow: liquidation intensity + global volume as market-pressure proxy
    flow_bkt = _bucket(liq_total, 25_000_000.0, 120_000_000.0)
    has_flow = liq_total is not None or total_vol is not None
    flow = {
        "key": "flow",
        "label": "Flow",
        "value": (
            f"{fmt_usd(liq_total)} liqs / {fmt_usd(total_vol)} vol"
            if has_flow else "—"
        ),
        "status": _status(flow_bkt),
        "hint": "pressure proxy (liqs/volume)",
    }
    if has_flow:
        parts_ok += 1

    # Leverage: OI + funding
    lev_bkt = _bucket(funding_pct, -0.02, 0.10)
    has_lev = oi_usd is not None or funding_pct is not None
    leverage = {
        "key": "leverage",
        "label": "Leverage",
        "value": (
            f"{fmt_usd(oi_usd)} OI \u2022 {fmt_pct(funding_pct, 3)} funding"
            if has_lev else "—"
        ),
        "status": _status(lev_bkt),
        "hint": "OI + funding stress",
    }
    if has_lev:
        parts_ok += 1

    # Fragility: liquidation skew
    frag_bkt = _bucket(liq_long_pct, 40.0, 70.0)
    has_frag = liq_long_pct is not None and liq_short_pct is not None
    fragility = {
        "key": "fragility",
        "label": "Fragility",
        "value": (
            f"{fmt_pct(liq_long_pct, 0)} long / {fmt_pct(liq_short_pct, 0)} short"
            if has_frag else "—"
        ),
        "status": _status(frag_bkt),
        "hint": "liq imbalance + spikes",
    }
    if has_frag:
        parts_ok += 1

    # Momentum: 24h change + price
    mom_bkt = _bucket(chg24, -1.0, 1.0)
    has_mom = price is not None or chg24 is not None
    momentum = {
        "key": "momentum",
        "label": "Momentum",
        "value": (
            f"{fmt_usd(price)} \u2022 {fmt_pct(chg24)} 24h"
            if has_mom else "—"
        ),
        "status": _status(mom_bkt),
        "hint": "trend + volatility",
    }
    if has_mom:
        parts_ok += 1

    # Sentiment: fear & greed
    if fg_val is not None:
        if fg_val <= 25:
            sent_bkt = "low"
        elif fg_val >= 60:
            sent_bkt = "high"
        else:
            sent_bkt = "neutral"
    else:
        sent_bkt = "neutral"
    sentiment = {
        "key": "sentiment",
        "label": "Sentiment",
        "value": (
            f"{fg_val} \u2014 {fg_label}"
            if fg_val is not None and fg_label else
            (str(fg_val) if fg_val is not None else "—")
        ),
        "status": _status(sent_bkt),
        "hint": "fear/greed index",
    }
    if fg_val is not None:
        parts_ok += 1

    # Risk: OI change + BTC dominance
    risk_bkt = _bucket(oi_chg24, -2.0, 2.0)
    has_risk = oi_chg24 is not None or btc_dom is not None
    risk = {
        "key": "risk",
        "label": "Risk",
        "value": (
            f"OI {fmt_pct(oi_chg24)} \u2022 BTC dom {fmt_pct(btc_dom, 1)}"
            if has_risk else "—"
        ),
        "status": _status(risk_bkt),
        "hint": "regime + confidence",
    }
    if has_risk:
        parts_ok += 1

    # ---- summary ----
    stance_label = _stance(chg24, fg_val, funding_pct, liq_long_pct)
    conf = _confidence(parts_ok)

    notes: list[str] = []
    if funding_pct is not None:
        notes.append("Funding reflects positioning pressure (crowding proxy).")
    if liq_total is not None:
        notes.append("Liquidations help gauge fragility and forced flow.")
    if fg_val is not None:
        notes.append("Sentiment adds a behavioral context layer.")
    while len(notes) < 3:
        notes.append("\u2014")
    notes = notes[:3]

    return {
        "ts": None,  # filled by caller
        "symbol": sym,
        "version": "v0.2-live",
        "summary": {
            "headline": f"{sym} SuperCard",
            "stance": stance_label,
            "confidence": conf,
            "notes": notes,
        },
        "pillars": [flow, leverage, fragility, momentum, sentiment, risk],
        "disclaimer": (
            "Interpretation signals derived from live snapshots. "
            "Values are intentionally high-level (no methodology disclosed)."
        ),
    }
