#!/usr/bin/env python3
"""Compute the evidence for the question-cluster pages.

Inputs (never modified):
  data/backtest/monthly_scores.csv   scores + point-in-time forward returns
  data/history/wb_gold_usd_toz.csv   World Bank monthly gold, 1960+

Output:
  data/backtest/question_stats.json  consumed by the question pages at build
                                     time, so every figure is computed.

Forward-return columns in monthly_scores are FRACTIONS (0.21 = 21%), per the
documented trap. Everything this script emits is in PERCENT.
"""
import csv
import json
import statistics
from datetime import date
from pathlib import Path

import os
ROOT = Path(os.environ.get("TGB_DATA_DIR", str(Path.home() / "projects" / "thegoldbarometer")))
OUT = ROOT / "data" / "backtest" / "question_stats.json"

# ---------------------------------------------------------------- load
prices = {}  # iso month-end -> usd/toz
with open(ROOT / "data" / "history" / "wb_gold_usd_toz.csv") as f:
    for r in csv.DictReader(f):
        prices[r["date"]] = float(r["value"])

rows = []
with open(ROOT / "data" / "backtest" / "monthly_scores.csv") as f:
    for r in csv.DictReader(f):
        rows.append(r)

dates = [r["date"] for r in rows]
scores = [int(r["score"]) for r in rows]
price_seq = [prices.get(d) for d in dates]
missing = [d for d, p in zip(dates, price_seq) if p is None]
if missing:
    raise SystemExit(f"price missing for {len(missing)} months, first: {missing[:3]}")


def frac_pct(r, col):
    v = r.get(col, "")
    return None if v in ("", None) else float(v) * 100.0


def med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


ZONES = [
    ("Historically very favorable", "80-100", 80, 100),
    ("Favorable", "60-79", 60, 79),
    ("Mixed", "40-59", 40, 59),
    ("Unfavorable", "20-39", 20, 39),
    ("Historically very unfavorable", "0-19", 0, 19),
]

# ---------------------------------------------------------------- 1. waiting
# From every month in a below-Mixed zone: how long until the score first reads
# Mixed or better (>= 40), what the price did during that wait, and what share
# of months never saw better conditions within five years.
wait_zones = []
for zone, band, lo, hi in ZONES:
    idxs = [i for i, s in enumerate(scores) if lo <= s <= hi]
    if not idxs or lo >= 40:
        continue
    waits, moves = [], []
    n_reach_1y = n_reach_5y = n_with_5y_window = 0
    for i in idxs:
        j = next((k for k in range(i + 1, len(scores)) if scores[k] >= 40), None)
        horizon_full = (len(scores) - 1 - i) >= 60
        if horizon_full:
            n_with_5y_window += 1
        if j is not None:
            w = j - i
            waits.append(w)
            moves.append((price_seq[j] / price_seq[i] - 1) * 100.0)
            if w <= 12:
                n_reach_1y += 1
            if w <= 60 and horizon_full:
                n_reach_5y += 1
    wait_zones.append({
        "zone": zone, "band": band, "n_months": len(idxs),
        "n_reached_better": len(waits),
        "median_wait_months": med([float(w) for w in waits]),
        "median_price_change_while_waiting_pct": med(moves),
        "share_reached_within_1y_pct": round(100.0 * n_reach_1y / len(idxs), 1),
        "share_reached_within_5y_pct": (
            round(100.0 * n_reach_5y / n_with_5y_window, 1) if n_with_5y_window else None),
        "n_with_full_5y_window": n_with_5y_window,
    })

# ---------------------------------------------------------------- 2. record highs
# "Too late" months: price within 5% of its own running record (1971+ scores
# window). Forward returns come from monthly_scores point-in-time columns.
NEAR = 0.95
runmax = 0.0
near_rows, other_rows = [], []
for i, r in enumerate(rows):
    runmax = max(runmax, price_seq[i])
    (near_rows if price_seq[i] >= NEAR * runmax else other_rows).append(r)


def fwd_stats(sub):
    return {
        "n_months": len(sub),
        "median_fwd_1y_nominal_pct": med([frac_pct(r, "fwd_1y_nominal") for r in sub]),
        "median_fwd_5y_nominal_pct": med([frac_pct(r, "fwd_5y_nominal") for r in sub]),
        "median_fwd_5y_real_pct": med([frac_pct(r, "fwd_5y_real") for r in sub]),
        "median_worst_fall_5y_pct": med([frac_pct(r, "maxdd_5y") for r in sub]),
    }


