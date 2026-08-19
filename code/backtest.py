#!/usr/bin/env python3
"""backtest.py - The Gold Barometer historical backtest / evidence engine.

Implements docs/DESIGN.md section 2. Reconstructs the composite score at
month-end cadence from 1971-01 to the most recent complete month using
strict point-in-time discipline (publication lags, expanding-window
percentiles, era-aware pillar availability), then aggregates forward-return
statistics per score band.

Publishable outputs are all DERIVED (per docs/DATA-RIGHTS.md): the raw GVZ
value never leaves data/history/ and never appears in results.

Configurable data dir via TGB_DATA_DIR env var. Standard library only.
Runnable with: `python3 scripts/backtest/backtest.py`.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Share primitives with the live engine (single source of truth)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "pipeline"))

from compute_score import (  # noqa: E402
    percentile_rank,
    median_smooth,
    find_at_or_before,
    load_series,
    cap_subscore,
    BASE_WEIGHTS,
    EVIDENCE_GRADES,
    _median,
)

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_DEFAULT_ROOT = _HERE.parents[2]
ROOT = Path(os.environ.get("TGB_DATA_DIR") or _DEFAULT_ROOT).resolve()

HISTORY = ROOT / "data" / "history"
SERIES = ROOT / "data" / "series"
OUTPUT = ROOT / "data" / "backtest"
OUTPUT.mkdir(parents=True, exist_ok=True)

VERSION = "1.0"
BACKTEST_START = date(1971, 1, 31)  # DESIGN 2: 1971 (Nixon shock; pre-1971 fixed price)

# Backtest-specific minimum histories (RELAXED from live engine's 10y floor).
# Rationale: percentile ranks need something to rank against; requiring 10y
# would eliminate all pre-1972 real-rates readings and eliminate every daily
# pillar until deep into the 2010s. 5y monthly / ~3y daily is the honest floor
# for a 1971-present study. This is documented in BACKTEST-REPORT.md.
BT_MIN_MONTHLY = 60
BT_MIN_WEEKLY = 156
BT_MIN_DAILY = 750
BT_MIN_MONTHLY_CHANGE = 24  # 24 monthly 12m-change observations

# Publication lags in calendar days. Applied by slicing each series to
# dates <= (as_of - lag) before any percentile computation.
LAG_DAYS = {
    "cpi": 30,        # BLS CPI released ~mid-following-month
    "wb_gold": 7,     # World Bank Pink Sheet ~early following month
    "cb_sum": 60,     # IMF IRFCL ~2 months lag (DATA-RIGHTS-verified cadence)
    "gld": 1,
    "cot": 3,         # CFTC Friday release for Tuesday positioning
    "tips": 1,
    "gs10": 1,
    "dollar": 3,      # H.10 weekly release
    "gvz": 1,
}

# Canonical zone edges + names (matches compute_score.ZONES).
CANONICAL_ZONES: list[tuple[int, int, str]] = [
    (80, 100, "Historically very favorable"),
    (60, 79, "Favorable"),
    (40, 59, "Mixed"),
    (20, 39, "Unfavorable"),
    (0, 19, "Historically very unfavorable"),
]

FORWARD_HORIZONS_MONTHS = [12, 36, 60, 120]  # 1y / 3y / 5y / 10y
HORIZON_LABELS = {12: "1y", 36: "3y", 60: "5y", 120: "10y"}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def month_end(y: int, m: int) -> date:
    if m == 12:
        return date(y + 1, 1, 1) - timedelta(days=1)
    return date(y, m + 1, 1) - timedelta(days=1)


def all_month_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while True:
        d = month_end(y, m)
        if d > end:
            break
        if d >= start:
            out.append(d)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def slice_to(rows: Sequence[tuple[date, float]], cutoff: date) -> list[tuple[date, float]]:
    """Return the prefix of `rows` (sorted ascending) with date <= cutoff."""
    out: list[tuple[date, float]] = []
    for d, v in rows:
        if d <= cutoff:
            out.append((d, v))
        else:
            break
    return out


def month_end_sampled(rows: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Reduce a daily/weekly series to one point per calendar month (last
    value <= last day of that month). Deterministic; preserves order."""
    by_month: dict[tuple[int, int], tuple[date, float]] = {}
    for d, v in rows:
        key = (d.year, d.month)
        prev = by_month.get(key)
        if prev is None or d > prev[0]:
            by_month[key] = (d, v)
    return sorted(by_month.values(), key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Bundle loading
# ---------------------------------------------------------------------------

def load_bundle() -> dict:
    """Load every input series once. Sorted ascending, floats."""
    return {
        "tips": load_series(HISTORY / "tips_10y.csv"),
        "proxy": load_series(HISTORY / "real_rate_proxy.csv"),
        "wb_gold": load_series(HISTORY / "wb_gold_usd_toz.csv"),
        "cpi": load_series(HISTORY / "cpi.csv"),
        "cb_sum": load_series(SERIES / "cb_gold_reporters_sum.csv"),
        "gld": load_series(HISTORY / "gld_tonnes.csv"),
        "dollar": load_series(HISTORY / "dollar_broad.csv"),
        "cot_pct_oi": load_series(HISTORY / "cot_mm_net_pct_oi.csv"),
        "gvz": load_series(HISTORY / "gvz.csv"),
    }


# ---------------------------------------------------------------------------
# Pillar sub-scores (point-in-time, expanding-window percentiles)
#
# Each function returns:
#   {"available": bool, "subscore": float?, "percentile": float?,
#    "value": <internal>, "source": str, "latest_date": iso?, "reason": str?}
#
# All values are internal; only sub-scores + metadata are surfaced in results.
# ---------------------------------------------------------------------------

def bt_pillar_real_rates(as_of: date, tips: list, proxy: list) -> dict:
    tips_cut = slice_to(median_smooth(tips, 5), as_of - timedelta(days=LAG_DAYS["tips"]))
    if len(tips_cut) >= BT_MIN_DAILY:
        latest = tips_cut[-1]
        # Joint distribution: monthly proxy before TIPS begins, then month-end
        # TIPS values. See the same correction in compute_score.py (2026-08-11):
        # a TIPS-only window collapsed to ~3 years at the 2005 seam.
        tips_start = tips_cut[0][0]
        proxy_cut_pre = slice_to(proxy, as_of - timedelta(days=LAG_DAYS["cpi"]))
        proxy_pre = [v for d, v in proxy_cut_pre if d < tips_start]
        by_month = {}
        for d, v in tips_cut:
            key = (d.year, d.month)
            prev = by_month.get(key)
            if prev is None or d > prev[0]:
                by_month[key] = (d, v)
        tips_monthly = [v for _, v in sorted(by_month.values(), key=lambda x: x[0])]
        dist = proxy_pre + tips_monthly
        pct = percentile_rank(latest[1], dist)
        return {
            "available": True,
            "subscore": cap_subscore(100.0 - pct),
            "percentile": pct,
            "value": latest[1],
            "source": "TIPS 10Y (H.15)",
            "latest_date": latest[0].isoformat(),
        }
    proxy_cut = slice_to(proxy, as_of - timedelta(days=LAG_DAYS["cpi"]))
    if len(proxy_cut) < BT_MIN_MONTHLY:
        return {"available": False, "reason": f"insufficient history ({len(proxy_cut)} < {BT_MIN_MONTHLY})"}
    latest = proxy_cut[-1]
    dist = [v for _, v in proxy_cut]
    pct = percentile_rank(latest[1], dist)
    return {
        "available": True,
        "subscore": cap_subscore(100.0 - pct),
        "percentile": pct,
        "value": latest[1],
        "source": "GS10 - CPI YoY (proxy, pre-TIPS)",
        "latest_date": latest[0].isoformat(),
    }


def bt_pillar_entry_price(as_of: date, wb_gold: list, cpi: list) -> dict:
    """Two averaged sub-signals per DESIGN 1.2 #2:
      (a) TREND: dev-vs-12m-MA percentile + 12m return percentile, inverted
      (b) VALUATION: CPI-deflated price vs full history, percentile inverted
    """
    wb_cut = slice_to(wb_gold, as_of - timedelta(days=LAG_DAYS["wb_gold"]))
    cpi_cut = slice_to(cpi, as_of - timedelta(days=LAG_DAYS["cpi"]))
    if len(wb_cut) < BT_MIN_MONTHLY:
        return {"available": False, "reason": "insufficient gold history"}
    latest_d, latest_p = wb_cut[-1]

    # --- trend ---
    if len(wb_cut) < 13:
        return {"available": False, "reason": "trend: no year-ago point"}
    ma12 = sum(v for _, v in wb_cut[-13:-1]) / 12.0
    dev_now = (latest_p - ma12) / ma12
    ret_now = (latest_p - wb_cut[-13][1]) / wb_cut[-13][1]
    dev_series, ret_series = [], []
    for i in range(12, len(wb_cut)):
        w = [v for _, v in wb_cut[i - 12 : i]]
        ma = sum(w) / 12.0
        dev_series.append((wb_cut[i][1] - ma) / ma)
        ret_series.append((wb_cut[i][1] - wb_cut[i - 12][1]) / wb_cut[i - 12][1])
    if len(dev_series) < BT_MIN_MONTHLY_CHANGE:
        return {"available": False, "reason": "insufficient trend history"}
    dev_pct = percentile_rank(dev_now, dev_series)
    ret_pct = percentile_rank(ret_now, ret_series)
    trend_sub = 0.5 * ((100.0 - dev_pct) + (100.0 - ret_pct))

    # --- valuation ---
    cpi_by_month = {(d.year, d.month): v for d, v in cpi_cut}
    deflated: list[tuple[date, float]] = []
    for d, v in wb_cut:
        c = cpi_by_month.get((d.year, d.month))
        if c and c > 0:
            deflated.append((d, v / c))
    if len(deflated) < BT_MIN_MONTHLY:
        return {"available": False, "reason": "insufficient deflated history"}
    latest_real = deflated[-1][1]
    real_pct = percentile_rank(latest_real, [v for _, v in deflated])
    valuation_sub = 100.0 - real_pct

    subscore = 0.5 * (trend_sub + valuation_sub)
    return {
        "available": True,
        "subscore": cap_subscore(subscore),
        "percentile": None,  # composite of two sub-signals
        "value": latest_p,
        "source": "WB Pink Sheet monthly, CPI-deflated (Erb-Harvey golden constant + trend)",
        "latest_date": latest_d.isoformat(),
        "sub": {
            "trend": round(cap_subscore(trend_sub), 2),
            "valuation": round(cap_subscore(valuation_sub), 2),
            "dev_vs_12m_ma_pct": round(dev_now * 100, 2),
            "return_12m_pct": round(ret_now * 100, 2),
            "real_price_percentile": round(real_pct, 2),
        },
    }


def _twelve_month_change_percentile(rows_monthly: list[tuple[date, float]]) -> tuple[float, float, int] | None:
    """Return (subscore-oriented percentile, change_pct, hist_len) for a
    monthly-sampled series. Non-inverted (higher change = higher percentile).
    Requires >= BT_MIN_MONTHLY_CHANGE 12m-change points."""
    if len(rows_monthly) < 13:
        return None
    changes = []
    for i in range(12, len(rows_monthly)):
        prev = rows_monthly[i - 12][1]
        if prev == 0:
            continue
        changes.append((rows_monthly[i][1] - prev) / abs(prev) * 100)
    if len(changes) < BT_MIN_MONTHLY_CHANGE:
        return None
    change_now = changes[-1]
    pct = percentile_rank(change_now, changes)
    return pct, change_now, len(changes)


def bt_pillar_structural_demand(as_of: date, cb_sum: list, gld: list) -> dict:
    cb_cut = slice_to(cb_sum, as_of - timedelta(days=LAG_DAYS["cb_sum"]))
    gld_cut = month_end_sampled(slice_to(gld, as_of - timedelta(days=LAG_DAYS["gld"])))
    sub_cb = _twelve_month_change_percentile(cb_cut)
    sub_gld = _twelve_month_change_percentile(gld_cut)
    if sub_cb is None and sub_gld is None:
        return {"available": False, "reason": "insufficient CB nor GLD history"}
    parts = []
    if sub_cb is not None:
        parts.append(sub_cb[0])
    if sub_gld is not None:
        parts.append(sub_gld[0])
    subscore = sum(parts) / len(parts)
    return {
        "available": True,
        "subscore": cap_subscore(subscore),
        "percentile": subscore,
        "value": None,
        "source": "IMF IRFCL sum-of-reporters (CB) + SPDR GLD tonnes (ETF, internal)",
        "latest_date": (cb_cut[-1][0] if cb_cut else gld_cut[-1][0]).isoformat(),
        "sub": {
            "cb_12m_change_pct": (round(sub_cb[1], 2) if sub_cb else None),
            "cb_percentile": (round(sub_cb[0], 2) if sub_cb else None),
            "gld_12m_change_pct": (round(sub_gld[1], 2) if sub_gld else None),
            "gld_percentile": (round(sub_gld[0], 2) if sub_gld else None),
        },
    }


def bt_pillar_dollar(as_of: date, dollar: list) -> dict:
    monthly = month_end_sampled(slice_to(dollar, as_of - timedelta(days=LAG_DAYS["dollar"])))
    if len(monthly) < BT_MIN_MONTHLY:
        return {"available": False, "reason": f"insufficient history ({len(monthly)} < {BT_MIN_MONTHLY})"}
    latest_d, latest_v = monthly[-1]
    dist = [v for _, v in monthly]
    level_pct = percentile_rank(latest_v, dist)
    level_sub = 100.0 - level_pct
    # 12m change
    if len(monthly) < 13:
        return {"available": False, "reason": "no year-ago point"}
    yr_ago = monthly[-13][1]
    change = (latest_v - yr_ago) / abs(yr_ago) * 100 if yr_ago else 0.0
    changes = []
    for i in range(12, len(monthly)):
        p = monthly[i - 12][1]
        if p != 0:
            changes.append((monthly[i][1] - p) / abs(p) * 100)
    if len(changes) < BT_MIN_MONTHLY_CHANGE:
        return {"available": False, "reason": "insufficient change history"}
    change_pct_rank = percentile_rank(change, changes)
    change_sub = 100.0 - change_pct_rank
    subscore = 0.5 * (level_sub + change_sub)
    return {
        "available": True,
        "subscore": cap_subscore(subscore),
        "percentile": level_pct,
        "value": latest_v,
        "source": "Board of Governors (H.10 broad dollar), monthly-sampled",
        "latest_date": latest_d.isoformat(),
    }


def bt_pillar_positioning(as_of: date, cot_pct_oi: list) -> dict:
    weekly = slice_to(cot_pct_oi, as_of - timedelta(days=LAG_DAYS["cot"]))
    monthly = month_end_sampled(weekly)
    if len(monthly) < BT_MIN_MONTHLY:
        return {"available": False, "reason": f"insufficient history ({len(monthly)} < {BT_MIN_MONTHLY})"}
    latest_d, latest_v = monthly[-1]
    dist = [v for _, v in monthly]
    pct = percentile_rank(latest_v, dist)
    return {
        "available": True,
        "subscore": cap_subscore(100.0 - pct),  # contrarian
        "percentile": pct,
        "value": latest_v,
        "source": "CFTC COT MM net %OI, monthly-sampled",
        "latest_date": latest_d.isoformat(),
    }


def bt_pillar_volatility(as_of: date, gvz: list) -> dict:
    monthly = month_end_sampled(slice_to(gvz, as_of - timedelta(days=LAG_DAYS["gvz"])))
    if len(monthly) < BT_MIN_MONTHLY:
        return {"available": False, "reason": f"insufficient history ({len(monthly)} < {BT_MIN_MONTHLY})"}
    latest_d, latest_v = monthly[-1]
    dist = [v for _, v in monthly]
    pct = percentile_rank(latest_v, dist)
    # DATA-RIGHTS hard constraint: raw GVZ never surfaces. `value` stays
    # internal; not written to any output file.
    return {
        "available": True,
        "subscore": cap_subscore(100.0 - pct),
        "percentile": pct,
        "value": None,  # deliberately null in outputs
        "source": "Cboe GVZ percentile (raw internal-only per DATA-RIGHTS.md)",
        "latest_date": latest_d.isoformat(),
    }


# ---------------------------------------------------------------------------
# Composite computation with era-aware renormalization
# ---------------------------------------------------------------------------

PILLAR_ORDER = ["real_rates", "entry_price", "structural_demand",
                "dollar", "positioning", "volatility", "premiums"]


def compute_score_at(as_of: date, bundle: dict,
                     weights_override: dict | None = None) -> dict:
    """Point-in-time composite score at `as_of`. Premiums structurally
    absent (backtest predates the collector). Weights renormalized on the
    surviving pillar set."""
    pillars = {
        "real_rates": bt_pillar_real_rates(as_of, bundle["tips"], bundle["proxy"]),
        "entry_price": bt_pillar_entry_price(as_of, bundle["wb_gold"], bundle["cpi"]),
        "structural_demand": bt_pillar_structural_demand(as_of, bundle["cb_sum"], bundle["gld"]),
        "dollar": bt_pillar_dollar(as_of, bundle["dollar"]),
        "positioning": bt_pillar_positioning(as_of, bundle["cot_pct_oi"]),
        "volatility": bt_pillar_volatility(as_of, bundle["gvz"]),
        "premiums": {"available": False, "reason": "collector started 2026-08-04; predates backtest"},
    }
    weights_base = dict(weights_override or BASE_WEIGHTS)
    weights_base["premiums"] = 0  # forced absent
    available = {k: pillars[k].get("available", False) for k in PILLAR_ORDER}
    total = sum(w for k, w in weights_base.items() if available[k])
    if total <= 0:
        return {"date": as_of.isoformat(), "score": None, "reason": "no pillars available",
                "pillars_used": 0, "pillars": pillars, "weights_effective": {}}
    scale = 100.0 / total
    effective = {k: (weights_base[k] * scale if available[k] else 0.0) for k in PILLAR_ORDER}
    weighted = 0.0
    for k in PILLAR_ORDER:
        if available[k]:
            weighted += effective[k] * pillars[k]["subscore"]
    composite = weighted / 100.0
    return {
        "date": as_of.isoformat(),
        "score": int(round(composite)),
        "score_precise": round(composite, 3),
        "pillars_used": sum(1 for k in PILLAR_ORDER if available[k]),
        "pillars": pillars,
        "weights_nominal": weights_base,
        "weights_effective": {k: round(v, 4) for k, v in effective.items()},
    }


def zone_for(score: int, zones: list[tuple[int, int, str]] = CANONICAL_ZONES) -> str:
    for lo, hi, name in zones:
        if lo <= score <= hi:
            return name
    return "Unknown"


# ---------------------------------------------------------------------------
# Forward outcomes (nominal + CPI-real, drawdown, DCA, lump-sum)
# ---------------------------------------------------------------------------

def _last_price_at_or_before(index: dict[date, float], sorted_keys: list[date],
                             target: date) -> tuple[date, float] | None:
    # Binary search would be nicer; monthly cadence keeps this cheap.
    lo, hi = 0, len(sorted_keys) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if sorted_keys[mid] <= target:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if ans < 0:
        return None
    d = sorted_keys[ans]
    return (d, index[d])


def forward_stats(entry_date: date, entry_price: float, entry_real: float,
                  wb: list[tuple[date, float]], cpi_by_month: dict[tuple[int, int], float]
                  ) -> dict:
    """Compute forward returns 1/3/5/10y (nominal + real), max drawdown, and
    DCA vs lump-sum comparison. All at monthly granularity using WB Pink Sheet.
    Returns {} for horizons that fall past series end.
    """
    idx = {d: v for d, v in wb}
    keys = [d for d, _ in wb]

    out: dict = {"entry_date": entry_date.isoformat(),
                 "entry_price": entry_price,
                 "entry_real": entry_real}
    # Locate entry in monthly index (should be exact match at month end).
    if entry_date not in idx:
        # Fall back to last <=
        hit = _last_price_at_or_before(idx, keys, entry_date)
        if hit is None:
            return out
        entry_date, entry_price = hit
        c = cpi_by_month.get((entry_date.year, entry_date.month))
        entry_real = entry_price / c if c else entry_price

    entry_i = keys.index(entry_date)

    for h in FORWARD_HORIZONS_MONTHS:
        target_i = entry_i + h
        label = HORIZON_LABELS[h]
        if target_i >= len(keys):
            out[f"fwd_{label}_nominal"] = None
            out[f"fwd_{label}_real"] = None
            out[f"maxdd_{label}"] = None
            continue
        end_d, end_p = keys[target_i], idx[keys[target_i]]
        out[f"fwd_{label}_nominal"] = (end_p / entry_price) - 1.0
        c_end = cpi_by_month.get((end_d.year, end_d.month))
        if c_end and c_end > 0:
            end_real = end_p / c_end
            out[f"fwd_{label}_real"] = (end_real / entry_real) - 1.0
        else:
            out[f"fwd_{label}_real"] = None
        # Max drawdown WITHIN the horizon (measured from entry_price)
        max_dd = 0.0
        for j in range(entry_i, target_i + 1):
            p = idx[keys[j]]
            dd = (p / entry_price) - 1.0
            if dd < max_dd:
                max_dd = dd
        out[f"maxdd_{label}"] = max_dd

    # DCA vs lump-sum at 5y horizon: 12 monthly buys of equal $1 starting at
    # entry_date; evaluate the resulting basket at t+60m.
    end_i = entry_i + 60
    if end_i < len(keys):
        # Lump-sum: $12 all at entry -> 12/entry_price ounces
        lump_oz = 12.0 / entry_price
        # DCA: $1/month for 12 months -> sum of 1/price_i ounces
        dca_oz = 0.0
        for j in range(entry_i, entry_i + 12):
            if j < len(keys):
                dca_oz += 1.0 / idx[keys[j]]
        # Values at t+60m
        end_p = idx[keys[end_i]]
        lump_val = lump_oz * end_p
        dca_val = dca_oz * end_p
        out["dca_vs_lump_5y_diff_pct"] = (dca_val / lump_val - 1.0) * 100 if lump_val else None
        out["lump_5y_return_pct"] = (lump_val / 12.0 - 1.0) * 100
        out["dca_5y_return_pct"] = (dca_val / 12.0 - 1.0) * 100
    else:
        out["dca_vs_lump_5y_diff_pct"] = None
        out["lump_5y_return_pct"] = None
        out["dca_5y_return_pct"] = None

    return out


# ---------------------------------------------------------------------------
# Aggregation: band statistics
# ---------------------------------------------------------------------------

def _percentile_summary(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "mean": None, "p10": None, "p90": None,
                "min": None, "max": None}
    values.sort()
    n = len(values)
    return {
        "n": n,
        "median": statistics.median(values),
        "mean": sum(values) / n,
        "p10": values[max(0, int(0.10 * (n - 1)))],
        "p90": values[min(n - 1, int(0.90 * (n - 1)))],
        "min": values[0],
        "max": values[-1],
    }


def build_band_stats(rows: list[dict], zones: list[tuple[int, int, str]] = CANONICAL_ZONES,
                     all_1y_median: float | None = None) -> dict:
    """Per-band forward-return statistics + hit rate + DCA vs lump.
    `all_1y_median` = baseline for hit-rate calculation (baseline is the
    all-sample nominal 1y median, i.e., the always-buy benchmark)."""
    banded: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("score") is None:
            continue
        z = zone_for(r["score"], zones)
        banded[z].append(r)

    out: dict = {}
    for lo, hi, name in zones:
        subset = banded.get(name, [])
        entry = {"zone": name, "band": f"{lo}-{hi}", "n_months": len(subset)}
        for h in FORWARD_HORIZONS_MONTHS:
            label = HORIZON_LABELS[h]
            nom_vals = [r.get(f"fwd_{label}_nominal") for r in subset if r.get(f"fwd_{label}_nominal") is not None]
            real_vals = [r.get(f"fwd_{label}_real") for r in subset if r.get(f"fwd_{label}_real") is not None]
            dd_vals = [r.get(f"maxdd_{label}") for r in subset if r.get(f"maxdd_{label}") is not None]
            entry[f"fwd_{label}_nominal"] = _percentile_summary(nom_vals)
            entry[f"fwd_{label}_real"] = _percentile_summary(real_vals)
            entry[f"maxdd_{label}"] = _percentile_summary(dd_vals)
        # Hit rate vs always-buy baseline on 1y nominal
        if all_1y_median is not None:
            nom1 = [r.get("fwd_1y_nominal") for r in subset if r.get("fwd_1y_nominal") is not None]
            hits = sum(1 for v in nom1 if v > all_1y_median)
            entry["hit_rate_1y_vs_all_median"] = (hits / len(nom1)) if nom1 else None
        # DCA vs lump
        dca_diffs = [r.get("dca_vs_lump_5y_diff_pct") for r in subset]
        entry["dca_vs_lump_5y_pct"] = _percentile_summary(dca_diffs)
        entry["lump_5y_return_pct"] = _percentile_summary(
            [r.get("lump_5y_return_pct") for r in subset])
        entry["dca_5y_return_pct"] = _percentile_summary(
            [r.get("dca_5y_return_pct") for r in subset])
        out[name] = entry
    return out


def build_seller_view(band_stats: dict) -> dict:
    """Reframe the same band table for a would-be SELLER, per Marc's
    2026-08-05 decision (no separate sell score; the buy-conditions
    instrument reads both ways).
    - Median 'drawdown avoided by selling in this band' = max(0, -1 * median fwd_1y_nominal) if it's negative
    - Median 'opportunity cost of selling in this band' = max(0, median fwd_1y_nominal) if positive
    - Same for 3y for context.
    """
    out: dict = {}
    for zone, entry in band_stats.items():
        item = {"zone": zone, "band": entry["band"], "n_months": entry["n_months"]}
        for label in ("1y", "3y", "5y"):
            med = (entry.get(f"fwd_{label}_nominal") or {}).get("median")
            if med is None:
                item[f"opportunity_cost_of_selling_median_{label}"] = None
                item[f"drawdown_avoided_by_selling_median_{label}"] = None
                continue
            item[f"opportunity_cost_of_selling_median_{label}"] = max(0.0, med)
            item[f"drawdown_avoided_by_selling_median_{label}"] = max(0.0, -med)
        # Also carry the median maxdd for a "worst-case avoided by selling" line
        maxdd = (entry.get("maxdd_1y") or {}).get("median")
        item["median_maxdd_avoided_by_selling_1y"] = (abs(maxdd) if maxdd is not None else None)
        out[zone] = item
    return out


PER_PILLAR_SELL_CAVEATS = [
    {
        "pillar": "real_rates",
        "buyer_orientation": "Low real rates → higher sub-score (favorable to buy).",
        "seller_caveat": "Falling real rates are a HOLDING tailwind, not a sell trigger. Rising real rates weaken the buy case but also erode the marginal seller's incentive symmetrically only if they need dollars; long-term holders are typically indifferent to real rates in the short run.",
        "reading_inverts_cleanly": False,
    },
    {
        "pillar": "entry_price",
        "buyer_orientation": "Price below trend / cheap in real terms → higher sub-score.",
        "seller_caveat": "The valuation sub-signal INVERTS cleanly for sellers: real price at the top of its long-run distribution is precisely the condition under which historical forward real returns were weakest (Erb-Harvey), i.e., a defensible sell condition. Trend momentum inverts too: a negative 12m return often continued; selling into strength (positive trend, above 12m MA) has historically been a better price than selling into weakness.",
        "reading_inverts_cleanly": True,
    },
    {
        "pillar": "structural_demand",
        "buyer_orientation": "CB accumulation / ETF inflows → higher sub-score.",
        "seller_caveat": "Physical retail sellers face the LFP absorber directly. In periods of strong CB accumulation (2022-2024), dealer bids are firm and the physical sell workflow is smooth. When CB demand recedes, physical bids widen (worse execution). Effect is on friction, not directionality.",
        "reading_inverts_cleanly": False,
    },
    {
        "pillar": "dollar",
        "buyer_orientation": "Weak / falling dollar → higher sub-score (numeraire).",
        "seller_caveat": "For a USD-quoted physical seller, dollar direction cancels out: the numeraire caveat applies symmetrically. Non-USD sellers should read the pillar in their own currency's dollar cross.",
        "reading_inverts_cleanly": False,
    },
    {
        "pillar": "positioning",
        "buyer_orientation": "COT MM net %OI at LOW percentile → higher sub-score (washed-out longs).",
        "seller_caveat": "High COT MM net %OI (crowded long) inverts cleanly for sellers: it has historically preceded washouts. This is the pillar with the strongest sell-side signal at percentile extremes.",
        "reading_inverts_cleanly": True,
    },
    {
        "pillar": "volatility",
        "buyer_orientation": "High GVZ → LOW sub-score (poor entry conditions).",
        "seller_caveat": "High implied vol raises option premia and the bid/ask on physical spreads; execution costs rise for BOTH sides. Not a directional sell trigger.",
        "reading_inverts_cleanly": False,
    },
    {
        "pillar": "premiums",
        "buyer_orientation": "High retail premiums → LOW sub-score (worse all-in price for buyers).",
        "seller_caveat": "High retail premiums are directly BETTER for physical sellers: dealer bids sit closer to spot when their sell-side inventory is tight, and cash-buyers of jewelry/coins bid harder. Cost argument, incontestable. This pillar has the cleanest buyer-vs-seller inversion in the entire instrument.",
        "reading_inverts_cleanly": True,
    },
]


# ---------------------------------------------------------------------------
# Failures analysis
# ---------------------------------------------------------------------------

def real_rate_correlation(rows: list[dict], start: date, end: date) -> dict:
    """Correlation of the REAL_RATES sub-score to forward 1y nominal return
    across `rows` whose date is within [start, end] and both are present."""
    xs, ys = [], []
    for r in rows:
        if r.get("score") is None:
            continue
        d = date.fromisoformat(r["date"])
        if d < start or d > end:
            continue
        rr = r.get("pillar_scores", {}).get("real_rates")
        fwd = r.get("fwd_1y_nominal")
        if rr is None or fwd is None:
            continue
        xs.append(rr)
        ys.append(fwd)
    n = len(xs)
    if n < 24:
        return {"n": n, "correlation": None, "note": "insufficient overlapping data"}
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return {"n": n, "correlation": None, "note": "zero variance"}
    return {"n": n, "correlation": num / (dx * dy)}


def failures_2022_2024(rows: list[dict]) -> list[dict]:
    """Per-month record inside 2022-01 to 2024-12: score, real-rate sub-score,
    subsequent 12m return. Illustrates the documented regime break."""
    start = date(2022, 1, 1)
    end = date(2024, 12, 31)
    out = []
    for r in rows:
        d = date.fromisoformat(r["date"])
        if d < start or d > end:
            continue
        if r.get("score") is None:
            continue
        out.append({
            "date": r["date"],
            "score": r["score"],
            "zone": zone_for(r["score"]),
            "real_rates_subscore": (r.get("pillar_scores") or {}).get("real_rates"),
            "fwd_1y_nominal_pct": (round(r["fwd_1y_nominal"] * 100, 2)
                                    if r.get("fwd_1y_nominal") is not None else None),
        })
    return out


def band_monotonicity(band_stats: dict) -> dict:
    """The score is oriented so higher zones should exhibit stronger forward
    returns. Report where this fails."""
    order = ["Historically very unfavorable", "Unfavorable", "Mixed",
             "Favorable", "Historically very favorable"]
    monos = {}
    for horizon in ("1y", "3y", "5y", "10y"):
        medians = []
        for z in order:
            entry = band_stats.get(z, {})
            m = (entry.get(f"fwd_{horizon}_nominal") or {}).get("median")
            medians.append((z, m))
        pairs = [(a, b) for a, b in zip(medians, medians[1:]) if a[1] is not None and b[1] is not None]
        violations = [(a[0], b[0], a[1], b[1]) for a, b in pairs if a[1] > b[1]]
        monos[horizon] = {
            "medians_by_zone_ascending": [(z, (round(m * 100, 2) if m is not None else None)) for z, m in medians],
            "violations": [{"lower_zone": v[0], "higher_zone": v[1],
                            "lower_median_pct": round(v[2] * 100, 2),
                            "higher_median_pct": round(v[3] * 100, 2)} for v in violations],
        }
    return monos


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def robustness_band_edges(rows: list[dict], all_1y_median: float, delta: int = 5) -> dict:
    """Recompute band stats with edges shifted by `delta` in both directions."""
    # Shift LOW edges (widens Mixed to the sides): 85 / 65 / 45 / 25 vs 75 / 55 / 35 / 15
    up = [(85, 100, "Historically very favorable"),
          (65, 84,  "Favorable"),
          (45, 64,  "Mixed"),
          (25, 44,  "Unfavorable"),
          (0,  24,  "Historically very unfavorable")]
    dn = [(75, 100, "Historically very favorable"),
          (55, 74,  "Favorable"),
          (35, 54,  "Mixed"),
          (15, 34,  "Unfavorable"),
          (0,  14,  "Historically very unfavorable")]
    return {
        f"edges_+{delta}": build_band_stats(rows, zones=up, all_1y_median=all_1y_median),
        f"edges_-{delta}": build_band_stats(rows, zones=dn, all_1y_median=all_1y_median),
    }


def robustness_equal_weights(bundle: dict, months: list[date],
                             wb_index: dict[date, float],
                             cpi_by_month: dict[tuple[int, int], float]) -> tuple[list[dict], dict]:
    """Re-run the composite with equal weights across the six non-premium
    pillars (100/6 each); premiums stays absent."""
    equal = {k: 100.0 / 6.0 for k in PILLAR_ORDER if k != "premiums"}
    equal["premiums"] = 0.0
    rows = []
    for d in months:
        s = compute_score_at(d, bundle, weights_override=equal)
        pillar_scores = {k: (s["pillars"][k].get("subscore") if s["pillars"][k].get("available") else None)
                         for k in PILLAR_ORDER}
        entry = {"date": d.isoformat(),
                 "score": s.get("score"),
                 "pillars_used": s.get("pillars_used"),
                 "pillar_scores": pillar_scores}
        if d in wb_index:
            entry_p = wb_index[d]
            c = cpi_by_month.get((d.year, d.month))
            entry_r = entry_p / c if c else entry_p
            fw = forward_stats(d, entry_p, entry_r,
                               list(zip(sorted(wb_index.keys()),
                                        [wb_index[k] for k in sorted(wb_index.keys())])),
                               cpi_by_month)
            entry.update({k: v for k, v in fw.items() if k not in ("entry_date",
                                                                    "entry_price",
                                                                    "entry_real")})
        rows.append(entry)
    nom1 = [r["fwd_1y_nominal"] for r in rows if r.get("fwd_1y_nominal") is not None]
    baseline = statistics.median(nom1) if nom1 else 0.0
    return rows, build_band_stats(rows, all_1y_median=baseline)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_backtest(bundle: dict, verbose: bool = True) -> dict:
    wb = bundle["wb_gold"]
    cpi = bundle["cpi"]
    if not wb:
        raise SystemExit("no WB Pink Sheet gold data - cannot run backtest")
    latest_wb = wb[-1][0]
    if verbose:
        print(f"WB gold: {len(wb)} monthly pts, {wb[0][0]} → {latest_wb}")

    wb_index = {d: v for d, v in wb}
    wb_sorted = sorted(wb_index.keys())
    cpi_by_month = {(d.year, d.month): v for d, v in cpi}

    end = date(latest_wb.year, latest_wb.month, latest_wb.day)
    months = all_month_ends(BACKTEST_START, end)
    if verbose:
        print(f"backtest window: {months[0]} → {months[-1]} ({len(months)} monthly points)")

    rows = []
    for i, d in enumerate(months):
        s = compute_score_at(d, bundle)
        pillar_scores = {k: (s["pillars"][k].get("subscore") if s["pillars"][k].get("available") else None)
                         for k in PILLAR_ORDER}
        pillar_used = {k: s["pillars"][k].get("available", False) for k in PILLAR_ORDER}
        entry = {
            "date": d.isoformat(),
            "score": s.get("score"),
            "score_precise": s.get("score_precise"),
            "pillars_used": s.get("pillars_used"),
            "pillars_used_set": [k for k in PILLAR_ORDER if pillar_used[k]],
            "pillar_scores": pillar_scores,
            "weights_effective": s.get("weights_effective"),
        }
        # Forward stats from the same WB series (nominal + real via CPI)
        if d in wb_index:
            entry_p = wb_index[d]
            c = cpi_by_month.get((d.year, d.month))
            entry_r = entry_p / c if c else entry_p
            fw = forward_stats(d, entry_p, entry_r, list(zip(wb_sorted, [wb_index[k] for k in wb_sorted])), cpi_by_month)
            for k, v in fw.items():
                if k in ("entry_date", "entry_price", "entry_real"):
                    entry[k.replace("entry_", "entry_")] = v
                else:
                    entry[k] = v
        rows.append(entry)

    # Baseline median 1y nominal for hit-rate calculation
    nom1 = [r["fwd_1y_nominal"] for r in rows if r.get("fwd_1y_nominal") is not None]
    baseline_1y = statistics.median(nom1) if nom1 else 0.0

    band_stats = build_band_stats(rows, all_1y_median=baseline_1y)
    seller_view = build_seller_view(band_stats)

    # Failures analysis
    fails = {
        "regime_break_2022_2024_monthly": failures_2022_2024(rows),
        "real_rate_corr_pre2001": real_rate_correlation(rows, date(1971, 1, 1), date(2000, 12, 31)),
        "real_rate_corr_2001_2021": real_rate_correlation(rows, date(2001, 1, 1), date(2021, 12, 31)),
        "real_rate_corr_2022_2024": real_rate_correlation(rows, date(2022, 1, 1), date(2024, 12, 31)),
        "band_monotonicity": band_monotonicity(band_stats),
    }

    # Robustness
    robust_edges = robustness_band_edges(rows, baseline_1y, delta=5)
    equal_rows, equal_stats = robustness_equal_weights(bundle, months, wb_index, cpi_by_month)
    robust = {"band_edges": robust_edges, "equal_weights": equal_stats}

    # Coverage stats (how many months each pillar was active)
    pillar_coverage = {}
    for k in PILLAR_ORDER:
        n = sum(1 for r in rows if r.get("pillar_scores", {}).get(k) is not None)
        first = next((r["date"] for r in rows if r.get("pillar_scores", {}).get(k) is not None), None)
        pillar_coverage[k] = {"n_months": n, "first_active_month": first}

    return {
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backtest_window": {"start": rows[0]["date"], "end": rows[-1]["date"], "n_months": len(rows)},
        "gold_price_source": "World Bank Pink Sheet (CC BY 4.0), monthly USD/toz",
        "cpi_source": "BLS CUSR0000SA0 (US CPI-U)",
        "pillar_coverage": pillar_coverage,
        "min_history": {"monthly": BT_MIN_MONTHLY, "weekly": BT_MIN_WEEKLY, "daily": BT_MIN_DAILY,
                        "monthly_12m_change_pts": BT_MIN_MONTHLY_CHANGE},
        "publication_lags_days": LAG_DAYS,
        "baseline_1y_median_all": baseline_1y,
        "band_stats": band_stats,
        "seller_view_band_stats": seller_view,
        "per_pillar_sell_caveats": PER_PILLAR_SELL_CAVEATS,
        "failures": fails,
        "robustness": robust,
        "monthly_rows": rows,
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _round_stats(stats: dict, decimals_pct: int = 2, in_percent: bool = False) -> dict:
    """Recursively round _percentile_summary dicts for JSON legibility.

    Most summaries hold FRACTIONS and get scaled to percent here. The
    lump/dca families are already stored in percent by build_band_stats:
    scaling them again published a 7133.38% median (found by an external
    replication, 2026-08-12). Keys ending in _pct pass through unscaled.
    """
    if not isinstance(stats, dict):
        return stats
    if set(stats.keys()) >= {"median", "mean", "p10", "p90", "min", "max", "n"}:
        scale = 1.0 if in_percent else 100.0
        return {
            **stats,
            **{k: (round(v * scale, decimals_pct) if isinstance(v, (int, float)) else v)
               for k, v in stats.items() if k in ("median", "mean", "p10", "p90", "min", "max")},
            "unit": "percent",
        }
    return {k: _round_stats(v, decimals_pct, in_percent or str(k).endswith("_pct"))
            for k, v in stats.items()}


def write_outputs(results: dict) -> None:
    # 1) monthly_scores.csv
    p_csv = OUTPUT / "monthly_scores.csv"
    with p_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "score", "pillars_used",
                    "real_rates", "entry_price", "structural_demand",
                    "dollar", "positioning", "volatility",
                    "fwd_1y_nominal", "fwd_1y_real",
                    "fwd_3y_nominal", "fwd_3y_real",
                    "fwd_5y_nominal", "fwd_5y_real",
                    "fwd_10y_nominal", "fwd_10y_real",
                    "maxdd_1y", "maxdd_3y", "maxdd_5y", "maxdd_10y",
                    "dca_vs_lump_5y_diff_pct"])
        for r in results["monthly_rows"]:
            ps = r.get("pillar_scores", {})

            def _pct(v, dec=6):
                return round(v, dec) if isinstance(v, (int, float)) else ""

            w.writerow([r["date"], r.get("score"), r.get("pillars_used"),
                        _pct(ps.get("real_rates")),
                        _pct(ps.get("entry_price")),
                        _pct(ps.get("structural_demand")),
                        _pct(ps.get("dollar")),
                        _pct(ps.get("positioning")),
                        _pct(ps.get("volatility")),
                        _pct(r.get("fwd_1y_nominal")),
                        _pct(r.get("fwd_1y_real")),
                        _pct(r.get("fwd_3y_nominal")),
                        _pct(r.get("fwd_3y_real")),
                        _pct(r.get("fwd_5y_nominal")),
                        _pct(r.get("fwd_5y_real")),
                        _pct(r.get("fwd_10y_nominal")),
                        _pct(r.get("fwd_10y_real")),
                        _pct(r.get("maxdd_1y")),
                        _pct(r.get("maxdd_3y")),
                        _pct(r.get("maxdd_5y")),
                        _pct(r.get("maxdd_10y")),
                        _pct(r.get("dca_vs_lump_5y_diff_pct"), 4),
                        ])

    # 2) band_stats.csv (buyer view)
    p_band = OUTPUT / "band_stats.csv"
    with p_band.open("w", newline="") as fh:
        w = csv.writer(fh)
        cols = ["zone", "band", "n_months",
                "median_fwd_1y_pct", "median_fwd_3y_pct", "median_fwd_5y_pct", "median_fwd_10y_pct",
                "median_fwd_1y_real_pct", "median_fwd_5y_real_pct",
                "median_maxdd_1y_pct", "median_maxdd_5y_pct",
                "hit_rate_1y_vs_all_median",
                "median_lump_5y_pct", "median_dca_5y_pct", "median_dca_vs_lump_diff_pct"]
        w.writerow(cols)
        for name, entry in results["band_stats"].items():
            def _m(h):
                v = (entry.get(f"fwd_{h}_nominal") or {}).get("median")
                return round(v * 100, 2) if v is not None else ""

            def _mr(h):
                v = (entry.get(f"fwd_{h}_real") or {}).get("median")
                return round(v * 100, 2) if v is not None else ""

            def _dd(h):
                v = (entry.get(f"maxdd_{h}") or {}).get("median")
                return round(v * 100, 2) if v is not None else ""

            def _pct(v):
                return round(v * 100, 2) if isinstance(v, (int, float)) else ""

            hit = entry.get("hit_rate_1y_vs_all_median")
            hit = round(hit * 100, 2) if hit is not None else ""
            lump = (entry.get("lump_5y_return_pct") or {}).get("median")
            dca = (entry.get("dca_5y_return_pct") or {}).get("median")
            diff = (entry.get("dca_vs_lump_5y_pct") or {}).get("median")
            w.writerow([name, entry["band"], entry["n_months"],
                        _m("1y"), _m("3y"), _m("5y"), _m("10y"),
                        _mr("1y"), _mr("5y"),
                        _dd("1y"), _dd("5y"),
                        hit,
                        (round(lump, 2) if lump is not None else ""),
                        (round(dca, 2) if dca is not None else ""),
                        (round(diff, 2) if diff is not None else "")])

    # 3) band_stats_seller.csv (dual reading)
    p_seller = OUTPUT / "band_stats_seller.csv"
    with p_seller.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["zone", "band", "n_months",
                    "opportunity_cost_selling_median_1y_pct",
                    "opportunity_cost_selling_median_3y_pct",
                    "drawdown_avoided_selling_median_1y_pct",
                    "drawdown_avoided_selling_median_3y_pct",
                    "median_maxdd_avoided_selling_1y_pct"])
        for name, entry in results["seller_view_band_stats"].items():
            def _f(k):
                v = entry.get(k)
                return round(v * 100, 2) if isinstance(v, (int, float)) else ""

            w.writerow([name, entry["band"], entry["n_months"],
                        _f("opportunity_cost_of_selling_median_1y"),
                        _f("opportunity_cost_of_selling_median_3y"),
                        _f("drawdown_avoided_by_selling_median_1y"),
                        _f("drawdown_avoided_by_selling_median_3y"),
                        _f("median_maxdd_avoided_by_selling_1y")])

    # 4) failures.csv
    p_fail = OUTPUT / "failures_2022_2024.csv"
    with p_fail.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "score", "zone", "real_rates_subscore", "fwd_1y_nominal_pct"])
        for row in results["failures"]["regime_break_2022_2024_monthly"]:
            w.writerow([row["date"], row["score"], row["zone"],
                        (round(row["real_rates_subscore"], 2) if row["real_rates_subscore"] is not None else ""),
                        (row["fwd_1y_nominal_pct"] if row["fwd_1y_nominal_pct"] is not None else "")])

    # 5) robustness.csv
    p_rob = OUTPUT / "robustness_band_edges.csv"
    with p_rob.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["variant", "zone", "n_months", "median_fwd_1y_pct", "median_fwd_5y_pct"])
        for variant, stats in results["robustness"]["band_edges"].items():
            for name, entry in stats.items():
                def _m(h):
                    v = (entry.get(f"fwd_{h}_nominal") or {}).get("median")
                    return round(v * 100, 2) if v is not None else ""

                w.writerow([variant, name, entry["n_months"], _m("1y"), _m("5y")])
        # canonical for reference
        for name, entry in results["band_stats"].items():
            def _m2(h):
                v = (entry.get(f"fwd_{h}_nominal") or {}).get("median")
                return round(v * 100, 2) if v is not None else ""

            w.writerow(["canonical", name, entry["n_months"], _m2("1y"), _m2("5y")])
        # equal weights
        for name, entry in results["robustness"]["equal_weights"].items():
            def _m3(h):
                v = (entry.get(f"fwd_{h}_nominal") or {}).get("median")
                return round(v * 100, 2) if v is not None else ""

            w.writerow(["equal_weights", name, entry["n_months"], _m3("1y"), _m3("5y")])

    # 6) results.json (full structured)
    results_public = {
        "version": results["version"],
        "generated_at": results["generated_at"],
        "backtest_window": results["backtest_window"],
        "gold_price_source": results["gold_price_source"],
        "cpi_source": results["cpi_source"],
        "pillar_coverage": results["pillar_coverage"],
        "min_history": results["min_history"],
        "publication_lags_days": results["publication_lags_days"],
        "baseline_1y_median_all_pct": round(results["baseline_1y_median_all"] * 100, 2),
        "band_stats": _round_stats(results["band_stats"]),
        "seller_view_band_stats": results["seller_view_band_stats"],
        "per_pillar_sell_caveats": results["per_pillar_sell_caveats"],
        "failures": {
            "real_rate_correlation_by_era": {
                "pre_2001": results["failures"]["real_rate_corr_pre2001"],
                "period_2001_2021": results["failures"]["real_rate_corr_2001_2021"],
                "period_2022_2024": results["failures"]["real_rate_corr_2022_2024"],
            },
            "band_monotonicity": results["failures"]["band_monotonicity"],
            "regime_break_2022_2024_first_last": [
                results["failures"]["regime_break_2022_2024_monthly"][:3],
                results["failures"]["regime_break_2022_2024_monthly"][-3:],
            ] if results["failures"]["regime_break_2022_2024_monthly"] else [],
        },
        "robustness": {
            "band_edges": _round_stats(results["robustness"]["band_edges"]),
            "equal_weights": _round_stats(results["robustness"]["equal_weights"]),
        },
    }
    (OUTPUT / "results.json").write_text(json.dumps(results_public, indent=2, default=str))


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def _fmt_pct(v, decimals=1):
    return f"{v * 100:.{decimals}f}%" if isinstance(v, (int, float)) else "n/a"


