#!/usr/bin/env python3
"""Effective sample size and bootstrap confidence intervals per zone.

Per the concept-audit quant spec:
  - episodes per zone (runs of consecutive months) and non-overlapping window
    counts, the honest N behind every band median;
  - stationary bootstrap (Politis-Romano, geometric block lengths with mean =
    the forward horizon), 10,000 draws, 90% CI on each band median and on the
    Favorable minus Unfavorable spread at 5y real.

When a zone holds fewer months than one horizon, the CI is reported as null:
there is less than one independent observation, and pretending a bootstrap can
fix that would be exactly the false precision this exercise exists to kill.

Output: data/backtest/uncertainty.json + a readable table on stdout.
Nothing on the site consumes this yet: publication is a separate decision.
"""
import csv
import json
import random
import statistics
from pathlib import Path

import os
ROOT = Path(os.environ.get("TGB_DATA_DIR", str(Path.home() / "projects" / "thegoldbarometer")))
OUT = ROOT / "data" / "backtest" / "uncertainty.json"

random.seed(19710101)  # fixed: reruns must reproduce
DRAWS = 10_000

rows = []
with open(ROOT / "data" / "backtest" / "monthly_scores.csv") as f:
    for r in csv.DictReader(f):
        rows.append(r)

ZONES = [
    ("Favorable", 60, 79),
    ("Mixed", 40, 59),
    ("Unfavorable", 20, 39),
    ("Historically very unfavorable", 0, 19),
]
HORIZONS = [
    ("fwd_1y_nominal", 12, "1y nominal"),
    ("fwd_5y_nominal", 60, "5y nominal"),
    ("fwd_5y_real", 60, "5y real"),
]


def frac_pct(r, col):
    v = r.get(col, "")
    return None if v in ("", None) else float(v) * 100.0


def month_index(iso):
    y, m = int(iso[:4]), int(iso[5:7])
    return y * 12 + m


def episodes_of(idxs, dates):
    """Runs of consecutive calendar months."""
    runs = []
    start = prev = idxs[0]
    for i in idxs[1:]:
        if month_index(dates[i]) != month_index(dates[prev]) + 1:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def stationary_bootstrap_median(series, mean_block, draws):
    """90% CI of the median under a stationary bootstrap of the ordered series."""
    n = len(series)
    p = 1.0 / mean_block
    meds = []
    for _ in range(draws):
        out = []
        i = random.randrange(n)
        while len(out) < n:
            out.append(series[i])
            if random.random() < p:
                i = random.randrange(n)
            else:
                i = (i + 1) % n
        meds.append(statistics.median(out))
    meds.sort()
    return meds[int(0.05 * draws)], meds[int(0.95 * draws)], meds


dates = [r["date"] for r in rows]
scores = [int(r["score"]) for r in rows]

zones_out = []
draws_cache = {}
for zone, lo, hi in ZONES:
    idxs = [i for i, s in enumerate(scores) if lo <= s <= hi]
    runs = episodes_of(idxs, dates)
    zrec = {
        "zone": zone,
        "n_months": len(idxs),
        "n_episodes": len(runs),
        "episodes": [
            {"from": dates[a][:7], "to": dates[b][:7], "months": b - a + 1}
            for a, b in runs
        ],
        "horizons": [],
    }
    for col, h, label in HORIZONS:
        series = [frac_pct(rows[i], col) for i in idxs]
        series = [v for v in series if v is not None]
        n = len(series)
        rec = {
            "horizon": label,
            "n_months_with_data": n,
            "n_nonoverlapping_windows": round(n / h, 2),
            "median_pct": round(statistics.median(series), 1) if series else None,
        }
        if n > h:
            lo90, hi90, meds = stationary_bootstrap_median(series, h, DRAWS)
            rec["ci90_pct"] = [round(lo90, 1), round(hi90, 1)]
            if (zone, label) in (("Favorable", "5y real"), ("Unfavorable", "5y real")):
                draws_cache[(zone, label)] = meds
        else:
            rec["ci90_pct"] = None
            rec["ci_note"] = "fewer months than one horizon: under one independent observation"
        zrec["horizons"].append(rec)
    zones_out.append(zrec)

# Spread Favorable - Unfavorable, 5y real: difference of independent band medians.
spread = None
if ("Favorable", "5y real") in draws_cache and ("Unfavorable", "5y real") in draws_cache:
    fa = draws_cache[("Favorable", "5y real")]
    un = draws_cache[("Unfavorable", "5y real")]
    random.shuffle(fa)
    random.shuffle(un)
    diffs = sorted(f - u for f, u in zip(fa, un))
    spread = {
        "what": "Favorable minus Unfavorable, median 5y real",
        "point_pct": None,
        "ci90_pct": [round(diffs[int(0.05 * len(diffs))], 1),
                     round(diffs[int(0.95 * len(diffs))], 1)],
        "share_of_draws_positive_pct": round(
            100.0 * sum(1 for d in diffs if d > 0) / len(diffs), 1),
    }
    fav = next(z for z in zones_out if z["zone"] == "Favorable")
    unf = next(z for z in zones_out if z["zone"] == "Unfavorable")
    fm = next(h for h in fav["horizons"] if h["horizon"] == "5y real")["median_pct"]
    um = next(h for h in unf["horizons"] if h["horizon"] == "5y real")["median_pct"]
    spread["point_pct"] = round(fm - um, 1)

out = {
    "note": "Stationary bootstrap, mean block = horizon, 10000 draws, seed fixed. Computed by scripts/backtest/uncertainty.py. NOT yet published on the site.",
    "record_months": len(rows),
    "zones": zones_out,
    "spread_5y_real": spread,
}
OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")

# ---- readable summary ----
print(f"{'zone':<32}{'months':>7}{'episodes':>9}  horizon      median   90% CI            n-indep")
for z in zones_out:
    for h in z["horizons"]:
        ci = f"[{h['ci90_pct'][0]}, {h['ci90_pct'][1]}]" if h["ci90_pct"] else "n/a (<1 window)"
        print(f"{z['zone']:<32}{z['n_months']:>7}{z['n_episodes']:>9}  {h['horizon']:<11}{h['median_pct']:>7}   {ci:<18}{h['n_nonoverlapping_windows']:>7}")
print()
if spread:
    print(f"Spread Favorable-Unfavorable 5y real: point {spread['point_pct']} | CI90 {spread['ci90_pct']} | draws>0: {spread['share_of_draws_positive_pct']}%")
for z in zones_out:
    eps = ", ".join(f"{e['from']}..{e['to']}({e['months']}m)" for e in z["episodes"][:8])
    more = "" if z["n_episodes"] <= 8 else f" +{z['n_episodes']-8} more"
    print(f"{z['zone']}: {z['n_episodes']} episodes: {eps}{more}")
print(f"\nwrote {OUT}")
