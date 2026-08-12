#!/usr/bin/env python3
"""compute_score.py — The Gold Barometer daily score engine v1.0.

Implements docs/DESIGN.md section 1 exactly. Reads canonical historical
series from data/history/ (built by scripts/pipeline/build_history.py),
plus daily premium data from data/premiums/*.jsonl, and produces:

  data/output/latest.json    — today's composite + full breakdown
  data/output/history.json   — composite time-series (backfilled monthly)
  data/output/history.csv    — same, tabular
  data/output/checks.json    — unit-style self-test results

DATA-RIGHTS constraint: raw GVZ values are NEVER included in any output.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
HISTORY = ROOT / "data" / "history"
SERIES = ROOT / "data" / "series"
PREMIUMS = ROOT / "data" / "premiums"
OUTPUT = ROOT / "data" / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)

VERSION = "1.0"

# Base weights per DESIGN 1.2 (Marc-validated 2026-08-04)
BASE_WEIGHTS = {
    "real_rates": 25,
    "entry_price": 20,
    "structural_demand": 15,
    "dollar": 10,
    "positioning": 10,
    "volatility": 10,
    "premiums": 10,
}

# Premium ramp per task spec: <90d = drop; 90d–12mo = 5%; ≥12mo = 10%
PREMIUM_COLLECTION_START = date(2026, 8, 4)  # first premium capture day
PREMIUM_RAMP_MIN_DAYS = 90
PREMIUM_FULL_WEIGHT_DAYS = 365

# Staleness limits per DESIGN 1.5 (calendar days, converted from business days
# with weekend/holiday buffer).
STALENESS_LIMITS_DAYS = {
    "daily": 7,     # 5 business days ≈ up to 7 cal days across a long weekend
    "weekly": 10,   # 10 calendar days for COT (published Fri, next Fri = 7d)
    "weekly_release": 14,  # H.10 broad dollar: weekly RELEASE with lag; the
                           # latest observation is routinely 8-13 cal days old
                           # just before the next release. The "daily" limit
                           # misclassified it (corrections log 2026-08-10).
    "monthly": 45,  # 45 cal days for CPI, WB gold, IMF, CB reporters
}

# Evidence grades per DESIGN 1.2 (published labels on /methodology/)
EVIDENCE_GRADES = {
    "real_rates": "strong (documented 2022 regime-break caveat)",
    "entry_price": "strong (Moskowitz–Ooi–Pedersen momentum; Erb–Harvey valuation)",
    "structural_demand": "strong for central banks, weak for ETF flows",
    "dollar": "moderate (numeraire caveat)",
    "positioning": "weak-to-moderate (contrarian at extremes only)",
    "volatility": "weak — risk/entry-quality gauge with no predictive claim",
    "premiums": "cost argument incontestable; predictive value anecdotal",
}

# Engine build: bumped on every implementation change that alters output.
# Published in latest.json and the daily archive so readings from
# different engines are distinguishable (verification wave 2026-08-12).
ENGINE_BUILD = "2026-08-12"

# Zones per DESIGN 1.4 (Marc-validated)
ZONES = [
    (80, 100, "Historically very favorable"),
    (60, 79, "Favorable"),
    (40, 59, "Mixed"),
    (20, 39, "Unfavorable"),
    (0, 19, "Historically very unfavorable"),
]

MIN_HISTORY_POINTS_DAILY = 2520     # ~10 years of business days
MIN_HISTORY_POINTS_WEEKLY = 520     # ~10 years of weeks
MIN_HISTORY_POINTS_MONTHLY = 120    # 10 years of months
MIN_HISTORY_POINTS_PREMIUMS = 90    # ramp threshold


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_series(path: Path) -> list[tuple[date, float]]:
    """Load a `date,value` CSV; return sorted ascending list of (date, float)."""
    rows: list[tuple[date, float]] = []
    if not path.exists():
        return rows
    with path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_d = (row.get("date") or "").strip()
            raw_v = (row.get("value") or "").strip()
            if not raw_d or not raw_v:
                continue
            try:
                d = datetime.fromisoformat(raw_d).date()
                v = float(raw_v)
            except ValueError:
                continue
            if math.isnan(v):
                continue
            rows.append((d, v))
    rows.sort(key=lambda r: r[0])
    return rows


# ---------------------------------------------------------------------------
# Numeric primitives (percentile, median, smoothing)
# ---------------------------------------------------------------------------

def percentile_rank(value: float, distribution: list[float]) -> float:
    """Percentile rank (0–100) of `value` inside `distribution`.

    Uses the midrank convention: `100 * (count_below + 0.5 * count_equal) / n`.
    This is symmetric and well-defined even for ties or single-point queries.
    """
    n = len(distribution)
    if n == 0:
        return 50.0
    below = 0
    equal = 0
    for x in distribution:
        if x < value:
            below += 1
        elif x == value:
            equal += 1
    return 100.0 * (below + 0.5 * equal) / n


def _median(values: list[float]) -> float:
    n = len(values)
    if n == 0:
        raise ValueError("median of empty sequence")
    s = sorted(values)
    if n % 2 == 1:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def median_smooth(rows: list[tuple[date, float]], window: int = 5) -> list[tuple[date, float]]:
    """Rolling median smoothing of a daily series (5-day default per DESIGN 1.3)."""
    if window <= 1:
        return list(rows)
    out: list[tuple[date, float]] = []
    values = [v for _, v in rows]
    for i, (d, _) in enumerate(rows):
        lo = max(0, i - window + 1)
        out.append((d, _median(values[lo : i + 1])))
    return out


def find_at_or_before(rows: list[tuple[date, float]], target: date) -> tuple[int, tuple[date, float]] | None:
    """Return (index, (date, value)) of the latest row with date <= target."""
    hit = None
    hit_idx = -1
    for i, (d, v) in enumerate(rows):
        if d <= target:
            hit = (d, v)
            hit_idx = i
        else:
            break
    if hit is None:
        return None
    return hit_idx, hit


def staleness_days(latest: date, today: date) -> int:
    return (today - latest).days


def cap_subscore(x: float) -> float:
    return max(0.0, min(100.0, x))


# ---------------------------------------------------------------------------
# Pillar 1 — Real rates (DESIGN 1.2 #1; EVIDENCE-BASE §1)
# Orientation: LOW real rates historically preceded better long-term gold
# returns (Erb & Harvey 2013; Chicago Fed Letter 464). Percentile → INVERT.
# 2022+ regime break disclosed on /methodology/, not curve-fitted here.
# ---------------------------------------------------------------------------

def pillar_real_rates(as_of: date, tips: list[tuple[date, float]],
                      proxy: list[tuple[date, float]]) -> dict:
    label = "real_rates"
    # Prefer TIPS 10Y (daily, 2003+); pre-2003 fall back to monthly proxy.
    smoothed = median_smooth(tips, window=5)
    hit = find_at_or_before(smoothed, as_of)
    source = "TIPS 10Y (H.15)"
    frequency = "daily"
    stale_limit = STALENESS_LIMITS_DAYS["daily"]
    if hit is None:
        # No TIPS at all: the monthly proxy is the designed pre-2003 path.
        pass
    elif (as_of - hit[1][0]).days > 365:
        # A YEAR-stale TIPS must not silently swap the measure back to the
        # proxy: the two differ by ~0.5pp in level, which once manufactured
        # an 18-point sub-score step (seam audit 2026-08-12). An honest drop
        # is the published behaviour for a silent source.
        return {"name": label, "available": False,
                "reason": f"TIPS silent for over a year ({(as_of - hit[1][0]).days}d)"}
    if hit is None:
        # Pre-2003 → use monthly real-rate proxy (GS10 minus CPI YoY)
        hit_p = find_at_or_before(proxy, as_of)
        if hit_p is None:
            return {"name": label, "available": False, "reason": "no data"}
        idx, (ld, lv) = hit_p
        dist = [v for _, v in proxy[: idx + 1]]
        if len(dist) < MIN_HISTORY_POINTS_MONTHLY:
            return {"name": label, "available": False, "reason": "insufficient history"}
        pct = percentile_rank(lv, dist)
        subscore = 100.0 - pct
        return _pillar_output(label, subscore,
                              latest_date=ld, latest_value=lv, unit="%",
                              source="10Y nominal – CPI YoY (proxy)",
                              frequency="monthly", staleness=staleness_days(ld, as_of),
                              stale_limit=STALENESS_LIMITS_DAYS["monthly"],
                              history_points=len(dist),
                              publishable_raw=True)
    idx, (ld, lv) = hit
    stale = staleness_days(ld, as_of)
    if stale > stale_limit:
        return {"name": label, "available": False, "reason": f"stale ({stale}d > {stale_limit}d)",
                "latest_date": ld.isoformat()}
    # Joint distribution: monthly proxy before TIPS begins, then month-end TIPS.
    # Ranking TIPS-era values inside the TIPS era alone collapsed the reference
    # window to a few years at the 2005 seam and manufactured a composite crash
    # with no economic cause (corrected 2026-08-11, see /corrections/). Monthly
    # sampling keeps eras equally weighted per unit of time.
    tips_start = smoothed[0][0]
    proxy_pre = [v for d, v in proxy if d < tips_start]
    by_month: dict[tuple[int, int], tuple[date, float]] = {}
    for d, v in smoothed[: idx + 1]:
        key = (d.year, d.month)
        prev = by_month.get(key)
        if prev is None or d > prev[0]:
            by_month[key] = (d, v)
    tips_monthly = [v for _, v in sorted(by_month.values(), key=lambda x: x[0])]
    dist = proxy_pre + tips_monthly
    if len(dist) < MIN_HISTORY_POINTS_MONTHLY:
        return {"name": label, "available": False, "reason": "insufficient history"}
    pct = percentile_rank(lv, dist)
    subscore = 100.0 - pct  # INVERT: low real rates → high sub-score
    return _pillar_output(label, subscore, latest_date=ld, latest_value=lv, unit="%",
                          source=source, frequency=frequency, staleness=stale,
                          stale_limit=stale_limit, history_points=len(dist),
                          publishable_raw=True,
                          notes="Lower real rates historically preceded better long-term "
                                "gold returns (EVIDENCE-BASE.md §1). 2022+ regime break published separately.")


# ---------------------------------------------------------------------------
# Pillar 2 — Entry price (DESIGN 1.2 #2; EVIDENCE-BASE §2)
# Two sub-signals averaged AND stored separately:
#   (a) Trend: price vs 12-month moving average (proxy for 200dma at the
#       monthly cadence of WB Pink Sheet) + 12-month return.
#       Orientation: below-average price / negative 12m return → HIGH sub-score.
#   (b) Valuation: real (CPI-deflated) price vs long-run average (multi-year,
#       Erb–Harvey golden constant). Orientation: below long-run real average
#       → HIGH sub-score.
# NOTE: The task calls for daily 200dma; we currently only have a monthly
# gold price with long history (World Bank, CC BY 4.0). Substitution documented
# here and surfaced in the output.
# ---------------------------------------------------------------------------

def pillar_entry_price(as_of: date, wb_gold: list[tuple[date, float]],
                       cpi: list[tuple[date, float]],
                       spot_now: float | None) -> dict:
    label = "entry_price"
    hit = find_at_or_before(wb_gold, as_of)
    if hit is None:
        return {"name": label, "available": False, "reason": "no gold price"}
    idx, (ld, lv) = hit
    stale = staleness_days(ld, as_of)
    if stale > STALENESS_LIMITS_DAYS["monthly"]:
        return {"name": label, "available": False,
                "reason": f"stale ({stale}d > {STALENESS_LIMITS_DAYS['monthly']}d)",
                "latest_date": ld.isoformat()}
    if idx < MIN_HISTORY_POINTS_MONTHLY:
        return {"name": label, "available": False, "reason": "insufficient history"}

    prices = wb_gold[: idx + 1]
    latest_price = lv
    # Only lean on live spot for the CURRENT reading (today), and only if it's
    # recent. Backfill computations pass spot_now=None.
    price_for_trend = latest_price
    if spot_now is not None and as_of == max((d for d, _ in wb_gold), default=as_of) + timedelta(days=1000):
        # never runs; kept for structural clarity
        pass
    if spot_now is not None:
        price_for_trend = spot_now  # today's live spot for the trend sub-signal only

    # 12-month moving average (proxy for 200dma at monthly cadence)
    # The MA must cover the 12 months BEFORE the point being measured,
    # matching the distribution convention (corrected 2026-08-11). With a
    # live spot the last 12 monthly closes already precede it; with the WB
    # fallback the window must exclude that same point.
    ma_window = prices[-12:] if spot_now is not None else prices[-13:-1]
    last_12 = [v for _, v in ma_window]
    if len(last_12) < 12:
        return {"name": label, "available": False, "reason": "insufficient 12-month window"}
    ma12 = sum(last_12) / 12.0
    dev_vs_ma = (price_for_trend - ma12) / ma12  # signed

    # 12-month return (t vs t-12)
    if len(prices) < 13:
        return {"name": label, "available": False, "reason": "no year-ago point"}
    # With a live spot the current point is NOT prices[-1], so twelve months
    # back is prices[-12]; the WB-fallback path keeps the exact 12-month gap
    # via prices[-13] (wave-3 review, 2026-08-12).
    year_ago_value = (prices[-12][1] if spot_now is not None else prices[-13][1])
    ret_12m = (price_for_trend - year_ago_value) / year_ago_value

    # Percentile within full history of the same signals (expanding window).
    # Build historical series of (dev_vs_ma, ret_12m).
    dev_series: list[float] = []
    ret_series: list[float] = []
    for i in range(12, len(prices)):
        window = [v for _, v in prices[i - 12 : i]]
        ma = sum(window) / 12.0
        dev_series.append((prices[i][1] - ma) / ma)
        ret_series.append((prices[i][1] - prices[i - 12][1]) / prices[i - 12][1])
    dev_pct = percentile_rank(dev_vs_ma, dev_series)
    ret_pct = percentile_rank(ret_12m, ret_series)
    trend_sub = 0.5 * ((100.0 - dev_pct) + (100.0 - ret_pct))  # invert: below-avg → high

    # Valuation sub-signal: CPI-deflated price vs long-run average.
    # Align to end-of-month CPI. Deflate each price by its own CPI level.
    cpi_by_month: dict[tuple[int, int], float] = {(d.year, d.month): v for d, v in cpi}
    deflated: list[tuple[date, float]] = []
    for pd_, pv in prices:
        key = (pd_.year, pd_.month)
        cval = cpi_by_month.get(key)
        if cval is None or cval <= 0:
            continue
        deflated.append((pd_, pv / cval))
    if len(deflated) < MIN_HISTORY_POINTS_MONTHLY:
        return {"name": label, "available": False, "reason": "insufficient deflated history"}
    latest_real = deflated[-1][1]
    real_dist = [v for _, v in deflated]
    real_pct = percentile_rank(latest_real, real_dist)
    valuation_sub = 100.0 - real_pct  # invert: cheap in real terms → high sub-score

    subscore = 0.5 * (trend_sub + valuation_sub)

    out = _pillar_output(label, subscore, latest_date=ld, latest_value=latest_price,
                         unit="USD/toz (WB monthly)",
                         source="World Bank Pink Sheet (CC BY 4.0)",
                         frequency="monthly", staleness=stale,
                         stale_limit=STALENESS_LIMITS_DAYS["monthly"],
                         history_points=len(prices),
                         publishable_raw=True)
    out["sub_signals"] = {
        "trend": {
            "subscore": round(cap_subscore(trend_sub), 2),
            "dev_vs_12m_ma_pct": round(dev_vs_ma * 100, 2),
            "return_12m_pct": round(ret_12m * 100, 2),
            "ma_window": "12 months (monthly proxy for 200dma; daily gold history not in pipeline v1)",
        },
        "valuation": {
            "subscore": round(cap_subscore(valuation_sub), 2),
            "real_price_percentile": round(real_pct, 2),
            "deflator": "US CPI-U (CUSR0000SA0), same-month",
            "reference": "Erb & Harvey golden constant",
        },
    }
    if spot_now is not None:
        out["sub_signals"]["trend"]["spot_used_usd_toz"] = round(spot_now, 2)
    return out


# ---------------------------------------------------------------------------
# Pillar 3 — Structural demand (DESIGN 1.2 #3; EVIDENCE-BASE §3)
# Average of two components computed separately:
#   (a) Central-bank reserves: 12-month change of sum-of-reporters
#       (IMF IRFCL, fine troy ounces). Orientation: net accumulation → HIGH.
#       Strong evidence (WGC GDT + Arslanalp/Eichengreen/Simpson-Bell 2023).
#   (b) ETF flows (COINCIDENT, low internal weight): 12-month change in GLD
#       holdings tonnes. Orientation: inflows → HIGH sub-score. Evidence weak
#       (WGC: ETF flows follow price).
# ---------------------------------------------------------------------------

def pillar_structural_demand(as_of: date,
                             cb_sum: list[tuple[date, float]],
                             gld: list[tuple[date, float]]) -> dict:
    label = "structural_demand"
    sub_a = _twelve_month_change_pct_pillar(cb_sum, as_of, stale_limit=STALENESS_LIMITS_DAYS["monthly"],
                                            invert=False, freq="monthly",
                                            min_hist=MIN_HISTORY_POINTS_MONTHLY)
    sub_b = _twelve_month_change_pct_pillar(gld, as_of, stale_limit=STALENESS_LIMITS_DAYS["daily"],
                                            invert=False, freq="daily",
                                            min_hist=MIN_HISTORY_POINTS_DAILY)
    if not sub_a["available"] and not sub_b["available"]:
        return {"name": label, "available": False, "reason": "no CB nor ETF"}
    parts = []
    if sub_a["available"]:
        parts.append(sub_a["subscore"])
    if sub_b["available"]:
        parts.append(sub_b["subscore"])
    subscore = sum(parts) / len(parts)
    latest_date = max((d for d in [sub_a.get("latest_date"), sub_b.get("latest_date")] if d))
    out = _pillar_output(label, subscore,
                         latest_date=date.fromisoformat(latest_date),
                         latest_value=None, unit="12m change of holdings",
                         source="IMF IRFCL (CB) + SPDR GLD tonnes (ETF, internal)",
                         frequency="mixed",
                         staleness=min(x for x in [sub_a.get("staleness_days", 999),
                                                   sub_b.get("staleness_days", 999)] if x is not None),
                         stale_limit=STALENESS_LIMITS_DAYS["monthly"],
                         history_points=max(sub_a.get("history_points", 0),
                                            sub_b.get("history_points", 0)),
                         publishable_raw=True)
    out["sub_signals"] = {"cb_purchases": sub_a, "etf_flows": sub_b}
    return out


def _twelve_month_change_pct_pillar(rows: list[tuple[date, float]], as_of: date,
                                    stale_limit: int, invert: bool, freq: str,
                                    min_hist: int) -> dict:
    hit = find_at_or_before(rows, as_of)
    if hit is None:
        return {"available": False, "reason": "no data"}
    idx, (ld, lv) = hit
    stale = staleness_days(ld, as_of)
    if stale > stale_limit:
        return {"available": False, "reason": f"stale ({stale}d)", "latest_date": ld.isoformat()}
    year_ago = ld - timedelta(days=365)
    prior = find_at_or_before(rows, year_ago)
    if prior is None or prior[1][1] == 0:
        return {"available": False, "reason": "no year-ago point"}
    _, (pd_, pv) = prior
    change_pct = (lv - pv) / abs(pv) * 100

    # Build historical 12m-change series at the same frequency to score by
    # percentile. Truncated to the as_of hit: a backfilled point must never
    # be ranked against changes from its own future (corrected 2026-08-11).
    rows = rows[: idx + 1]
    changes = []
    if freq == "monthly":
        for i in range(12, len(rows)):
            if rows[i - 12][1] != 0:
                changes.append((rows[i][1] - rows[i - 12][1]) / abs(rows[i - 12][1]) * 100)
    else:
        # daily: sample end-of-month values for a stable comparison base
        by_month: dict[tuple[int, int], tuple[date, float]] = {}
        for d, v in rows:
            key = (d.year, d.month)
            prev = by_month.get(key)
            if prev is None or d > prev[0]:
                by_month[key] = (d, v)
        monthlies = sorted(by_month.values(), key=lambda x: x[0])
        for i in range(12, len(monthlies)):
            if monthlies[i - 12][1] != 0:
                changes.append((monthlies[i][1] - monthlies[i - 12][1])
                               / abs(monthlies[i - 12][1]) * 100)
    if len(changes) < min_hist // 12 + 1:
        return {"available": False, "reason": "insufficient history for percentile"}
    pct = percentile_rank(change_pct, changes)
    subscore = pct if not invert else (100.0 - pct)
    return {
        "available": True,
        "subscore": round(cap_subscore(subscore), 2),
        "percentile": round(pct, 2),
        "change_12m_pct": round(change_pct, 3),
        "latest_date": ld.isoformat(),
        "prior_date": pd_.isoformat(),
        "staleness_days": stale,
        "history_points": len(changes),
    }


# ---------------------------------------------------------------------------
# Pillar 4 — Dollar (DESIGN 1.2 #4; EVIDENCE-BASE §4)
# H.10 broad dollar: percentile of LEVEL + percentile of 12m change.
# Orientation: strong / rising dollar = HEADWIND for gold → INVERT both.
# Numeraire caveat (Pukthuanthong–Roll) published on /methodology/.
# ---------------------------------------------------------------------------

def pillar_dollar(as_of: date, dollar: list[tuple[date, float]]) -> dict:
    label = "dollar"
    smoothed = median_smooth(dollar, window=5)
    hit = find_at_or_before(smoothed, as_of)
    if hit is None:
        return {"name": label, "available": False, "reason": "no data"}
    idx, (ld, lv) = hit
    stale = staleness_days(ld, as_of)
    if stale > STALENESS_LIMITS_DAYS["weekly_release"]:
        return {"name": label, "available": False,
                "reason": f"stale ({stale}d)", "latest_date": ld.isoformat()}
    dist = [v for _, v in smoothed[: idx + 1]]
    if len(dist) < MIN_HISTORY_POINTS_DAILY:
        return {"name": label, "available": False, "reason": "insufficient history"}
    level_pct = percentile_rank(lv, dist)
    level_sub = 100.0 - level_pct

    # 12m change
    year_ago = ld - timedelta(days=365)
    prior = find_at_or_before(smoothed, year_ago)
    if prior is None or prior[1][1] == 0:
        return {"name": label, "available": False, "reason": "no year-ago"}
    pv = prior[1][1]
    change_pct = (lv - pv) / abs(pv) * 100

    changes = []
    for i, (d, v) in enumerate(smoothed[: idx + 1]):
        prev = find_at_or_before(smoothed[:i + 1], d - timedelta(days=365))
        if prev is None or prev[1][1] == 0 or prev[0] == i:
            continue
        changes.append((v - prev[1][1]) / abs(prev[1][1]) * 100)
    change_pct_rank = percentile_rank(change_pct, changes) if changes else 50.0
    change_sub = 100.0 - change_pct_rank  # rising dollar → invert

    subscore = 0.5 * (level_sub + change_sub)
    return _pillar_output(label, subscore, latest_date=ld, latest_value=lv,
                          unit="index (H.10 broad)",
                          source="Board of Governors of the Federal Reserve System (H.10)",
                          frequency="weekly release", staleness=stale,
                          stale_limit=STALENESS_LIMITS_DAYS["weekly_release"],
                          history_points=len(dist),
                          publishable_raw=True,
                          notes="Numeraire caveat (Pukthuanthong–Roll) published on /methodology/.")


# ---------------------------------------------------------------------------
# Pillar 5 — Positioning (DESIGN 1.2 #5; EVIDENCE-BASE §5)
# COT managed-money net length as % of open interest (Socrata 088691).
# Framing: contrarian at extremes only (crowded long = fragility; washed-out
# short = better forward entry). Sub-score = 100 – percentile (monotonic
# contrarian; middle percentiles → neutral ~50).
# Lore caveat (Sanders–Irwin–Merrin; Wang: academic contrarian sits on
# HEDGERS, not managed-money) published on /methodology/.
# ---------------------------------------------------------------------------

def pillar_positioning(as_of: date, cot_pct_oi: list[tuple[date, float]]) -> dict:
    label = "positioning"
    hit = find_at_or_before(cot_pct_oi, as_of)
    if hit is None:
        return {"name": label, "available": False, "reason": "no data"}
    idx, (ld, lv) = hit
    stale = staleness_days(ld, as_of)
    if stale > STALENESS_LIMITS_DAYS["weekly"]:
        return {"name": label, "available": False, "reason": f"stale ({stale}d)",
                "latest_date": ld.isoformat()}
    dist = [v for _, v in cot_pct_oi[: idx + 1]]
    if len(dist) < MIN_HISTORY_POINTS_WEEKLY:
        return {"name": label, "available": False, "reason": "insufficient history"}
    pct = percentile_rank(lv, dist)
    subscore = 100.0 - pct  # contrarian
    return _pillar_output(label, subscore, latest_date=ld, latest_value=lv,
                          unit="% of open interest",
                          source="CFTC COT disaggregated, managed money (Socrata kh3c-gbw2)",
                          frequency="weekly", staleness=stale,
                          stale_limit=STALENESS_LIMITS_DAYS["weekly"],
                          history_points=len(dist),
                          publishable_raw=True,
                          notes="Contrarian orientation applied across the full percentile range; "
                                "effective signal concentrates at extremes.")


# ---------------------------------------------------------------------------
# Pillar 6 — Volatility (DESIGN 1.2 #6; EVIDENCE-BASE §6; DATA-RIGHTS Yellow)
# Cboe GVZ percentile. Orientation: high implied vol = LOW sub-score
# (framed as risk / entry-quality gauge, no predictive claim).
# HARD CONSTRAINT (DATA-RIGHTS.md): the transformation must be non-reversible
# without our internal full history. Percentile rank fits: recovering the raw
# GVZ from the published percentile requires knowing our full historical
# distribution. Raw GVZ values NEVER leave data/history/ into any output.
# ---------------------------------------------------------------------------

def pillar_volatility(as_of: date, gvz: list[tuple[date, float]]) -> dict:
    label = "volatility"
    smoothed = median_smooth(gvz, window=5)
    hit = find_at_or_before(smoothed, as_of)
    if hit is None:
        return {"name": label, "available": False, "reason": "no data"}
    idx, (ld, lv) = hit
    stale = staleness_days(ld, as_of)
    if stale > STALENESS_LIMITS_DAYS["daily"]:
        return {"name": label, "available": False, "reason": f"stale ({stale}d)",
                "latest_date": ld.isoformat()}
    dist = [v for _, v in smoothed[: idx + 1]]
    if len(dist) < MIN_HISTORY_POINTS_DAILY:
        return {"name": label, "available": False, "reason": "insufficient history"}
    pct = percentile_rank(lv, dist)
    subscore = 100.0 - pct  # high vol → poor entry conditions
    # NOTE: latest_value / raw is DELIBERATELY OMITTED from the returned dict
    # per DATA-RIGHTS.md (Cboe GVZ = YELLOW, non-reversible transformation only).
    return {
        "name": label,
        "available": True,
        "subscore": round(cap_subscore(subscore), 2),
        "percentile": round(pct, 2),
        "latest_date": ld.isoformat(),
        "staleness_days": stale,
        "stale_limit_days": STALENESS_LIMITS_DAYS["daily"],
        "history_points": len(dist),
        "source": "Cboe Gold Volatility Index (GVZ) — internal-only per DATA-RIGHTS.md",
        "frequency": "daily",
        "publishable_raw": False,
        "notes": "Percentile rank is non-reversible without our full internal history "
                 "(DATA-RIGHTS hard constraint). Raw GVZ values are not published.",
    }


# ---------------------------------------------------------------------------
# Pillar 7 — Retail premiums (DESIGN 1.2 #7; EVIDENCE-BASE §7)
# Own daily data collected by scripts/collect_premiums.js since 2026-08-04.
# For v1 we parse SD Bullion records (JM Bullion + APMEX blocked as of
# 2026-08-04 — stealth layer TODO). Premium % = median across products of
# (product_price − spot) / spot × 100. Daily percentile rank (once we have
# enough history) → INVERT (high premium = mechanically worse all-in price).
# Ramp per task spec: <90d = drop (weight redistributed); 90d–12mo = 5% weight;
# ≥12mo = 10% weight. Predictive claim NOT made (cost argument only).
# ---------------------------------------------------------------------------

def _parse_premium_day(path: Path) -> dict | None:
    """Parse one daily JSONL file and return {date, median_premium_pct,
    products_used, spot_usd_toz} or None if unparsable."""
    meta_spot: float | None = None
    per_product_pct: dict[str, list[float]] = defaultdict(list)
    if not path.exists():
        return None
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "meta":
                    spot = obj.get("spot_gcf_usd")
                    if isinstance(spot, (int, float)) and spot > 0:
                        meta_spot = float(spot)
                    continue
                if obj.get("type") != "product":
                    continue
                if obj.get("site") not in ("sdbullion",):  # v1 basket
                    continue
                if obj.get("status") != 200:
                    continue
                if meta_spot is None or meta_spot <= 0:
                    continue
                key = obj.get("key") or obj.get("url") or ""
                # Base 1oz product: filter prices to reasonable premium band
                lo = meta_spot * 1.005  # at least +0.5% (below = accessory)
                hi = meta_spot * 1.30   # at most +30% (above = collectible)
                candidates = [p for p in (obj.get("prices_usd") or []) if lo <= float(p) <= hi]
                if not candidates:
                    continue
                base_price = min(candidates)  # cheapest tier / payment method
                premium_pct = (base_price - meta_spot) / meta_spot * 100
                per_product_pct[key].append(premium_pct)
    except OSError:
        return None
    if not per_product_pct:
        return None
    # For each product, keep the MEDIAN of the day's captures (multiple runs).
    per_product_median = [_median(v) for v in per_product_pct.values()]
    day_median = _median(per_product_median)
    d = date.fromisoformat(path.stem)
    return {
        "date": d,
        "median_premium_pct": day_median,
        "products_used": len(per_product_median),
        "spot_usd_toz": meta_spot,
    }


def _load_premium_history() -> list[dict]:
    if not PREMIUMS.exists():
        return []
    out = []
    for p in sorted(PREMIUMS.glob("*.jsonl")):
        parsed = _parse_premium_day(p)
        if parsed:
            out.append(parsed)
    return out


def pillar_premiums(as_of: date, premium_hist: list[dict]) -> dict:
    label = "premiums"
    if not premium_hist:
        return {"name": label, "available": False, "reason": "no premium capture yet"}
    # Latest premium row <= as_of
    hits = [row for row in premium_hist if row["date"] <= as_of]
    if not hits:
        return {"name": label, "available": False, "reason": "no data <= as_of"}
    latest = hits[-1]
    stale = staleness_days(latest["date"], as_of)
    if stale > STALENESS_LIMITS_DAYS["daily"]:
        return {"name": label, "available": False, "reason": f"stale ({stale}d)",
                "latest_date": latest["date"].isoformat()}
    history_days = len(hits)
    dist = [r["median_premium_pct"] for r in hits]
    if history_days < MIN_HISTORY_POINTS_PREMIUMS:
        # Not enough history for a meaningful percentile → treat pillar as
        # unavailable so the ramp+drop logic redistributes its weight.
        return {
            "name": label,
            "available": False,
            "reason": f"insufficient history ({history_days} days < {MIN_HISTORY_POINTS_PREMIUMS})",
        "source": "The Gold Barometer premium collector (7-dealer basket)",
        "frequency": "daily",
        "ramping": True,
        "min_history_days": MIN_HISTORY_POINTS_PREMIUMS,
            "latest_date": latest["date"].isoformat(),
            "latest_premium_pct": round(latest["median_premium_pct"], 3),
            "history_days": history_days,
            "note": "Pillar is being observed but not scored until 90-day history threshold is met.",
        }
    pct = percentile_rank(latest["median_premium_pct"], dist)
    subscore = 100.0 - pct  # high premium → low sub-score
    return _pillar_output(label, subscore,
                          latest_date=latest["date"], latest_value=latest["median_premium_pct"],
                          unit="%",
                          source="The Gold Barometer Retail Premium Index (own daily capture, SD Bullion)",
                          frequency="daily", staleness=stale,
                          stale_limit=STALENESS_LIMITS_DAYS["daily"],
                          history_points=history_days,
                          publishable_raw=False,
                          notes="Published as a derived aggregate only (DATA-RIGHTS).")


# ---------------------------------------------------------------------------
# Composite: renormalize weights, apply ramp, compose
# ---------------------------------------------------------------------------

def resolve_premium_weight(as_of: date, premium_available: bool,
                           premium_history_days: int) -> float:
    """Return the effective NOMINAL weight for premiums (before renormalization).

    Rules (task spec):
      - < 90 days of premium history  → 0  (pillar dropped, weights redistributed)
      - 90 days ≤ history < 12 months → 5  (ramp weight)
      - ≥ 12 months                   → 10 (full weight)
    """
    if not premium_available or premium_history_days < PREMIUM_RAMP_MIN_DAYS:
        return 0.0
    # Continuous ramp (decided 2026-08-12, BEFORE first effect ~Nov 2026): a
    # weight step of several points in one day would move the composite
    # mechanically and could manufacture a zone change. The weight now grows
    # by ~0.02/day: 0 -> 5 between day 90 and one year, 5 -> 10 between one
    # and two years. Published on /methodology/ before taking effect.
    d = premium_history_days
    if d < PREMIUM_FULL_WEIGHT_DAYS:
        return round(5.0 * (d - PREMIUM_RAMP_MIN_DAYS) / (PREMIUM_FULL_WEIGHT_DAYS - PREMIUM_RAMP_MIN_DAYS), 2)
    return round(min(10.0, 5.0 + 5.0 * (d - PREMIUM_FULL_WEIGHT_DAYS) / 365.0), 2)


def renormalize(nominal_weights: dict[str, float], available: dict[str, bool]) -> dict[str, float]:
    """Renormalize so effective weights of AVAILABLE pillars sum to 100.

    Dropped pillars get effective weight 0. Preserves the RELATIVE proportions
    of the surviving pillars.
    """
    total = sum(w for k, w in nominal_weights.items() if available.get(k))
    if total <= 0:
        return {k: 0.0 for k in nominal_weights}
    scale = 100.0 / total
    return {k: (w * scale if available.get(k) else 0.0) for k, w in nominal_weights.items()}


def score_zone(score: int) -> str:
    for lo, hi, name in ZONES:
        if lo <= score <= hi:
            return name
    return "Unknown"


def _pillar_output(name: str, subscore: float, *, latest_date: date, latest_value,
                   unit: str, source: str, frequency: str, staleness: int,
                   stale_limit: int, history_points: int, publishable_raw: bool,
                   notes: str | None = None) -> dict:
    out = {
        "name": name,
        "available": True,
        "subscore": round(cap_subscore(subscore), 2),
        "latest_date": latest_date.isoformat(),
        "latest_value": (None if latest_value is None
                         else (round(latest_value, 4)
                               if isinstance(latest_value, float) else latest_value)),
        "unit": unit,
        "source": source,
        "frequency": frequency,
        "staleness_days": staleness,
        "stale_limit_days": stale_limit,
        "history_points": history_points,
        "publishable_raw": publishable_raw,
    }
    if notes:
        out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# Top-level composite computation
# ---------------------------------------------------------------------------

def compute_composite(as_of: date, bundle: dict, spot_now: float | None = None,
                      premium_history: list[dict] | None = None,
                      allow_missing_premium: bool = True) -> dict:
    pillars = {
        "real_rates": pillar_real_rates(as_of, bundle["tips"], bundle["real_rate_proxy"]),
        "entry_price": pillar_entry_price(as_of, bundle["wb_gold"], bundle["cpi"], spot_now),
        "structural_demand": pillar_structural_demand(as_of, bundle["cb_sum"], bundle["gld"]),
        "dollar": pillar_dollar(as_of, bundle["dollar"]),
        "positioning": pillar_positioning(as_of, bundle["cot_pct_oi"]),
        "volatility": pillar_volatility(as_of, bundle["gvz"]),
        "premiums": pillar_premiums(as_of, premium_history or []),
    }
    available = {k: p.get("available", False) for k, p in pillars.items()}
    # Ramp for premiums (weight based on history days)
    premium_hist_days = len(premium_history or [])
    nominal = dict(BASE_WEIGHTS)
    nominal["premiums"] = resolve_premium_weight(as_of, available["premiums"], premium_hist_days)
    # Force-drop premiums if nominal weight is 0
    if nominal["premiums"] == 0:
        available["premiums"] = False
    effective = renormalize(nominal, available)

    # Weighted composite
    weighted_sum = 0.0
    for k, p in pillars.items():
        if available[k]:
            weighted_sum += effective[k] * p["subscore"]
    composite = weighted_sum / 100.0  # since effective weights sum to 100
    score_int = int(round(composite))
    zone = score_zone(score_int)

    pillar_out = {}
    for k, p in pillars.items():
        entry = dict(p)
        entry["weight_nominal"] = nominal[k]
        entry["weight_effective"] = round(effective[k], 4)
        entry["evidence_grade"] = EVIDENCE_GRADES[k]
        # Flag staleness at pillar level (green/yellow/red-ish)
        if entry.get("available"):
            stale = entry.get("staleness_days", 0)
            limit = entry.get("stale_limit_days", 0) or 1
            if stale > limit:
                entry["staleness_flag"] = "over_limit"
            elif stale > 0.5 * limit:
                entry["staleness_flag"] = "aging"
            else:
                entry["staleness_flag"] = "fresh"
        else:
            entry["staleness_flag"] = "dropped"
        pillar_out[k] = entry

    return {
        "date": as_of.isoformat(),
        "score": score_int,
        "score_precise": round(composite, 3),
        "zone": zone,
        "version": VERSION,
        "engine_build": ENGINE_BUILD,
        # Data-shape contract for embedded clients. Fields may be ADDED, never
        # removed or renamed. Bump only on a breaking change, which must be
        # avoided: third-party pages run whatever widget.js they last cached.
        "api_version": 1,
        "pillars_used": sum(1 for v in available.values() if v),
        "pillars_total": len(pillars),
        "weights_nominal": nominal,
        "weights_effective": {k: round(v, 4) for k, v in effective.items()},
        "pillars": pillar_out,
    }


# ---------------------------------------------------------------------------
# Backfill (monthly cadence) — engine output, distinct from step-4 backtest
# ---------------------------------------------------------------------------

def month_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while True:
        # Last day of (y, m)
        if m == 12:
            nxt = date(y + 1, 1, 1)
        else:
            nxt = date(y, m + 1, 1)
        d = nxt - timedelta(days=1)
        if d > end:
            break
        if d >= start:
            out.append(d)
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return out


def backfill_history(bundle: dict, today: date) -> list[dict]:
    """Compute the composite at monthly cadence for the entire window where
    all 6 non-premium pillars are simultaneously available (min 10y history)."""
    # Earliest date where all 6 non-premium pillars have min history:
    # constrained by GVZ (~2019, 2009-09-18 + ~10y) and dollar (~2016).
    start = date(2019, 10, 1)
    ends = month_ends(start, today)
    rows = []
    for d in ends:
        r = compute_composite(d, bundle, spot_now=None, premium_history=[])
        r["backfilled"] = True
        rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# Unit-style self-checks
# ---------------------------------------------------------------------------

def run_self_checks() -> dict:
    checks = {}

    # 1) percentile_rank
    dist = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    checks["percentile_rank_middle"] = ("PASS"
        if abs(percentile_rank(5.5, dist) - 50.0) < 1e-9 else "FAIL")
    checks["percentile_rank_min"] = ("PASS"
        if percentile_rank(0, dist) == 0.0 else "FAIL")
    checks["percentile_rank_max"] = ("PASS"
        if percentile_rank(11, dist) == 100.0 else "FAIL")
    checks["percentile_rank_ties"] = ("PASS"
        if abs(percentile_rank(5, [5, 5, 5, 5]) - 50.0) < 1e-9 else "FAIL")
    checks["percentile_rank_empty"] = ("PASS"
        if percentile_rank(1, []) == 50.0 else "FAIL")

    # 2) renormalization
    all_in = renormalize({"a": 25, "b": 25, "c": 50},
                         {"a": True, "b": True, "c": True})
    checks["renorm_full"] = ("PASS"
        if all(abs(all_in[k] - w) < 1e-9 for k, w in {"a": 25, "b": 25, "c": 50}.items())
        else f"FAIL {all_in}")
    dropped = renormalize({"a": 25, "b": 25, "c": 50},
                          {"a": True, "b": True, "c": False})
    checks["renorm_drop"] = ("PASS"
        if abs(dropped["a"] - 50) < 1e-9 and abs(dropped["b"] - 50) < 1e-9
        and dropped["c"] == 0.0 else f"FAIL {dropped}")
    # 6-of-7 with premiums dropped: 25/20/15/10/10/10 → each × 100/90
    base_drop = renormalize(BASE_WEIGHTS, {"real_rates": True, "entry_price": True,
        "structural_demand": True, "dollar": True, "positioning": True,
        "volatility": True, "premiums": False})
    expected = 25 * 100 / 90
    checks["renorm_6of7"] = ("PASS"
        if abs(base_drop["real_rates"] - expected) < 1e-9
        and abs(sum(base_drop.values()) - 100) < 1e-9 else f"FAIL {base_drop}")

    # 3) ramp logic
    checks["ramp_pre90"] = ("PASS"
        if resolve_premium_weight(date.today(), True, 30) == 0.0 else "FAIL")
    checks["ramp_90_boundary"] = ("PASS"
        if resolve_premium_weight(date.today(), True, 90) == 0.0 else "FAIL")
    checks["ramp_one_year"] = ("PASS"
        if resolve_premium_weight(date.today(), True, 365) == 5.0 else "FAIL")
    checks["ramp_two_years"] = ("PASS"
        if resolve_premium_weight(date.today(), True, 730) == 10.0 else "FAIL")
    checks["ramp_monotonic"] = ("PASS"
        if all(resolve_premium_weight(date.today(), True, d)
               <= resolve_premium_weight(date.today(), True, d + 1)
               for d in range(88, 732)) else "FAIL")
    checks["ramp_unavailable"] = ("PASS"
        if resolve_premium_weight(date.today(), False, 400) == 0.0 else "FAIL")

    # 4) zone mapping
    checks["zone_100"] = "PASS" if score_zone(100) == "Historically very favorable" else "FAIL"
    checks["zone_80"] = "PASS" if score_zone(80) == "Historically very favorable" else "FAIL"
    checks["zone_79"] = "PASS" if score_zone(79) == "Favorable" else "FAIL"
    checks["zone_50"] = "PASS" if score_zone(50) == "Mixed" else "FAIL"
    checks["zone_20"] = "PASS" if score_zone(20) == "Unfavorable" else "FAIL"
    checks["zone_0"] = "PASS" if score_zone(0) == "Historically very unfavorable" else "FAIL"

    # 5) median_smooth
    ms = median_smooth([(date(2020, 1, i + 1), v) for i, v in enumerate([10, 20, 30, 40, 50])], window=3)
    got = [round(v, 4) for _, v in ms]
    checks["median_smooth_3"] = "PASS" if got == [10.0, 15.0, 20.0, 30.0, 40.0] else f"FAIL {got}"

    checks["_all_pass"] = "PASS" if all(v == "PASS" for k, v in checks.items() if k != "_all_pass") else "FAIL"
    return checks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_bundle() -> dict:
    return {
        "tips": load_series(HISTORY / "tips_10y.csv"),
        "real_rate_proxy": load_series(HISTORY / "real_rate_proxy.csv"),
        "wb_gold": load_series(HISTORY / "wb_gold_usd_toz.csv"),
        "cpi": load_series(HISTORY / "cpi.csv"),
        "cb_sum": load_series(SERIES / "cb_gold_reporters_sum.csv"),
        "gld": load_series(HISTORY / "gld_tonnes.csv"),
        "dollar": load_series(HISTORY / "dollar_broad.csv"),
        "cot_pct_oi": load_series(HISTORY / "cot_mm_net_pct_oi.csv"),
        "gvz": load_series(HISTORY / "gvz.csv"),
    }


def load_todays_spot() -> float | None:
    p = SERIES / "spot_usd_toz.csv"
    rows = load_series(p)
    if rows:
        return rows[-1][1]
    return None


def render_breakdown_table(latest: dict) -> str:
    lines = []
    header = ("| # | Pillar | Sub-score | Nominal | Effective | Latest date | Stale | Grade |")
    sep = "|---|---|---|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)
    order = ["real_rates", "entry_price", "structural_demand", "dollar",
             "positioning", "volatility", "premiums"]
    for i, key in enumerate(order, 1):
        p = latest["pillars"][key]
        if p.get("available"):
            sub = f"{p['subscore']:.2f}"
            latest_date = p.get("latest_date", "n/a")
            stale = f"{p.get('staleness_days', '?')}d"
        else:
            sub = "— (dropped)"
            latest_date = p.get("latest_date", "n/a")
            stale = p.get("reason", "n/a")
        wn = p.get("weight_nominal", 0)
        we = p.get("weight_effective", 0)
        grade = p.get("evidence_grade", "").split(" (")[0]
        lines.append(f"| {i} | {key} | {sub} | {wn} | {we:.2f} | {latest_date} | {stale} | {grade} |")
    return "\n".join(lines)


def main():
    today = date.today()
    bundle = load_bundle()
    spot_now = load_todays_spot()
    premium_history = _load_premium_history()

    print(f"compute_score.py v{VERSION} — as of {today.isoformat()}")
    print(f"  Loaded {sum(len(v) for v in bundle.values())} series rows "
          f"across {len(bundle)} inputs")
    print(f"  Live spot (internal fallback): {spot_now}")
    print(f"  Premium history days: {len(premium_history)}")

    latest = compute_composite(today, bundle, spot_now=spot_now,
                               premium_history=premium_history)

    print()
    print(f"  COMPOSITE: {latest['score']}/100 ({latest['zone']}) — "
          f"pillars_used={latest['pillars_used']}/{latest['pillars_total']}")
    print()
    print(render_breakdown_table(latest))
    print()

    # Backfill monthly composite
    backfill = backfill_history(bundle, today)
    print(f"  Backfilled monthly composite: {len(backfill)} months "
          f"({backfill[0]['date']} → {backfill[-1]['date']})" if backfill else "  Backfill: 0 months")

    # Merge latest into the history stream (latest = today, non-backfilled).
    # Every PUBLISHED daily reading is rehydrated from the immutable public
    # archive first: the daily series is the only source of events, and this
    # write must never erase it again (it did until 2026-08-12, which made
    # the home page contradict the archive).
    all_rows = list(backfill)
    pub_dir = ROOT / "public-data" / "data"
    if pub_dir.exists():
        for f in sorted(pub_dir.glob("*/*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            d = rec.get("date")
            sc = rec.get("score")
            # The public archive is a writable surface (a bad manual commit
            # would otherwise flow straight into the site every night):
            # validate before trusting (wave-3 review, 2026-08-12).
            if (not d or d == latest["date"] or d > latest["date"]
                    or rec.get("zone") not in {z[2] for z in ZONES}
                    or not isinstance(sc, int) or not (0 <= sc <= 100)):
                continue
            all_rows.append({
                "date": d,
                "score": rec.get("score"),
                "score_precise": rec.get("score_precise"),
                "zone": rec.get("zone"),
                "version": rec.get("version", VERSION),
                "engine_build": rec.get("engine_build", "pre-2026-08-12"),
                "pillars_used": rec.get("pillars_used"),
                "backfilled": False,
            })
    all_rows.append({**latest, "backfilled": False})
    all_rows.sort(key=lambda r: (r["date"], 0 if r.get("backfilled") else 1))
    deduped = {}
    for r in all_rows:
        prev = deduped.get(r["date"])
        if prev is None or (prev.get("backfilled") and not r.get("backfilled")):
            deduped[r["date"]] = r
    all_rows = [deduped[k] for k in sorted(deduped)]

    # --- Write outputs ---
    latest_out = OUTPUT / "latest.json"
    latest_out.write_text(json.dumps(latest, indent=2))
    print(f"  → wrote {latest_out.relative_to(ROOT)}")

    hist_json = OUTPUT / "history.json"
    hist_json.write_text(json.dumps(
        [{"date": r["date"], "score": r["score"], "zone": r["zone"],
          "version": r["version"], "pillars_used": r["pillars_used"],
          "backfilled": r.get("backfilled", False)} for r in all_rows], indent=2))
    print(f"  → wrote {hist_json.relative_to(ROOT)}")

    hist_csv = OUTPUT / "history.csv"
    with hist_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "score", "zone", "version", "pillars_used", "backfilled"])
        for r in all_rows:
            w.writerow([r["date"], r["score"], r["zone"], r["version"],
                        r["pillars_used"], str(r.get("backfilled", False)).lower()])
    print(f"  → wrote {hist_csv.relative_to(ROOT)}")

    checks = run_self_checks()
    checks_out = OUTPUT / "checks.json"
    checks_out.write_text(json.dumps(checks, indent=2))
    print(f"  → wrote {checks_out.relative_to(ROOT)} ({checks['_all_pass']})")
    if checks["_all_pass"] != "PASS":
        for k, v in checks.items():
            if v != "PASS" and k != "_all_pass":
                print(f"    !! {k}: {v}")
        return 2

    # Final integrity: no raw GVZ leaked into outputs
    for path in [latest_out, hist_json, hist_csv]:
        text = path.read_text()
        pillar = latest["pillars"].get("volatility", {})
        # We disallow "latest_value" being non-null for volatility in outputs
        if pillar.get("available") and "latest_value" in pillar and pillar["latest_value"] is not None:
            print(f"  !! DATA-RIGHTS violation: raw GVZ leaked in {path}")
            return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
