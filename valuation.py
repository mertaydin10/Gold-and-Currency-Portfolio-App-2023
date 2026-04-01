"""TL valuation, P&L, interpolated time series for charts."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

import database as db
from portfolio_service import default_asset_codes, rebuild_lots


def _snapshots_chrono() -> list[tuple[int, datetime]]:
    rows = db.list_price_snapshot_times()
    out: list[tuple[int, datetime]] = []
    for sid, iso in rows:
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            continue
        out.append((sid, dt))
    out.sort(key=lambda x: x[1])
    return out


def rate_on_snapshot(snapshot_id: int, asset_code: str, use: str = "selling") -> float | None:
    rates = db.get_rates_for_snapshot(snapshot_id)
    r = rates.get(asset_code)
    if not r:
        return None
    v = r.get("selling" if use == "selling" else "buying")
    return float(v) if v is not None else None


def pick_snapshot_for_day(day: date) -> int | None:
    """Latest fetch whose calendar date is on or before ``day``; else earliest."""
    snaps = _snapshots_chrono()
    if not snaps:
        return None
    best: int | None = None
    best_dt: datetime | None = None
    for sid, dt in snaps:
        d = dt.date()
        if d <= day:
            if best_dt is None or dt >= best_dt:
                best, best_dt = sid, dt
    if best is not None:
        return best
    return snaps[0][0]


def ordered_asset_codes() -> list[str]:
    sid = db.get_latest_snapshot_id()
    if not sid:
        return default_asset_codes()
    rates = db.get_rates_for_snapshot(sid)
    keys = list(rates.keys())
    order = default_asset_codes()
    seen: set[str] = set()
    out: list[str] = []
    for c in order:
        if c in rates:
            out.append(c)
            seen.add(c)
    for c in sorted(keys):
        if c not in seen:
            out.append(c)
    return out


def breakdown_for_snapshot_view(
    snapshot_id: int,
) -> tuple[dict[str, float], dict[str, float], float]:
    """(positions_qty, tl_per_asset, total_tl) for snapshot day × that snapshot's rates."""
    iso = db.get_snapshot_fetched_at(snapshot_id)
    if not iso:
        return {}, {}, 0.0
    try:
        snap_dt = datetime.fromisoformat(iso)
    except ValueError:
        snap_dt = datetime.now()
    day = snap_dt.date()
    saves = db.iter_portfolio_saves_ordered()
    pos = _positions_as_of(saves, day)
    per, total = value_in_tl(pos, snapshot_id, "selling")
    return pos, per, total


def value_in_tl(
    quantities: dict[str, float], snapshot_id: int, use: str = "selling"
) -> tuple[dict[str, float], float]:
    per: dict[str, float] = {}
    total = 0.0
    rates = db.get_rates_for_snapshot(snapshot_id)
    for code, qty in quantities.items():
        if qty <= 0:
            continue
        r = rates.get(code)
        if not r:
            per[code] = 0.0
            continue
        px = float(r["selling" if use == "selling" else "buying"])
        v = qty * px
        per[code] = v
        total += v
    return per, total


@dataclass
class LotPnL:
    asset_code: str
    amount: float
    entry_date: date
    days_held: int
    entry_tl: float
    current_tl: float
    pnl_tl: float
    pnl_pct: float


def compute_lot_pnl(as_of: date | None = None) -> list[LotPnL]:
    """P&L per FIFO lot vs entry-day rate and as-of (default today) rate."""
    as_of = as_of or date.today()
    lots_map = rebuild_lots()
    snap_now = pick_snapshot_for_day(as_of)
    out: list[LotPnL] = []
    if snap_now is None:
        return out

    for asset, lots in lots_map.items():
        for lot in lots:
            if lot.remaining <= 1e-9:
                continue
            snap_entry = pick_snapshot_for_day(lot.entry_date)
            if snap_entry is None:
                continue
            r0 = rate_on_snapshot(snap_entry, asset, "selling")
            r1 = rate_on_snapshot(snap_now, asset, "selling")
            if r0 is None or r1 is None:
                continue
            entry_tl = lot.remaining * r0
            current_tl = lot.remaining * r1
            pnl = current_tl - entry_tl
            pct = (pnl / entry_tl * 100.0) if entry_tl > 1e-9 else 0.0
            days = max(0, (as_of - lot.entry_date).days)
            out.append(
                LotPnL(
                    asset_code=asset,
                    amount=lot.remaining,
                    entry_date=lot.entry_date,
                    days_held=days,
                    entry_tl=entry_tl,
                    current_tl=current_tl,
                    pnl_tl=pnl,
                    pnl_pct=pct,
                )
            )
    return out


def _positions_as_of(saves: Sequence, max_day: date) -> dict[str, float]:
    """Latest known quantity per asset from saves with effective_date <= max_day."""
    prev: dict[str, float] = {}
    for row in saves:
        ed = date.fromisoformat(str(row["effective_date"]))
        if ed > max_day:
            break
        sid = int(row["id"])
        lines = db.get_lines_for_save(sid)
        keys = set(prev) | set(lines)
        for a in keys:
            prev[a] = float(lines[a]) if a in lines else prev.get(a, 0.0)
    return {k: v for k, v in prev.items() if v > 1e-9}


def _daily_price_series(asset_code: str) -> list[tuple[date, float]]:
    """One price per calendar day; same-day fetches overwrite with latest."""
    snaps = _snapshots_chrono()
    by_day: dict[date, float] = {}
    for sid, dt in snaps:
        p = rate_on_snapshot(sid, asset_code, "selling")
        if p is not None:
            by_day[dt.date()] = float(p)
    return sorted(by_day.items())


def interpolated_daily_series(
    asset_code: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[date], list[float]]:
    """
    Daily TL value of *total* holding of ``asset_code``, linearly interpolating
    rates by **calendar day** between saved API days; quantity stepped by
    portfolio save effective dates.
    """
    series = _daily_price_series(asset_code)
    if not series:
        return [], []

    t0 = series[0][0]
    if start is None:
        start = t0
    if end is None:
        end = date.today()
    start = max(start, t0)
    end = max(end, start)
    dates_p = [x[0] for x in series]
    prices_a = [x[1] for x in series]

    def price_at(d: date) -> float | None:
        if d <= dates_p[0]:
            return prices_a[0]
        if d >= dates_p[-1]:
            return prices_a[-1]
        idx = bisect_left(dates_p, d)
        if idx <= 0:
            return prices_a[0]
        d_lo, d_hi = dates_p[idx - 1], dates_p[idx]
        p_lo, p_hi = prices_a[idx - 1], prices_a[idx]
        if d_hi == d_lo:
            return p_lo
        span = (d_hi - d_lo).days
        if span <= 0:
            return p_hi
        alpha = (d - d_lo).days / float(span)
        return p_lo + alpha * (p_hi - p_lo)

    saves = db.iter_portfolio_saves_ordered()
    days: list[date] = []
    values: list[float] = []
    d = start
    while d <= end:
        px = price_at(d)
        pos = _positions_as_of(saves, d)
        qty = pos.get(asset_code, 0.0)
        if px is not None:
            days.append(d)
            values.append(qty * float(px))
        d += timedelta(days=1)

    return days, values
