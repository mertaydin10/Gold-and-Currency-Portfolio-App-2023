"""SQLite persistence: API rate snapshots and portfolio save history."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Generator, Iterable, Sequence

DB_PATH = Path(__file__).resolve().parent / "varlik.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL REFERENCES price_snapshots(id) ON DELETE CASCADE,
                asset_code TEXT NOT NULL,
                label TEXT,
                buying REAL,
                selling REAL,
                UNIQUE(snapshot_id, asset_code)
            );

            CREATE TABLE IF NOT EXISTS portfolio_saves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                effective_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS portfolio_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                save_id INTEGER NOT NULL REFERENCES portfolio_saves(id) ON DELETE CASCADE,
                asset_code TEXT NOT NULL,
                quantity REAL NOT NULL,
                UNIQUE(save_id, asset_code)
            );

            CREATE INDEX IF NOT EXISTS idx_portfolio_saves_order
                ON portfolio_saves(effective_date, created_at);

            CREATE INDEX IF NOT EXISTS idx_rates_snapshot
                ON rates(snapshot_id, asset_code);
            """
        )


def insert_price_snapshot(
    fetched_at: datetime, rates: Sequence[tuple[str, str | None, float, float]]
) -> int:
    """Insert snapshot and rates. rates: (asset_code, label, buying, selling)."""
    iso = fetched_at.isoformat(timespec="microseconds")
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO price_snapshots(fetched_at) VALUES (?)", (iso,)
        )
        sid = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO rates(snapshot_id, asset_code, label, buying, selling)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(sid, code, lab, b, s) for code, lab, b, s in rates],
        )
        return sid


def list_price_snapshot_times() -> list[tuple[int, str]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, fetched_at FROM price_snapshots ORDER BY fetched_at DESC"
        ).fetchall()
    return [(int(r["id"]), str(r["fetched_at"])) for r in rows]


def get_rates_for_snapshot(snapshot_id: int) -> dict[str, dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT asset_code, label, buying, selling
            FROM rates WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
    return {
        str(r["asset_code"]): {
            "label": r["label"],
            "buying": float(r["buying"]),
            "selling": float(r["selling"]),
        }
        for r in rows
    }


def get_latest_snapshot_id() -> int | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM price_snapshots ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
    return int(row["id"]) if row else None


def get_snapshot_fetched_at(snapshot_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT fetched_at FROM price_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
    return str(row["fetched_at"]) if row else None


def insert_portfolio_save(
    effective_date: date,
    lines: Iterable[tuple[str, float]],
    created_at: datetime | None = None,
) -> int:
    created = (created_at or datetime.now()).isoformat(timespec="seconds")
    ed = effective_date.isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO portfolio_saves(effective_date, created_at) VALUES (?, ?)",
            (ed, created),
        )
        sid = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO portfolio_lines(save_id, asset_code, quantity)
            VALUES (?, ?, ?)
            """,
            [(sid, code, float(qty)) for code, qty in lines],
        )
        return sid


def iter_portfolio_saves_ordered() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return list(
            conn.execute(
                """
                SELECT id, effective_date, created_at
                FROM portfolio_saves
                ORDER BY effective_date ASC, created_at ASC, id ASC
                """
            ).fetchall()
        )


def get_lines_for_save(save_id: int) -> dict[str, float]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT asset_code, quantity FROM portfolio_lines WHERE save_id = ?",
            (save_id,),
        ).fetchall()
    return {str(r["asset_code"]): float(r["quantity"]) for r in rows}


def list_portfolio_save_summaries() -> list[tuple[int, str, str]]:
    """(save_id, effective_date, created_at) newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, effective_date, created_at
            FROM portfolio_saves
            ORDER BY effective_date DESC, created_at DESC, id DESC
            """
        ).fetchall()
    return [(int(r["id"]), str(r["effective_date"]), str(r["created_at"])) for r in rows]
