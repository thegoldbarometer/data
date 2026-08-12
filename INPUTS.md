# Rebuilding the input series

`code/backtest.py` regenerates `backtest/monthly_scores.csv` from nine input
series. The public-domain and openly licensed ones ship in `inputs/`. Two are
licence-restricted and must be fetched from their sources; the steps below
reproduce them exactly.

| File | Source | Licence | In `inputs/`? |
|---|---|---|---|
| `tips_10y.csv` | Federal Reserve H.15, 10-Year TIPS constant maturity (FRED: DFII10) | US public domain | yes |
| `nominal_10y.csv` | Federal Reserve H.15, 10-Year Treasury constant maturity (FRED: GS10) | US public domain | yes |
| `cpi.csv` | US BLS, CPI-U all items SA (CUSR0000SA0) | US public domain | yes |
| `real_rate_proxy.csv` | Derived: GS10 minus CPI year-over-year (our stitch, code in `code/`) | derived, public-domain inputs | yes |
| `wb_gold_usd_toz.csv` | World Bank Pink Sheet, gold monthly average USD/toz | CC BY 4.0 (World Bank) | yes |
| `dollar_broad.csv` | Federal Reserve H.10, broad dollar index | US public domain | yes |
| `cot_mm_net_pct_oi.csv` | Derived from CFTC disaggregated COT (code 088691): managed-money net length as % of open interest | derived, public-domain input | yes |
| `cb_gold_reporters_sum.csv` | Derived from IMF IRFCL: sum of reporting countries' gold holdings | derived; attribute the IMF | yes |
| `gvz.csv` | Cboe Gold ETF Volatility Index, daily close | Cboe data — NOT redistributable | **no** — download the official GVZ history CSV from Cboe's site, save as `date,value` |
| `gld_tonnes.csv` | SPDR Gold Shares daily holdings (tonnes) | SSGA data — redistribution unclear | **no** — fetch `api.spdrgoldshares.com/api/v1/historical-archive`, save as `date,value` |

Run order, from the repository root:

    TGB_DATA_DIR=. py code/backtest.py
    TGB_DATA_DIR=. py code/question_stats.py
    TGB_DATA_DIR=. py code/uncertainty.py

`uncertainty.py` is seeded (19710101): its JSON output is byte-identical run
to run. Without the two restricted series, the volatility pillar (2014+) and
the ETF half of structural demand cannot be regenerated; every other pillar
and every published table derived from `backtest/monthly_scores.csv` can.
