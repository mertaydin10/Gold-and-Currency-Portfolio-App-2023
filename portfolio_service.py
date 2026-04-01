"""FIFO lots from sequential portfolio saves."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import database as db


@dataclass
class Lot:
    asset_code: str
    remaining: float
    original: float
    entry_date: date


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def rebuild_lots() -> dict[str, list[Lot]]:
    """Replay portfolio saves in order; apply deltas with FIFO on decreases."""
    saves = db.iter_portfolio_saves_ordered()
    lots: dict[str, list[Lot]] = {}
    prev_totals: dict[str, float] = {}

    for row in saves:
        sid = int(row["id"])
        lines = db.get_lines_for_save(sid)
        ed = _parse_date(str(row["effective_date"]))
        keys = set(prev_totals) | set(lines)
        for asset in keys:
            new_total = float(lines[asset]) if asset in lines else prev_totals.get(asset, 0.0)
            old = prev_totals.get(asset, 0.0)
            delta = new_total - old
            if abs(delta) < 1e-12:
                prev_totals[asset] = new_total
                continue
            if asset not in lots:
                lots[asset] = []
            if delta > 0:
                lots[asset].append(
                    Lot(
                        asset_code=asset,
                        remaining=delta,
                        original=delta,
                        entry_date=ed,
                    )
                )
            else:
                need = -delta
                q = lots.get(asset, [])
                while need > 1e-9 and q:
                    top = q[0]
                    take = min(top.remaining, need)
                    top.remaining -= take
                    need -= take
                    if top.remaining <= 1e-9:
                        q.pop(0)
            prev_totals[asset] = new_total

    return {k: [l for l in v if l.remaining > 1e-9] for k, v in lots.items()}


def current_totals_from_lots(lots: dict[str, list[Lot]]) -> dict[str, float]:
    return {a: sum(l.remaining for l in ls) for a, ls in lots.items() if ls}


def current_totals_latest_save() -> dict[str, float]:
    summaries = db.list_portfolio_save_summaries()
    if not summaries:
        return {}
    newest_id = summaries[0][0]
    return db.get_lines_for_save(newest_id)


def default_asset_codes() -> list[str]:
    """Preferred UI ordering."""
    return [
        "USD",
        "EUR",
        "GBP",
        "CHF",
        "JPY",
        "SAR",
        "AED",
        "GOLD_GRAM",
        "GOLD_CEYREK",
        "GOLD_YARIM",
        "GOLD_TAM",
        "GOLD_ATA",
        "GOLD_BESLI",
        "GOLD_GREMS",
    ]


def merge_with_defaults(quantities: dict[str, float]) -> dict[str, float]:
    out = {c: 0.0 for c in default_asset_codes()}
    for k, v in quantities.items():
        out[k] = float(v)
    return out
