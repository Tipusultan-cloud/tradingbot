"""Bot 1 final targets 2.5R-5R: periods, slippage, 20 start dates. Reuses
three_bots_prices.pkl from backtest_three_bots.py (run that first).
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
targets = [2.5, 3.0, 3.5, 4.0, 5.0]
periods = {"full": full, "first5": full[full < last - pd.DateOffset(years=5)],
           "last5": full[full >= last - pd.DateOffset(years=5)],
           "last2": full[full >= last - pd.DateOffset(years=2)],
           "last1": full[full >= last - pd.DateOffset(months=12)]}
rows = []
for t in targets:
    three.TARGET_R = t
    r = {"target": t}
    for p, days in periods.items():
        tr, eq, ex = three.simulate(L, days, dfs, 1, earn)
        s = three.summarize(tr, eq, ex)
        r[p] = s["total_return_pct"]
        if p == "full":
            r.update(dd=s["max_drawdown_pct"], trades=s["trades"], win=s["win_rate_pct"], expR=s["expectancy_r"], pf=s["profit_factor"], hold=s["avg_hold_days"])
            hit = tr.exit_reason.str.contains("TARGET").sum()
            half = tr.partial_price.notna().sum()
            r.update(half=int(half), hit_target=int(hit), half_back_to_entry=int(tr.exit_reason.str.contains("PARTIAL.*STOP").sum()), half_time=int(tr.exit_reason.isin(["PARTIAL+TIME_EXIT"]).sum()), half_earn=int((tr.exit_reason=="PARTIAL+EARNINGS_EXIT").sum()))
    tr, eq, ex = three.simulate(L, full, dfs, 1, earn, three.HIGH_SLIPPAGE_PCT)
    r["slip25"] = three.summarize(tr, eq, ex)["total_return_pct"]
    rows.append(r)
tab = pd.DataFrame(rows)
shift = {}
for k in range(0, 60, 3):
    days = full[k:]
    res = {}
    for t in targets:
        three.TARGET_R = t
        tr, eq, ex = three.simulate(L, days, dfs, 1, earn)
        res[t] = three.summarize(tr, eq, ex)["total_return_pct"]
    shift[k] = res
sh = pd.DataFrame(shift).T
pd.set_option("display.width", 220, "display.max_columns", 30)
print(tab.to_string(index=False)); print()
print("start-date shifts (20 starts):"); print(sh.describe().loc[["min", "50%", "max"]].round(1).to_string())
print("best target count:", sh.idxmax(axis=1).value_counts().to_dict())
print("4R beats 3R in", int((sh[4.0] > sh[3.0]).sum()), "of", len(sh))
print("3R beats 2.5R in", int((sh[3.0] > sh[2.5]).sum()), "of", len(sh))
