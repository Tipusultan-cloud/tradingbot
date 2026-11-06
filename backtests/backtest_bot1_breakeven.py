"""Bot 1 at 3R: move the stop to the entry (0R) at different profit levels.
Reuses three_bots_prices.pkl from backtest_three_bots.py (run that first).
"""
import pickle, sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backtests")]
import backtest_three_bots as three
bp, hv = three.bp, three.hv
with three.PRICE_CACHE.open("rb") as f:
    frames = pickle.load(f)
cons = hv.load_sp500_constituents()
sp500 = set(cons["ticker"])
market = hv.download_many(["SPY", *hv.SECTOR_ETFS.values()], "market")
spy = hv.add_indicators(market["SPY"])
sectors = {e: hv.add_indicators(market[e]) for e in hv.SECTOR_ETFS.values() if e in market}
cal = spy.index; last = cal.max()
full = cal[cal >= last - pd.DateOffset(years=10)]
raw = {t: frames[t].dropna(subset=["Close"]).astype("float64") for t in sp500 if t in frames}
del frames
dfs, panel = hv.prepare_stock_data(cons, raw)
panel = panel[panel["AVG_VOLUME20"] > 0]
a = bp.build_a_plus_candidates(panel, spy, sectors, full[:-1])
L = bp.ranked(a); earn = three.load_earnings()
three.TARGET_BY_SOURCE = {"A_PLUS": 3.0}
periods = {"first5": full[full < last - pd.DateOffset(years=5)], "last5": full[full >= last - pd.DateOffset(years=5)]}
rows = []
for be in (None, 1.5, 1.0, 0.75, 0.5, 0.25):
    three.BREAKEVEN_R = be
    tr, eq, ex = three.simulate(L, full, dfs, 1, earn)
    s = three.summarize(tr, eq, ex)
    r = {"move_at": be or "2R (live)", "ret": s["total_return_pct"], "dd": s["max_drawdown_pct"], "trades": s["trades"], "win": s["win_rate_pct"], "expR": s["expectancy_r"],
         "full_losses": int((tr.r_multiple <= -0.9).sum()), "scratch": int(tr.r_multiple.abs().lt(0.1).sum()), "hit_target": int(tr.exit_reason.str.contains("TARGET").sum())}
    for p, d in periods.items():
        r[p] = three.summarize(*three.simulate(L, d, dfs, 1, earn)[:2], 0)["total_return_pct"]
    rows.append(r)
pd.set_option("display.width", 200, "display.max_columns", 30)
print(pd.DataFrame(rows).to_string(index=False))