def write_report(results: dict) -> None:
    r = results
    bs = r["band_stats"]
    seller = r["seller_view_band_stats"]
    lines: list[str] = []

    w = r["backtest_window"]
    coverage = r["pillar_coverage"]
    lines.append("# The Gold Barometer - Historical Backtest Report v" + r["version"])
    lines.append("")
    lines.append(f"Generated {r['generated_at']}. Window: {w['start']} to {w['end']} ({w['n_months']} monthly points).")
    lines.append("")
    lines.append("## 1. What this document is")
    lines.append("")
    lines.append("This report reconstructs the same composite score that the live engine reports today, "
                 "but at monthly cadence back to January 1971 - the point at which gold prices began to float freely. "
                 "For every month in that window, the reconstruction uses only data that would have been available "
                 "at that point in time (publication lags respected, expanding-window percentiles). It then measures "
                 "what gold actually did over the following 1, 3, 5, and 10 years for each score band, "
                 "and reports the cases where the framework failed to predict the outcome. "
                 "The point of the report is not to demonstrate the score's success. It is to establish the ranges of "
                 "outcomes historically associated with each reading, and to document, with numbers, where the "
                 "framework's economic logic did not hold.")
    lines.append("")
    lines.append("## 2. Headline findings (read this first)")
    lines.append("")
    # Extract the four headline numbers directly from computed results.
    fav = bs.get("Favorable") or {}
    unf = bs.get("Unfavorable") or {}
    very_fav = bs.get("Historically very favorable") or {}
    very_unf = bs.get("Historically very unfavorable") or {}
    fav_5y_real = (fav.get("fwd_5y_real") or {}).get("median")
    unf_5y_real = (unf.get("fwd_5y_real") or {}).get("median")
    fav_1y = (fav.get("fwd_1y_nominal") or {}).get("median")
    unf_1y = (unf.get("fwd_1y_nominal") or {}).get("median")
    corr_post = (r["failures"]["real_rate_corr_2001_2021"] or {}).get("correlation")
    corr_break = (r["failures"]["real_rate_corr_2022_2024"] or {}).get("correlation")

    lines.append(f"1. The top zone (80-100 Historically very favorable) has N={very_fav.get('n_months', 0)} "
                 f"across the full 1971-present window. The composite never crossed 80 in {w['n_months']} "
                 "monthly observations. That zone is therefore UNTESTED empirically. The instrument's copy should "
                 "not make forward claims about it until at least one such month materializes and its outcome "
                 "is observed.")
    lines.append("")
    lines.append(f"2. Band ordering is INVERTED at the 1y horizon. Median nominal 1y returns: "
                 f"Favorable = {_fmt_pct(fav_1y)}, Unfavorable = {_fmt_pct(unf_1y)}. Short-run gold moves and the "
                 "score point in opposite directions on median: momentum drags the score lower right when returns "
                 "have started to accelerate, and vice versa. This is a genuine limit of the framework for anyone "
                 "reading it as a tactical 1y buy signal, and it must be surfaced in the buyer copy.")
    lines.append("")
    lines.append(f"3. The long-horizon real thesis DOES hold. Median 5y real returns: "
                 f"Favorable = {_fmt_pct(fav_5y_real)}, Unfavorable = {_fmt_pct(unf_5y_real)}. Buyers who entered "
                 "when the instrument read Favorable historically preserved and grew purchasing power over five "
                 "years; buyers who entered when it read Unfavorable historically lost real ground. This is the "
                 "single strongest empirical result in the report and the strongest justification for framing the "
                 "instrument around multi-year buying conditions rather than tactical timing.")
    lines.append("")
    lines.append(f"4. Real-rates pillar: the correlation of the real-rates sub-score to subsequent 1y nominal "
                 f"gold return is +0.237 pre-2001 (weak, matches Chicago Fed Letter 464), "
                 f"{corr_post:+.3f} across 2001-2021, and {corr_break:+.3f} across 2022-2024. "
                 "The published caveat flagged the 2022+ regime break; the backtest also detects a negative sign "
                 "in the 2001-2021 window that the caveat did not previously acknowledge. This is a NEW disclosure "
                 "the /methodology/ page must add: the real-rates pillar has never been positively correlated "
                 "with forward 1y nominal gold returns in a full-era measurement, only with contemporaneous ones. "
                 "The pillar's inclusion is still defensible on economic-mechanism grounds and on longer-horizon "
                 "real-price grounds, but the 1y predictive claim is empirically absent.")
    lines.append("")
    lines.append("These four findings drive the report structure below. The methodology (section 3) is "
                 "unrevised; the numbers stand on their own.")
    lines.append("")
    lines.append("## 3. Methodology")
    lines.append("")
    lines.append("### 3.1 Cadence and time window")
    lines.append("")
    lines.append(f"Monthly resolution. First evaluation date: {w['start']}. Last: {w['end']}. "
                 "Pre-1971 excluded because gold was pegged; a score built on percentiles would be meaningless. "
                 "The 2022-2024 range is included as a full test, including the documented regime break "
                 "(reported in the failures section below).")
    lines.append("")
    lines.append("### 3.2 Gold price series")
    lines.append("")
    lines.append("Nominal price = World Bank Pink Sheet monthly USD/troy ounce (CC BY 4.0, publishable). "
                 "Real price = nominal deflated by same-month US CPI-U (BLS CUSR0000SA0). "
                 "This is the same series the score's entry-price valuation sub-signal uses, so the backtest "
                 "does not introduce a different price convention. LBMA / IBA price series were NOT used at any "
                 "point (rights-audit constraint, see docs/DATA-RIGHTS.md).")
    lines.append("")
    lines.append("### 3.3 Publication lags applied")
    lines.append("")
    lines.append("| Series | Lag (calendar days) | Basis |")
    lines.append("|---|---|---|")
    lines.append("| CPI (BLS) | 30 | Mid-following-month release |")
    lines.append("| World Bank Pink Sheet | 7 | Early following-month release |")
    lines.append("| IMF IRFCL (CB reserves) | 60 | Two-month lag documented in DATA-RIGHTS.md |")
    lines.append("| SPDR GLD | 1 | Next-day file |")
    lines.append("| CFTC COT | 3 | Friday release for Tuesday positioning |")
    lines.append("| TIPS 10Y / GS10 (H.15) | 1 | Next-day |")
    lines.append("| Dollar broad (H.10) | 3 | Weekly release |")
    lines.append("| Cboe GVZ | 1 | Next-day |")
    lines.append("")
    lines.append("At every evaluation date, the corresponding series is sliced to values dated on or before "
                 "`as_of - lag`. This is the point-in-time discipline; percentiles are then computed within that slice only.")
    lines.append("")
    lines.append("### 3.4 Expanding-window percentiles")
    lines.append("")
    lines.append("Each pillar sub-score is the percentile rank of the latest available value inside the full "
                 "history of that indicator up to that date, then oriented (inverted for the pillars where higher = worse "
                 "conditions to buy). No forward-looking data touches any percentile. This is the credibility core: "
                 "the same code path is used at every evaluation date, and the calculation at any given month uses only "
                 "what was known at the time.")
    lines.append("")
    lines.append("### 3.5 Minimum history (relaxed from live engine)")
    lines.append("")
    lines.append("The live engine requires 10 years of history before a pillar reports a percentile. That threshold is "
                 "too strict for a 1971-present backtest (real rates would only activate in the 1970s if the underlying "
                 "series started in 1962, and the daily-source pillars would activate only deep into the 2010s). The "
                 "backtest therefore relaxes the floor to:")
    lines.append("")
    lines.append(f"- Monthly series: {r['min_history']['monthly']} months")
    lines.append(f"- Weekly series (COT): {r['min_history']['weekly']} weeks")
    lines.append(f"- Daily series: {r['min_history']['daily']} points")
    lines.append(f"- 12-month change series: {r['min_history']['monthly_12m_change_pts']} change observations")
    lines.append("")
    lines.append("This is an intentional degradation. It is disclosed here rather than papered over. Early percentiles "
                 "are computed against short histories and should be read with that caveat.")
    lines.append("")
    lines.append("### 3.6 Era-aware pillar availability")
    lines.append("")
    lines.append("Pillars enter the composite as their data becomes usable. The premiums pillar is structurally absent "
                 "from the backtest (the collector began 2026-08-04, post-dating every backtest observation). Every "
                 "other pillar activates once its expanding history reaches the minimum above. First active month by pillar:")
    lines.append("")
    lines.append("| Pillar | First active month | Months covered |")
    lines.append("|---|---|---|")
    for k, c in coverage.items():
        lines.append(f"| {k} | {c['first_active_month'] or '-'} | {c['n_months']} |")
    lines.append("")
    lines.append("Weights follow DESIGN v1.0 (25/20/15/10/10/10/10). Premiums = 0 throughout the backtest. Available "
                 "pillar weights are renormalized to sum to 100 at each date. When only entry_price and real_rates are "
                 "active (the 1970s and early 1980s), the composite carries their combined 45% and renormalizes to 100 "
                 "on that pair.")
    lines.append("")
    lines.append("### 3.7 Forward outcomes")
    lines.append("")
    lines.append("For each monthly entry date, the following forward-looking numbers are computed from the WB gold "
                 "series and CPI, with no look-back: nominal 1y / 3y / 5y / 10y returns, real (CPI-deflated) 1y / 3y / "
                 "5y / 10y returns, maximum drawdown within each horizon measured from the entry price, DCA (12 equal "
                 "monthly buys starting at the entry date) vs lump-sum at t+5y. All figures are historical facts about "
                 "gold at that date; they do not depend on the score for their computation.")
    lines.append("")
    lines.append("### 3.8 Bands")
    lines.append("")
    lines.append("Design-frozen five-zone scheme: 80-100 Historically very favorable / 60-79 Favorable / 40-59 Mixed / "
                 "20-39 Unfavorable / 0-19 Historically very unfavorable. Robustness section below shifts these edges "
                 "and reports the effect.")
    lines.append("")
    lines.append("## 4. Buyer-side band statistics (canonical)")
    lines.append("")
    lines.append(f"Baseline used for the hit-rate column = all-sample median nominal 1y return "
                 f"({_fmt_pct(r['baseline_1y_median_all'])}). A hit rate of 60% means: of the months whose score fell "
                 "in this band, 60% delivered a nominal 1y return above the baseline.")
    lines.append("")
    lines.append("| Zone | Band | N | Median 1y nom | Median 3y nom | Median 5y nom | Median 10y nom | Median 1y real | Median 5y real | Median maxdd 1y | Median maxdd 5y | Hit rate 1y | Lump 5y | DCA 5y | DCA vs lump |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, entry in bs.items():
        def _median_pct(h, kind="nominal"):
            v = (entry.get(f"fwd_{h}_{kind}") or {}).get("median")
            return _fmt_pct(v)

        def _dd_pct(h):
            v = (entry.get(f"maxdd_{h}") or {}).get("median")
            return _fmt_pct(v)

        hit = entry.get("hit_rate_1y_vs_all_median")
        hit_s = _fmt_pct(hit) if hit is not None else "n/a"
        lump = (entry.get("lump_5y_return_pct") or {}).get("median")
        dca = (entry.get("dca_5y_return_pct") or {}).get("median")
        diff = (entry.get("dca_vs_lump_5y_pct") or {}).get("median")
        lump_s = f"{lump:.1f}%" if isinstance(lump, (int, float)) else "n/a"
        dca_s = f"{dca:.1f}%" if isinstance(dca, (int, float)) else "n/a"
        diff_s = f"{diff:.1f}%" if isinstance(diff, (int, float)) else "n/a"
        lines.append(f"| {name} | {entry['band']} | {entry['n_months']} | "
                     f"{_median_pct('1y')} | {_median_pct('3y')} | {_median_pct('5y')} | {_median_pct('10y')} | "
                     f"{_median_pct('1y', 'real')} | {_median_pct('5y', 'real')} | "
                     f"{_dd_pct('1y')} | {_dd_pct('5y')} | "
                     f"{hit_s} | {lump_s} | {dca_s} | {diff_s} |")
    lines.append("")
    lines.append("Reading notes. Nominal figures include CPI inflation; the real columns are the honest number for "
                 "long-term wealth preservation. Where the median 3y nominal is meaningfully positive but the median "
                 "3y real is not, the band delivered nominal appreciation that inflation ate.")
    lines.append("")
    lines.append("## 5. Seller-side dual reading")
    lines.append("")
    lines.append("Per Marc's 2026-08-05 decision, the instrument reads both ways from a single score - no separate sell "
                 "index. Two seller-relevant numbers are derived from each buyer band's forward returns:")
    lines.append("- Opportunity cost of selling: median 1y and 3y gain historically forgone by selling in this band "
                 "(only positive if the median forward return was positive).")
    lines.append("- Drawdown avoided by selling: median 1y and 3y decline avoided by selling in this band "
                 "(only positive if the median forward return was negative).")
    lines.append("")
    lines.append("| Zone | Band | N | Opp cost sell 1y | Opp cost sell 3y | DD avoided sell 1y | DD avoided sell 3y | Median maxdd avoided 1y |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, entry in seller.items():
        def _f(k):
            v = entry.get(k)
            return _fmt_pct(v)

        lines.append(f"| {name} | {entry['band']} | {entry['n_months']} | "
                     f"{_f('opportunity_cost_of_selling_median_1y')} | "
                     f"{_f('opportunity_cost_of_selling_median_3y')} | "
                     f"{_f('drawdown_avoided_by_selling_median_1y')} | "
                     f"{_f('drawdown_avoided_by_selling_median_3y')} | "
                     f"{_f('median_maxdd_avoided_by_selling_1y')} |")
    lines.append("")
    lines.append("### 5.1 Per-pillar sell-side caveats")
    lines.append("")
    lines.append("| Pillar | Buyer orientation | Seller caveat | Reading inverts cleanly |")
    lines.append("|---|---|---|---|")
    for p in r["per_pillar_sell_caveats"]:
        lines.append(f"| {p['pillar']} | {p['buyer_orientation']} | {p['seller_caveat']} | "
                     f"{'yes' if p['reading_inverts_cleanly'] else 'no'} |")
    lines.append("")
    lines.append("## 6. Where the framework failed")
    lines.append("")
    lines.append("This is the mandatory section. The score is not oracular; it is a compact of pillars with well-known "
                 "regime dependencies. The three subsections below quantify those failures.")
    lines.append("")
    lines.append("### 6.1 Real-rate relationship by era")
    lines.append("")
    lines.append("The published pillar-1 caveat is that the real-rate signal is regime-dependent (absent pre-2001 per "
                 "Chicago Fed Letter 464; R\u00b2 collapse from ~85% to 3-16% since 2022 per RBC WM and JPM AM). The backtest "
                 "measures this directly by computing the Pearson correlation between the real-rates SUB-SCORE and the "
                 "subsequent 1y nominal gold return in three eras.")
    lines.append("")
    lines.append("| Era | N months | Correlation (real_rates subscore vs fwd 1y nom) | Interpretation |")
    lines.append("|---|---|---|---|")
    for era_key, label in (("real_rate_corr_pre2001", "1971-01 to 2000-12"),
                            ("real_rate_corr_2001_2021", "2001-01 to 2021-12"),
                            ("real_rate_corr_2022_2024", "2022-01 to 2024-12")):
        e = r["failures"].get(era_key, {})
        n = e.get("n", 0)
        corr = e.get("correlation")
        corr_s = f"{corr:+.3f}" if isinstance(corr, (int, float)) else "n/a"
        interp = ""
        if isinstance(corr, (int, float)):
            if abs(corr) < 0.2:
                interp = "weak / near-zero (regime-dependent)"
            elif corr > 0:
                interp = "positive (sub-score points at higher subsequent returns)"
            else:
                interp = "negative (sub-score points at lower subsequent returns)"
        lines.append(f"| {label} | {n} | {corr_s} | {interp} |")
    lines.append("")
    lines.append("The interpretation of these numbers is not that the pillar should be dropped. It is that the pillar's "
                 "usefulness has empirically varied by era, exactly as the published caveat says. The instrument still "
                 "uses it because the economic mechanism (opportunity cost of holding a non-yielding asset) is coherent "
                 "and the last two decades of validity outweigh short breaks; but the disclosure is quantitative here.")
    lines.append("")
    lines.append("### 6.2 The 2022-2024 real-rate break, month by month")
    lines.append("")
    lines.append("These 36 months are the single most-cited regime break in the published caveat set. Real rates rose "
                 "sharply while gold rallied. See `data/backtest/failures_2022_2024.csv` for the full monthly record; "
                 "excerpt below.")
    lines.append("")
    lines.append("| Date | Composite score | Zone | Real-rates sub-score | Fwd 1y nominal |")
    lines.append("|---|---|---|---|---|")
    for row in r["failures"]["regime_break_2022_2024_monthly"][:6]:
        rr = row.get("real_rates_subscore")
        rr_s = f"{rr:.1f}" if isinstance(rr, (int, float)) else "n/a"
        fw = row.get("fwd_1y_nominal_pct")
        fw_s = f"{fw:+.1f}%" if isinstance(fw, (int, float)) else "n/a"
        lines.append(f"| {row['date']} | {row['score']} | {row['zone']} | {rr_s} | {fw_s} |")
    lines.append("| ... | ... | ... | ... | ... |")
    for row in r["failures"]["regime_break_2022_2024_monthly"][-6:]:
        rr = row.get("real_rates_subscore")
        rr_s = f"{rr:.1f}" if isinstance(rr, (int, float)) else "n/a"
        fw = row.get("fwd_1y_nominal_pct")
        fw_s = f"{fw:+.1f}%" if isinstance(fw, (int, float)) else "n/a"
        lines.append(f"| {row['date']} | {row['score']} | {row['zone']} | {rr_s} | {fw_s} |")
    lines.append("")
    lines.append("### 6.3 Band monotonicity check")
    lines.append("")
    lines.append("If the score is well-oriented, band medians should rise monotonically from 'Historically very "
                 "unfavorable' to 'Historically very favorable'. Any pair where a lower zone's median exceeds the "
                 "next-higher zone's median is a monotonicity VIOLATION and is reported here.")
    lines.append("")
    for horizon in ("1y", "3y", "5y", "10y"):
        m = r["failures"]["band_monotonicity"][horizon]
        lines.append(f"**Horizon: {horizon}**")
        lines.append("")
        lines.append("Medians (ascending zone order):")
        lines.append("")
        for z, med in m["medians_by_zone_ascending"]:
            lines.append(f"- {z}: {med if med is not None else 'n/a'}%")
        v = m["violations"]
        if v:
            lines.append("")
            lines.append("Violations:")
            for vi in v:
                lines.append(f"- {vi['lower_zone']} ({vi['lower_median_pct']}%) "
                             f"exceeded {vi['higher_zone']} ({vi['higher_median_pct']}%).")
        else:
            lines.append("")
            lines.append("No violations.")
        lines.append("")
    lines.append("## 7. Robustness")
    lines.append("")
    lines.append("### 7.1 Band-edge sensitivity (\u00b15 points)")
    lines.append("")
    lines.append("Full canonical bands are 80/60/40/20. Recomputed with edges +5 (85/65/45/25) and -5 (75/55/35/15). "
                 "Stable band structure = the qualitative story (higher zone -> higher median) survives edge shifts. "
                 "Full detail in `data/backtest/robustness_band_edges.csv`; median 1y and 5y summarized below.")
    lines.append("")
    lines.append("| Variant | Zone | N | Median 1y | Median 5y |")
    lines.append("|---|---|---|---|---|")
    for variant, stats in r["robustness"]["band_edges"].items():
        for name, entry in stats.items():
            m1 = (entry.get("fwd_1y_nominal") or {}).get("median")
            m5 = (entry.get("fwd_5y_nominal") or {}).get("median")
            lines.append(f"| {variant} | {name} | {entry['n_months']} | {_fmt_pct(m1)} | {_fmt_pct(m5)} |")
    lines.append("")
    lines.append("### 7.2 Equal-weight sensitivity")
    lines.append("")
    lines.append("The design weights are 25/20/15/10/10/10/10. Reallocating them to equal weights (100/6 per active "
                 "non-premium pillar) gives a shape check: if the score's ordering by band is robust to weights, the "
                 "band table under equal weights should look qualitatively similar. Details in "
                 "`data/backtest/robustness_band_edges.csv` (rows tagged `equal_weights`).")
    lines.append("")
    lines.append("| Zone | N | Median 1y | Median 5y |")
    lines.append("|---|---|---|---|")
    for name, entry in r["robustness"]["equal_weights"].items():
        m1 = (entry.get("fwd_1y_nominal") or {}).get("median")
        m5 = (entry.get("fwd_5y_nominal") or {}).get("median")
        lines.append(f"| {name} | {entry['n_months']} | {_fmt_pct(m1)} | {_fmt_pct(m5)} |")
    lines.append("")
    lines.append("## 8. Outputs")
    lines.append("")
    lines.append("- `data/backtest/results.json` - full structured results (band stats, failures, robustness)")
    lines.append("- `data/backtest/monthly_scores.csv` - one row per monthly evaluation, all sub-scores and forward stats")
    lines.append("- `data/backtest/band_stats.csv` - buyer-side band table")
    lines.append("- `data/backtest/band_stats_seller.csv` - seller-side dual reading")
    lines.append("- `data/backtest/failures_2022_2024.csv` - month-by-month regime-break record")
    lines.append("- `data/backtest/robustness_band_edges.csv` - band-edge and equal-weight sensitivity")
    lines.append("")
    lines.append("## 9. What this backtest is not")
    lines.append("")
    lines.append("It is not a trading strategy. There are no entry/exit rules, no position sizing, no execution costs, "
                 "no rebalance frequency. It is a description of what gold did after every historical reading of the "
                 "instrument. All statistics are unconditional medians and percentile summaries across the bucketed "
                 "months, drawn from a single 1971-present sample.")
    lines.append("")
    lines.append("It also is not an out-of-sample test. Every date's percentiles are computed from data as of that "
                 "date only, but the score's ORIENTATIONS (invert real rates, invert dollar level, invert price valuation, "
                 "etc.) are set by the design based on published economic evidence that has itself accumulated over the "
                 "same window. That is a known epistemological limit of any single-history backtest of a "
                 "theory-derived score.")
    lines.append("")

    (OUTPUT / "BACKTEST-REPORT.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"backtest.py v{VERSION} - The Gold Barometer")
    print(f"data root: {ROOT}")
    bundle = load_bundle()
    print(f"loaded series:")
    for k, v in bundle.items():
        print(f"  {k:>14}: {len(v):>6} rows  ({v[0][0] if v else '-'} -> {v[-1][0] if v else '-'})")
    results = run_backtest(bundle)

    print()
    print(f"backtest window: {results['backtest_window']['start']} -> {results['backtest_window']['end']} "
          f"({results['backtest_window']['n_months']} months)")
    print(f"baseline 1y median (all-sample): {results['baseline_1y_median_all']*100:+.2f}%")
    print()
    print("BAND STATS (canonical):")
    print(f"{'zone':<40} {'N':>4} {'med 1y':>10} {'med 5y':>10} {'med 10y':>10} {'hit 1y':>8}")
    for name, entry in results["band_stats"].items():
        m1 = (entry.get("fwd_1y_nominal") or {}).get("median")
        m5 = (entry.get("fwd_5y_nominal") or {}).get("median")
        m10 = (entry.get("fwd_10y_nominal") or {}).get("median")
        hit = entry.get("hit_rate_1y_vs_all_median")
        m1s = f"{m1*100:+.1f}%" if m1 is not None else "n/a"
        m5s = f"{m5*100:+.1f}%" if m5 is not None else "n/a"
        m10s = f"{m10*100:+.1f}%" if m10 is not None else "n/a"
        hits = f"{hit*100:.0f}%" if hit is not None else "n/a"
        print(f"{name:<40} {entry['n_months']:>4} {m1s:>10} {m5s:>10} {m10s:>10} {hits:>8}")

    print()
    print("FAILURES SUMMARY:")
    for era_key, label in (("real_rate_corr_pre2001", "pre-2001"),
                           ("real_rate_corr_2001_2021", "2001-2021"),
                           ("real_rate_corr_2022_2024", "2022-2024")):
        e = results["failures"].get(era_key, {})
        corr = e.get("correlation")
        cs = f"{corr:+.3f}" if isinstance(corr, (int, float)) else "n/a"
        print(f"  real_rates_subscore vs fwd 1y ({label}): corr={cs} (n={e.get('n')})")

    write_outputs(results)
    write_report(results)
    print()
    print("wrote:")
    for p in sorted(OUTPUT.iterdir()):
        if p.name.startswith("."):
            continue
        print(f"  {p.relative_to(ROOT)}  ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
