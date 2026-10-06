#!/usr/bin/env python3
"""Compute the evidence for the question-cluster pages.

Inputs (never modified):
  data/backtest/monthly_scores.csv   scores + point-in-time forward returns
  data/history/wb_gold_usd_toz.csv   World Bank monthly average gold price, 1960+
  data/history/wb_silver_usd_toz.csv World Bank monthly average silver price, 1960+
  data/history/cpi.csv               BLS CPI-U, for the figures after inflation

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
# Root from the script's own location (scripts/backtest/), TGB_DATA_DIR
# overrides. A $HOME-based default held here until 2026-09-11: same defect
# class as the Windows path that froze the site on 2026-09-06.
ROOT = Path(os.environ.get("TGB_DATA_DIR") or Path(__file__).resolve().parents[2])
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
# Episodes where the monthly average price fell 20%+ from its running peak
# (1971+). An episode opens at the first month 20% under the peak ("cross"),
# bottoms at its lowest month ("trough"), and closes when the peak is regained.
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

# ---------------------------------------------------------------- CPI (for the two record tables)
# BLS CPI-U, the series the backtest already deflates with. One month is
# missing (2025-10, no release during the shutdown): a month without a value
# takes the last one published before it, and the pages say so.
cpi = {}
with open(ROOT / "data" / "history" / "cpi.csv") as f:
    for r in csv.DictReader(f):
        if r["value"] not in ("", None):
            cpi[r["date"]] = float(r["value"])
cpi_seq, cpi_filled, last_cpi = [], [], None
for d in dates:
    if d in cpi:
        last_cpi = cpi[d]
    else:
        cpi_filled.append(d)
    cpi_seq.append(last_cpi)
if cpi_seq[0] is None:
    raise SystemExit("CPI missing at the start of the record")
real_seq = [p / c for p, c in zip(price_seq, cpi_seq)]


def score_zone(i):
    s = scores[i]
    return s, next(z for z, _, lo, hi in ZONES if lo <= s <= hi)


# ---------------------------------------------------------------- 5. peaks
# The same five episodes as the deep falls, read from the peak: how many
# months from the peak to the lowest month, how many until the monthly average
# price stood at or above the peak again, before inflation and after it (first
# month after the low whose price divided by CPI reaches the peak's), and what
# the barometer read at the peak and at the low. null = not yet.
peaks_out = []
for peak_i, cross_i, trough_i, end_i in episodes:
    # searched after the low, like the nominal recovery of the deep falls
    real_end = next((j for j in range(trough_i + 1, len(real_seq)) if real_seq[j] >= real_seq[peak_i]), None)
    s_peak, z_peak = score_zone(peak_i)
    s_trough, z_trough = score_zone(trough_i)
    peaks_out.append({
        "peak_month": dates[peak_i], "peak_price": round(price_seq[peak_i], 2),
        "trough_month": dates[trough_i], "trough_price": round(price_seq[trough_i], 2),
        "depth_pct": round((price_seq[trough_i] / price_seq[peak_i] - 1) * 100.0, 1),
        "months_peak_to_trough": trough_i - peak_i,
        "recovered_month": dates[end_i] if end_i is not None else None,
        "months_to_recover_nominal": (end_i - peak_i) if end_i is not None else None,
        "recovered_real_month": dates[real_end] if real_end is not None else None,
        "months_to_recover_real": (real_end - peak_i) if real_end is not None else None,
        "score_at_peak": s_peak, "zone_at_peak": z_peak,
        "score_at_trough": s_trough, "zone_at_trough": z_trough,
    })
run_i = max(range(len(price_seq)), key=lambda i: price_seq[i])
peaks = {
    "threshold_pct": 20,
    "n_peaks": len(peaks_out),
    "peaks": peaks_out,
    "median_months_to_recover_nominal": med([float(p["months_to_recover_nominal"]) for p in peaks_out if p["months_to_recover_nominal"] is not None]),
    "median_months_to_recover_real": med([float(p["months_to_recover_real"]) for p in peaks_out if p["months_to_recover_real"] is not None]),
    "since_last_peak": {
        "peak_month": dates[run_i], "peak_price": round(price_seq[run_i], 2),
        "last_month": dates[-1], "last_price": round(price_seq[-1], 2),
        "gap_to_peak_pct": round((price_seq[-1] / price_seq[run_i] - 1) * 100.0, 1),
        "months_since_peak": len(price_seq) - 1 - run_i,
        "in_episode": in_ep,
    },
    "cpi_months_filled": cpi_filled,
}

# ---------------------------------------------------------------- 6. purchase years
# One row per full calendar year of the record: $10,000 of gold bought at the
# year's average monthly price, valued at the last monthly average price we
# hold, before inflation and after it (each price divided by its own CPI, the
# year's average CPI for the purchase). Also the worst monthly average after
# the year relative to the price paid, and the longest run of months spent
# below it. The current year is left out until it is complete.
STAKE = 10000.0
now_i = len(price_seq) - 1
price_now, cpi_now = price_seq[now_i], cpi_seq[now_i]
years = sorted({int(d[:4]) for d in dates})
last_full_year = years[-1] if dates[-1][5:7] == "12" else years[-1] - 1
years_out = []
for y in range(years[0], last_full_year + 1):
    idxs = [i for i, d in enumerate(dates) if int(d[:4]) == y]
    if len(idxs) != 12:
        continue
    paid = statistics.mean(price_seq[i] for i in idxs)
    paid_real = statistics.mean(real_seq[i] for i in idxs)
    after = list(range(idxs[-1] + 1, len(price_seq)))
    lows = [price_seq[i] / paid - 1 for i in after]
    worst_after = min(lows) if lows else 0.0
    longest = run = 0
    for i in after:
        run = run + 1 if price_seq[i] < paid else 0
        longest = max(longest, run)
    s_avg = statistics.mean(scores[i] for i in idxs)
    s_round = int(round(s_avg))
    years_out.append({
        "year": y, "price_paid": round(paid, 2),
        "value_now_nominal": round(STAKE * price_now / paid),
        "gain_nominal_pct": round((price_now / paid - 1) * 100.0, 1),
        "value_now_real": round(STAKE * (price_now / cpi_now) / paid_real),
        "gain_real_pct": round(((price_now / cpi_now) / paid_real - 1) * 100.0, 1),
        "worst_drop_pct": round(min(worst_after, 0.0) * 100.0, 1),
        "longest_stretch_below_months": longest,
        "score_year_avg": s_round,
        "zone_year": next(z for z, _, lo, hi in ZONES if lo <= s_round <= hi),
    })
ahead_real = [r for r in years_out if r["gain_real_pct"] > 0]
behind_real = [r for r in years_out if r["gain_real_pct"] <= 0]
purchase_years = {
    "stake_usd": int(STAKE),
    "price_now": round(price_now, 2), "price_now_month": dates[now_i],
    "cpi_now_month": dates[now_i],
    "years": years_out,
    "years_total": len(years_out),
    "first_year": years_out[0]["year"], "last_year": years_out[-1]["year"],
    "years_ahead_nominal": sum(1 for r in years_out if r["gain_nominal_pct"] > 0),
    "years_ahead_real": len(ahead_real),
    "years_behind_real": [r["year"] for r in behind_real],
    "median_gain_nominal_pct": med([r["gain_nominal_pct"] for r in years_out]),
    "median_gain_real_pct": med([r["gain_real_pct"] for r in years_out]),
    "median_value_now_real": round(statistics.median(r["value_now_real"] for r in years_out)),
    "worst_year_real": min(years_out, key=lambda r: r["gain_real_pct"])["year"],
    "best_year_real": max(years_out, key=lambda r: r["gain_real_pct"])["year"],
    "median_longest_stretch_below_months": med([float(r["longest_stretch_below_months"]) for r in years_out]),
    "longest_stretch_below_months_max": max(r["longest_stretch_below_months"] for r in years_out),
    "cpi_months_filled": cpi_filled,
}

# ---------------------------------------------------------------- 7. price against its own past
# For the question "is gold expensive right now": where the month's price sat
# against every earlier month after inflation, the way the entry price part
# measures it (compute_score.pillar_entry_price: price divided by the CPI of
# its month, midrank percentile inside the expanding window, World Bank series
# from 1960). Then what followed from months in each band, using the
# point-in-time forward columns of monthly_scores.
all_prices = []  # (iso, usd) from 1960, the same window compute_score uses
with open(ROOT / "data" / "history" / "wb_gold_usd_toz.csv") as f:
    for r in csv.DictReader(f):
        all_prices.append((r["date"], float(r["value"])))
all_real = []
for d, p in all_prices:
    c = cpi.get(d)
    if c and c > 0:
        all_real.append((d, p / c))


def midrank_pct(value, dist):
    below = sum(1 for v in dist if v < value)
    equal = sum(1 for v in dist if v == value)
    return 100.0 * (below + 0.5 * equal) / len(dist)


real_by_date = {d: v for d, v in all_real}
real_order = [d for d, _ in all_real]
pct_by_month = {}
for k, d in enumerate(real_order):
    if d < dates[0]:
        continue
    pct_by_month[d] = midrank_pct(real_by_date[d], [v for _, v in all_real[: k + 1]])

BANDS = [
    ("Top tenth", 90, 100.01), ("75 to 90", 75, 90), ("50 to 75", 50, 75), ("25 to 50", 25, 50), ("Bottom quarter", 0, 25),
]


def band_of(pct):
    return next(name for name, lo, hi in BANDS if lo <= pct < hi)


band_rows = {name: [] for name, _, _ in BANDS}
for r in rows:
    pct = pct_by_month.get(r["date"])
    if pct is None:
        continue  # a month without CPI cannot be placed
    band_rows[band_of(pct)].append(r)


def share_positive(vals):
    vals = [v for v in vals if v is not None]
    return round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1) if vals else None


def runs_of(sub, min_len=12):
    """Consecutive stretches of months in a band, a year or longer: the eras a band is made of."""
    idx = sorted(dates.index(r["date"]) for r in sub)
    out, start, prev = [], None, None
    for i in idx + [None]:
        if start is None:
            start, prev = i, i
        elif i is not None and i == prev + 1:
            prev = i
        else:
            if prev - start + 1 >= min_len:
                out.append({"from": dates[start], "to": dates[prev], "months": prev - start + 1})
            start, prev = i, i
    return out


bands_out = []
for name, lo, hi in BANDS:
    sub = band_rows[name]
    runs = runs_of(sub)
    bands_out.append({
        "band": name, "lo": lo, "hi": min(hi, 100),
        "n_months": len(sub),
        "first_month": sub[0]["date"] if sub else None,
        "last_month": sub[-1]["date"] if sub else None,
        "runs": runs,
        "months_in_runs": sum(r["months"] for r in runs),
        "median_fwd_1y_nominal_pct": med([frac_pct(r, "fwd_1y_nominal") for r in sub]),
        "median_fwd_1y_real_pct": med([frac_pct(r, "fwd_1y_real") for r in sub]),
        "median_fwd_5y_nominal_pct": med([frac_pct(r, "fwd_5y_nominal") for r in sub]),
        "median_fwd_5y_real_pct": med([frac_pct(r, "fwd_5y_real") for r in sub]),
        "median_fwd_10y_real_pct": med([frac_pct(r, "fwd_10y_real") for r in sub]),
        "worst_fwd_5y_real_pct": (lambda v: round(min(v), 1) if v else None)([frac_pct(r, "fwd_5y_real") for r in sub if r.get("fwd_5y_real", "") != ""]),
        "median_worst_fall_5y_pct": med([frac_pct(r, "maxdd_5y") for r in sub]),
        "share_5y_real_positive_pct": share_positive([frac_pct(r, "fwd_5y_real") for r in sub]),
        "n_with_5y": sum(1 for r in sub if r.get("fwd_5y_real", "") != ""),
    })
last_pct = pct_by_month.get(dates[-1])
price_vs_past = {
    "measure": "price divided by the CPI of its month, midrank percentile against every earlier month of the World Bank series (from 1960)",
    "last_month": dates[-1], "last_price": round(price_seq[-1], 2),
    "last_percentile": round(last_pct, 1) if last_pct is not None else None,
    "last_band": band_of(last_pct) if last_pct is not None else None,
    "months_higher_than_last_pct": round(100.0 - last_pct, 1) if last_pct is not None else None,
    "months_in_top_tenth_since_1971": len(band_rows["Top tenth"]),
    "bands": bands_out,
    "cpi_months_filled": cpi_filled,
}

# ---------------------------------------------------------------- 8. monthly moves
# For the page on why the price is moving: the change of the monthly average
# price from one month to the next, 1971+, in five bands, and what followed
# from the months in each band (point-in-time forward columns). The page maps
# a 30-day move of the daily price to the nearest band and says the two are
# neighbouring measures, not the same one.
MOVE_BANDS = [
    ("Down more than 5%", -1e9, -5.0), ("Down 2% to 5%", -5.0, -2.0), ("Within 2%", -2.0, 2.0),
    ("Up 2% to 5%", 2.0, 5.0), ("Up more than 5%", 5.0, 1e9),
]


def move_band(pct):
    return next(name for name, lo, hi in MOVE_BANDS if lo <= pct < hi)


move_rows = {name: [] for name, _, _ in MOVE_BANDS}
for i in range(1, len(rows)):
    pct = (price_seq[i] / price_seq[i - 1] - 1) * 100.0
    move_rows[move_band(pct)].append(rows[i])
moves_out = []
for name, lo, hi in MOVE_BANDS:
    sub = move_rows[name]
    f1 = [frac_pct(r, "fwd_1y_nominal") for r in sub]
    moves_out.append({
        "band": name, "lo": None if lo < -1e8 else lo, "hi": None if hi > 1e8 else hi,
        "n_months": len(sub),
        "median_fwd_1y_nominal_pct": med(f1),
        "median_fwd_1y_real_pct": med([frac_pct(r, "fwd_1y_real") for r in sub]),
        "median_fwd_5y_nominal_pct": med([frac_pct(r, "fwd_5y_nominal") for r in sub]),
        "median_fwd_5y_real_pct": med([frac_pct(r, "fwd_5y_real") for r in sub]),
        "share_1y_positive_pct": share_positive(f1),
        "worst_fwd_1y_nominal_pct": (lambda v: round(min(v), 1) if v else None)([x for x in f1 if x is not None]),
        "best_fwd_1y_nominal_pct": (lambda v: round(max(v), 1) if v else None)([x for x in f1 if x is not None]),
        "n_with_1y": sum(1 for x in f1 if x is not None),
    })
last_move_pct = round((price_seq[-1] / price_seq[-2] - 1) * 100.0, 1)
monthly_moves = {
    "measure": "change of the World Bank monthly average price from the previous month, 1971 onward",
    "last_month": dates[-1], "last_move_pct": last_move_pct, "last_band": move_band(last_move_pct),
    "bands": moves_out,
    "share_up_pct": round(100.0 * sum(1 for i in range(1, len(rows)) if price_seq[i] > price_seq[i - 1]) / (len(rows) - 1), 1),
}

# ---------------------------------------------------------------- 9. price by year
# For the price history page: one row per calendar year of the record, from
# the World Bank monthly average price (CC BY 4.0). Average of the year,
# highest and lowest monthly average with their months, change of the yearly
# average against the previous year, and the average in the dollars of the
# last month (each month's price divided by its CPI, times the latest CPI).
# The current year is included as far as it goes and marked partial.
years_all = sorted({int(d[:4]) for d in dates})
cpi_last = cpi_seq[-1]
by_year = []
prev_avg = None
for y in years_all:
    idxs = [i for i, d in enumerate(dates) if int(d[:4]) == y]
    avg = statistics.mean(price_seq[i] for i in idxs)
    avg_today = statistics.mean(real_seq[i] for i in idxs) * cpi_last
    hi_i = max(idxs, key=lambda i: price_seq[i])
    lo_i = min(idxs, key=lambda i: price_seq[i])
    s_avg = int(round(statistics.mean(scores[i] for i in idxs)))
    by_year.append({
        "year": y, "months": len(idxs), "partial": len(idxs) < 12,
        "avg": round(avg, 2), "avg_today_dollars": round(avg_today, 2),
        "high": round(price_seq[hi_i], 2), "high_month": dates[hi_i],
        "low": round(price_seq[lo_i], 2), "low_month": dates[lo_i],
        "change_pct": None if prev_avg is None else round((avg / prev_avg - 1) * 100.0, 1),
        "score_year_avg": s_avg,
        "zone_year": next(z for z, _, lo, hi in ZONES if lo <= s_avg <= hi),
    })
    prev_avg = avg
full = [r for r in by_year if not r["partial"]]
rec_i = max(range(len(price_seq)), key=lambda i: price_seq[i])
real_rec_i = max(range(len(real_seq)), key=lambda i: real_seq[i])
rises = [r for r in full if r["change_pct"] is not None]
price_by_year = {
    "measure": "World Bank monthly average price, USD per troy ounce, 1971 onward; today's dollars use the CPI of the last month",
    "last_month": dates[-1], "last_price": round(price_seq[-1], 2), "cpi_last_month": dates[-1],
    "years": by_year,
    "first_full_year": full[0]["year"], "last_full_year": full[-1]["year"],
    "multiple_nominal": round(full[-1]["avg"] / full[0]["avg"], 1),
    "multiple_real": round(full[-1]["avg_today_dollars"] / full[0]["avg_today_dollars"], 1),
    "record_month": dates[rec_i], "record_price": round(price_seq[rec_i], 2),
    "real_record_month": dates[real_rec_i], "real_record_today_dollars": round(real_seq[real_rec_i] * cpi_last, 2),
    "years_up": sum(1 for r in rises if r["change_pct"] > 0), "years_down": sum(1 for r in rises if r["change_pct"] <= 0),
    "biggest_rise": max(rises, key=lambda r: r["change_pct"])["year"], "biggest_rise_pct": max(r["change_pct"] for r in rises),
    "biggest_fall": min(rises, key=lambda r: r["change_pct"])["year"], "biggest_fall_pct": min(r["change_pct"] for r in rises),
    "cpi_months_filled": cpi_filled,
}

# ---------------------------------------------------------------- 10. gold to silver ratio
# Ounces of silver one ounce of gold buys: the World Bank monthly average gold
# price divided by the monthly average silver price (same sheet, CC BY 4.0),
# 1960 onward. Where the ratio stood, by decade and by year, and what followed
# from the record window (1971 onward, where the forward columns exist): the
# ratio one and five years later, and both metals' own price changes.
silver = {}
with open(ROOT / "data" / "history" / "wb_silver_usd_toz.csv") as f:
    for r in csv.DictReader(f):
        if r["value"] not in ("", None):
            silver[r["date"]] = float(r["value"])
ratio_all = [(d, prices[d] / silver[d]) for d in sorted(prices) if d in silver and silver[d] > 0]
ratio_by_date = dict(ratio_all)
ratio_seq = [ratio_by_date.get(d) for d in dates]
if any(v is None for v in ratio_seq):
    raise SystemExit("gold to silver ratio missing inside the record window")
silver_seq = [silver[d] for d in dates]
ratio_vals = [v for _, v in ratio_all]
ratio_last_iso, ratio_last = ratio_all[-1]


def _ext(pairs, pick):
    d, v = pick(pairs, key=lambda x: x[1])
    return {"month": d, "ratio": round(v, 1)}


ratio_decades = []
for dec in range(1960, int(ratio_last_iso[:4]) + 1, 10):
    sub = [(d, v) for d, v in ratio_all if dec <= int(d[:4]) < dec + 10]
    if not sub:
        continue
    ratio_decades.append({
        "decade": f"{dec}s", "months": len(sub), "partial": len(sub) < 120,
        "median": round(statistics.median(v for _, v in sub), 1),
        "low": _ext(sub, min), "high": _ext(sub, max),
    })

ratio_years = []
for y in sorted({int(d[:4]) for d, _ in ratio_all}):
    sub = [d for d, _ in ratio_all if int(d[:4]) == y]
    ratio_years.append({
        "year": y, "months": len(sub), "partial": len(sub) < 12,
        "avg_ratio": round(statistics.mean(ratio_by_date[d] for d in sub), 1),
        "gold_avg": round(statistics.mean(prices[d] for d in sub), 2),
        "silver_avg": round(statistics.mean(silver[d] for d in sub), 2),
    })

RATIO_BANDS = [("Below 40", 0, 40), ("40 to 60", 40, 60), ("60 to 80", 60, 80), ("80 and above", 80, 10 ** 9)]


def ratio_band(v):
    return next(name for name, lo, hi in RATIO_BANDS if lo <= v < hi)


def _pct_change(seq, i, k):
    return (seq[i + k] / seq[i] - 1) * 100.0


ratio_bands = []
for name, lo, hi in RATIO_BANDS:
    idx = [i for i, v in enumerate(ratio_seq) if lo <= v < hi]
    sub = [rows[i] for i in idx]
    i1 = [i for i in idx if i + 12 < len(dates)]
    i5 = [i for i in idx if i + 60 < len(dates)]
    ratio_bands.append({
        "band": name, "lo": lo, "hi": min(hi, 999),
        "n_months": len(sub),
        "first_month": dates[idx[0]] if idx else None,
        "last_month": dates[idx[-1]] if idx else None,
        "runs": runs_of(sub) if sub else [],
        "median_ratio": round(statistics.median(ratio_seq[i] for i in idx), 1) if idx else None,
        "median_ratio_1y_later": med([ratio_seq[i + 12] for i in i1]),
        "median_ratio_5y_later": med([ratio_seq[i + 60] for i in i5]),
        "share_ratio_lower_1y_pct": share_positive([ratio_seq[i] - ratio_seq[i + 12] for i in i1]),
        "share_ratio_lower_5y_pct": share_positive([ratio_seq[i] - ratio_seq[i + 60] for i in i5]),
        "median_gold_fwd_1y_nominal_pct": med([_pct_change(price_seq, i, 12) for i in i1]),
        "median_gold_fwd_5y_nominal_pct": med([_pct_change(price_seq, i, 60) for i in i5]),
        "median_silver_fwd_1y_nominal_pct": med([_pct_change(silver_seq, i, 12) for i in i1]),
        "median_silver_fwd_5y_nominal_pct": med([_pct_change(silver_seq, i, 60) for i in i5]),
        "median_gold_fwd_5y_real_pct": med([frac_pct(rows[i], "fwd_5y_real") for i in idx]),
        "n_with_1y": len(i1), "n_with_5y": len(i5),
    })


def _ago(months):
    k = len(ratio_all) - 1 - months
    return {"month": ratio_all[k][0], "ratio": round(ratio_all[k][1], 1)} if k >= 0 else None


gold_silver_ratio = {
    "measure": "World Bank monthly average gold price divided by the monthly average silver price, both in US dollars per troy ounce, 1960 onward; what followed is measured on the record window from 1971",
    "first_month": ratio_all[0][0], "last_month": ratio_last_iso,
    "last_ratio": round(ratio_last, 1),
    "last_gold": round(prices[ratio_last_iso], 2), "last_silver": round(silver[ratio_last_iso], 2),
    "last_percentile_since_1960": round(midrank_pct(ratio_last, ratio_vals), 1),
    "last_band": ratio_band(ratio_last),
    "median_since_1960": round(statistics.median(ratio_vals), 1),
    "median_since_1971": round(statistics.median(ratio_seq), 1),
    "months_since_1960": len(ratio_all), "months_in_record": len(dates),
    "months_at_or_above_80_since_1960": sum(1 for v in ratio_vals if v >= 80),
    "months_below_40_since_1960": sum(1 for v in ratio_vals if v < 40),
    "highest": _ext(ratio_all, max), "lowest": _ext(ratio_all, min),
    "ago_1y": _ago(12), "ago_5y": _ago(60), "ago_10y": _ago(120),
    "top5": [{"month": d, "ratio": round(v, 1)} for d, v in sorted(ratio_all, key=lambda x: -x[1])[:5]],
    "bottom5": [{"month": d, "ratio": round(v, 1)} for d, v in sorted(ratio_all, key=lambda x: x[1])[:5]],
    "decades": ratio_decades,
    "years": ratio_years,
    "bands": ratio_bands,
}

# ---------------------------------------------------------------- write
out = {
    "note": "Computed by scripts/backtest/question_stats.py from monthly_scores.csv, wb_gold_usd_toz.csv and wb_silver_usd_toz.csv. All figures in percent. Nominal unless the key says real.",
    "record_months": len(rows),
    "first_month": dates[0], "last_month": dates[-1],
    "wait": {"better_means": "score of 40 or higher (Mixed or better)", "zones": wait_zones},
    "record_highs": ath,
    "seasonality": seasonality,
    "deep_falls": dips,
    "peaks": peaks,
    "purchase_years": purchase_years,
    "price_vs_past": price_vs_past,
    "monthly_moves": monthly_moves,
    "price_by_year": price_by_year,
    "gold_silver_ratio": gold_silver_ratio,
}
OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
print(json.dumps({k: out[k] for k in ("record_months", "first_month", "last_month")}))
print("wait zones:", [(z["zone"], z["median_wait_months"], z["median_price_change_while_waiting_pct"], z["share_reached_within_5y_pct"]) for z in wait_zones])
print("ath near:", ath["near_record"])
print("ath away:", ath["away_from_record"])
print("season best/worst:", best["name"], best["median_mom_pct"], "/", worst["name"], worst["median_mom_pct"])
print("peaks:", [(p["peak_month"][:7], p["months_to_recover_nominal"], p["months_to_recover_real"]) for p in peaks_out], "since", peaks["since_last_peak"])
print("years:", purchase_years["years_total"], "ahead real", purchase_years["years_ahead_real"], "behind", purchase_years["years_behind_real"], "median real", purchase_years["median_gain_real_pct"])
print("price vs past:", price_vs_past["last_month"], price_vs_past["last_percentile"], price_vs_past["last_band"], [(b["band"], b["n_months"], b["median_fwd_5y_real_pct"]) for b in bands_out])
print("monthly moves:", monthly_moves["last_month"], monthly_moves["last_move_pct"], monthly_moves["last_band"], [(b["band"], b["n_months"], b["median_fwd_1y_nominal_pct"], b["share_1y_positive_pct"]) for b in moves_out])
print("price by year:", price_by_year["first_full_year"], price_by_year["last_full_year"], price_by_year["multiple_nominal"], price_by_year["multiple_real"], price_by_year["record_month"], price_by_year["real_record_month"], price_by_year["years_up"], price_by_year["years_down"])
print("dips:", dips["n_episodes"], "episodes; further fall", dips["median_further_fall_after_cross_pct"], "; fwd5y", dips["median_fwd_from_cross_5y_pct"])
print("gold to silver:", gold_silver_ratio["last_month"], gold_silver_ratio["last_ratio"], gold_silver_ratio["last_band"], "pct", gold_silver_ratio["last_percentile_since_1960"], "median60", gold_silver_ratio["median_since_1960"], [(b["band"], b["n_months"], b["share_ratio_lower_1y_pct"], b["median_ratio_5y_later"]) for b in ratio_bands])
