"""CollectAPI client: gold and FX rates in TRY."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

BASE = "https://api.collectapi.com"
TRUNCGIL_URL = "https://finans.truncgil.com/today.json"


@dataclass
class FetchResult:
    fetched_at: datetime
    rates: list[tuple[str, str | None, float, float]]  # code, label, buy, sell


def _header() -> dict[str, str]:
    key = os.environ.get("COLLECTAPI_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "COLLECTAPI_KEY ortam değişkeni tanımlı değil. "
            "CollectAPI hesabınızdan token alıp export COLLECTAPI_KEY='apikey ...' şeklinde ayarlayın."
        )
    if not key.lower().startswith("apikey "):
        key = f"apikey {key}"
    return {
        "authorization": key,
        "content-type": "application/json",
    }


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"[-+]?\d*(?:\.\d+)?", s)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
        return None


def _normalize_gold_name(name: str) -> str | None:
    n = name.lower()
    if "gram" in n:
        return "GOLD_GRAM"
    if "çeyrek" in n or "ceyrek" in n:
        return "GOLD_CEYREK"
    if "yarım" in n or "yarim" in n:
        return "GOLD_YARIM"
    if "tam" in n and "ata" not in n:
        return "GOLD_TAM"
    if "ata" in n and "beşli" not in n and "besli" not in n:
        return "GOLD_ATA"
    if "beşli" in n or "besli" in n:
        return "GOLD_BESLI"
    if "gremse" in n or "gremse" in n:
        return "GOLD_GREMS"
    return None


def _parse_currency_item(item: dict[str, Any]) -> tuple[str, str | None, float, float] | None:
    code = (
        item.get("code")
        or item.get("Currency")
        or item.get("currency")
        or item.get("name")
        or item.get("symbol")
    )
    if not code:
        return None
    code = str(code).strip().upper()
    if len(code) > 6:
        return None
    buy = _to_float(
        item.get("buying")
        or item.get("alis")
        or item.get("Alış")
        or item.get("buy")
        or item.get("forexBuying")
    )
    sell = _to_float(
        item.get("selling")
        or item.get("satis")
        or item.get("Satış")
        or item.get("sell")
        or item.get("forexSelling")
    )
    if buy is None and sell is None:
        return None
    if buy is None:
        buy = sell or 0.0
    if sell is None:
        sell = buy or 0.0
    label = item.get("name") or item.get("text")
    return (code, str(label) if label else None, float(buy), float(sell))


def fetch_gold_price(session: requests.Session | None = None) -> list[tuple[str, str | None, float, float]]:
    sess = session or requests.Session()
    r = sess.get(f"{BASE}/economy/goldPrice", headers=_header(), timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(data.get("message") or "goldPrice başarısız")
    out: list[tuple[str, str | None, float, float]] = []
    for item in data.get("result") or []:
        name = item.get("name") or ""
        code = _normalize_gold_name(str(name))
        if not code:
            continue
        buy = _to_float(item.get("buying"))
        sell = _to_float(item.get("selling"))
        if buy is None and sell is None:
            continue
        if buy is None:
            buy = sell or 0.0
        if sell is None:
            sell = buy or 0.0
        out.append((code, str(name), float(buy), float(sell)))
    return out


def fetch_all_currency(session: requests.Session | None = None) -> list[tuple[str, str | None, float, float]]:
    sess = session or requests.Session()
    paths = ["/economy/allCurrency", "/economy/currencyAll", "/economy/exchange"]
    last_err: Exception | None = None
    for path in paths:
        try:
            r = sess.get(f"{BASE}{path}", headers=_header(), timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                last_err = RuntimeError(data.get("message") or path)
                continue
            raw = data.get("result")
            if not isinstance(raw, list):
                last_err = RuntimeError(f"{path} beklenmeyen gövde")
                continue
            out: list[tuple[str, str | None, float, float]] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                row = _parse_currency_item(item)
                if row:
                    out.append(row)
            if out:
                return out
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError("Döviz uç noktasından veri alınamadı (CollectAPI paketinizde bu uç açık olmayabilir).")


def _parse_truncgil_number(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(".", "").replace(",", ".")
    return _to_float(s)


def fetch_from_truncgil(session: requests.Session | None = None) -> list[tuple[str, str | None, float, float]]:
    """Fallback source if CollectAPI endpoints fail."""
    sess = session or requests.Session()
    r = sess.get(TRUNCGIL_URL, timeout=30)
    r.raise_for_status()
    data = r.json()
    out: list[tuple[str, str | None, float, float]] = []

    # FX
    for code in ("USD", "EUR", "GBP", "CHF", "JPY", "SAR", "AED"):
        item = data.get(code)
        if not isinstance(item, dict):
            continue
        buy = _parse_truncgil_number(item.get("Alış"))
        sell = _parse_truncgil_number(item.get("Satış"))
        if buy is None and sell is None:
            continue
        if buy is None:
            buy = sell or 0.0
        if sell is None:
            sell = buy or 0.0
        out.append((code, code, float(buy), float(sell)))

    # Gold
    gold_map = {
        "Gram Altın": "GOLD_GRAM",
        "Çeyrek Altın": "GOLD_CEYREK",
        "Yarım Altın": "GOLD_YARIM",
        "Tam Altın": "GOLD_TAM",
        "Ata Altın": "GOLD_ATA",
    }
    for key, code in gold_map.items():
        item = data.get(key)
        if not isinstance(item, dict):
            continue
        buy = _parse_truncgil_number(item.get("Alış"))
        sell = _parse_truncgil_number(item.get("Satış"))
        if buy is None and sell is None:
            continue
        if buy is None:
            buy = sell or 0.0
        if sell is None:
            sell = buy or 0.0
        out.append((code, key, float(buy), float(sell)))

    if not out:
        raise RuntimeError("Truncgil fallback verisi parse edilemedi.")
    return out


def fetch_rates() -> FetchResult:
    """Gold + currency merged. Primary source: CollectAPI, fallback: Truncgil."""
    session = requests.Session()
    errors: list[str] = []
    try:
        gold = fetch_gold_price(session)
    except Exception as e:
        gold = []
        errors.append(f"CollectAPI goldPrice hata: {e}")
    try:
        fx = fetch_all_currency(session)
    except Exception as e:
        fx = []
        errors.append(f"CollectAPI döviz hata: {e}")
    by_code: dict[str, tuple[str, str | None, float, float]] = {}
    for row in fx:
        by_code[row[0]] = row
    for row in gold:
        by_code[row[0]] = row
    merged = list(by_code.values())
    if not merged:
        try:
            merged = fetch_from_truncgil(session)
            errors.append("CollectAPI yerine Truncgil fallback kullanıldı.")
        except Exception as e:
            errors.append(f"Fallback hata: {e}")
    if not merged:
        raise RuntimeError("Hiç kur kaydı oluşturulamadı. " + " | ".join(errors))
    return FetchResult(fetched_at=datetime.now(), rates=merged)


KNOWN_ASSET_LABELS: dict[str, str] = {
    "USD": "ABD Doları",
    "EUR": "Euro",
    "GBP": "İngiliz Sterlini",
    "GOLD_GRAM": "Gram Altın",
    "GOLD_CEYREK": "Çeyrek Altın",
    "GOLD_YARIM": "Yarım Altın",
    "GOLD_TAM": "Tam Altın",
    "GOLD_ATA": "Ata Altın",
    "GOLD_BESLI": "Beşli Altın",
    "GOLD_GREMS": "Gremse Altın",
}