ath = {
    "threshold_pct": 5,
    "near_record": fwd_stats(near_rows),
    "away_from_record": fwd_stats(other_rows),
}

# ---------------------------------------------------------------- 3. seasonality
# Month-over-month price change by calendar month, 1971+.
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
by_month = {m: [] for m in range(1, 13)}
for i in range(1, len(rows)):
    m = int(dates[i][5:7])
    by_month[m].append((price_seq[i] / price_seq[i - 1] - 1) * 100.0)
season = [{
    "month": m, "name": MONTH_NAMES[m - 1], "n": len(v),
    "median_mom_pct": med(v),
    "share_positive_pct": round(100.0 * sum(1 for x in v if x > 0) / len(v), 1),
} for m, v in by_month.items()]
best = max(season, key=lambda s: s["median_mom_pct"])
worst = min(season, key=lambda s: s["median_mom_pct"])
seasonality = {
    "months": season,
    "best": best, "worst": worst,
    "spread_pct": round(best["median_mom_pct"] - worst["median_mom_pct"], 1),
}

# ---------------------------------------------------------------- 4. deep falls
# Episodes where the monthly close fell 20%+ from its running peak (1971+).
# An episode opens at the first month 20% under the peak ("cross"), bottoms at
# its lowest close ("trough"), and closes when the peak is regained.
TH = 0.80
episodes = []
peak = price_seq[0]
peak_i = 0
in_ep = False
cross_i = trough_i = None
for i in range(1, len(price_seq)):
    p = price_seq[i]
    if not in_ep:
        if p > peak:
            peak, peak_i = p, i
        elif p <= peak * TH:
            in_ep, cross_i, trough_i = True, i, i
    else:
        if p < price_seq[trough_i]:
            trough_i = i
        if p >= peak:
            episodes.append((peak_i, cross_i, trough_i, i))
            peak, peak_i, in_ep = p, i, False
if in_ep:
    episodes.append((peak_i, cross_i, trough_i, None))


def fwd_from(i, months):
    j = i + months
    return round((price_seq[j] / price_seq[i] - 1) * 100.0, 1) if j < len(price_seq) else None


ep_out = []
for peak_i, cross_i, trough_i, end_i in episodes:
    ep_out.append({
        "peak_month": dates[peak_i], "cross_month": dates[cross_i],
        "trough_month": dates[trough_i],
        "recovered_month": dates[end_i] if end_i is not None else None,
        "depth_pct": round((price_seq[trough_i] / price_seq[peak_i] - 1) * 100.0, 1),
        "further_fall_after_cross_pct": round((price_seq[trough_i] / price_seq[cross_i] - 1) * 100.0, 1),
        "months_cross_to_trough": trough_i - cross_i,
        "fwd_from_cross_1y_pct": fwd_from(cross_i, 12),
        "fwd_from_cross_5y_pct": fwd_from(cross_i, 60),
    })
dips = {
    "threshold_pct": 20,
    "n_episodes": len(ep_out),
    "episodes": ep_out,
    "median_further_fall_after_cross_pct": med([e["further_fall_after_cross_pct"] for e in ep_out]),
    "median_months_cross_to_trough": med([float(e["months_cross_to_trough"]) for e in ep_out]),
    "median_fwd_from_cross_1y_pct": med([e["fwd_from_cross_1y_pct"] for e in ep_out]),
    "median_fwd_from_cross_5y_pct": med([e["fwd_from_cross_5y_pct"] for e in ep_out]),
}

# ---------------------------------------------------------------- write
out = {
    "note": "Computed by scripts/backtest/question_stats.py from monthly_scores.csv and wb_gold_usd_toz.csv. All figures in percent. Nominal unless the key says real.",
    "record_months": len(rows),
    "first_month": dates[0], "last_month": dates[-1],
    "wait": {"better_means": "score of 40 or higher (Mixed or better)", "zones": wait_zones},
    "record_highs": ath,
    "seasonality": seasonality,
    "deep_falls": dips,
}
OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
print(json.dumps({k: out[k] for k in ("record_months", "first_month", "last_month")}))
print("wait zones:", [(z["zone"], z["median_wait_months"], z["median_price_change_while_waiting_pct"], z["share_reached_within_5y_pct"]) for z in wait_zones])
print("ath near:", ath["near_record"])
print("ath away:", ath["away_from_record"])
print("season best/worst:", best["name"], best["median_mom_pct"], "/", worst["name"], worst["median_mom_pct"])
print("dips:", dips["n_episodes"], "episodes; further fall", dips["median_further_fall_after_cross_pct"], "; fwd5y", dips["median_fwd_from_cross_5y_pct"])
