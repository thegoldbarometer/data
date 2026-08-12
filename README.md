# The Gold Barometer — Daily Data

Public daily archive of [The Gold Barometer](https://thegoldbarometer.com), a daily gold buying-conditions score (0-100) built on a fully published methodology.

Each day this repository receives one commit with the day's reading: composite score, zone, per-pillar sub-scores, and the input values whose licenses permit raw redistribution. The commit history doubles as a tamper-evident timestamp of the archive: readings are never silently revised.

- Live score: https://thegoldbarometer.com
- Methodology: https://thegoldbarometer.com/methodology/
- API: https://thegoldbarometer.com/data/

## Licensing

Dual license: data = CC BY 4.0 (attribution "The Gold Barometer, thegoldbarometer.com" — see LICENSE-DATA.md), code = MIT (see LICENSE).

## Code and backtest

- `code/` — the score engine (`compute_score.py`), the point-in-time backtest
  (`backtest.py`), the question-page statistics (`question_stats.py`) and the
  bootstrap uncertainty run (`uncertainty.py`, fixed seed). MIT licence
  (see LICENSE-CODE note in the site's legal pages). Standard library only.
- `backtest/` — the full monthly series 1971-01 to today
  (`monthly_scores.csv`, one row per month with sub-scores and forward
  returns) and every published table derived from it. Refreshed at each
  regeneration, with the change logged at /corrections/ on the site.
