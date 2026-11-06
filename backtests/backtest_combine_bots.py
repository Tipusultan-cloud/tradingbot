"""Bots 2 and 3 merged into one bot against each alone. Each trade keeps its own
exit. Reuses three_bots_prices.pkl from backtest_three_bots.py (run that first).
"""
import pickle, sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backtests")]
import backtest_three_bots as three
bp, hv, top200 = three.bp, three.hv, three.top200
with three.PRICE_CACHE.open("rb") as f:
    frames = pickle.load(f)
cons = hv.load_sp500_constituents()
sp500 = set(cons["ticker"])
market = hv.download_many(["SPY"], "market")
spy = hv.add_indicators(market["SPY"])
last_stock = max(f.index.max() for f in list(frames.values())[:50])
cal = spy.index[spy.index <= pd.Timestamp(last_stock).normalize()]; last = cal.max()
full = cal[cal >= last - pd.DateOffset(years=10)]
sig = set(full[:-1])
raw = {t: frames[t].dropna(subset=["Close"]).astype("float64") for t in sp500 if t in frames}
dfs, panel = hv.prepare_stock_data(cons, raw)
panel = panel[panel["AVG_VOLUME20"] > 0]
pull = bp.build_pullback_candidates(panel, sig)
del panel
others = [t for t in frames if t not in sp500]
ranks = top200.dollar_volume_ranks(frames, others).reindex(pd.DatetimeIndex(full).normalize())
mom = three.build_momentum_candidates(frames, sp500, ranks <= top200.TOP_TRADED_COUNT, sig)
del ranks
for t in set(mom["ticker"]) - set(dfs):
    dfs[t] = frames[t].dropna(subset=["Close"]).astype("float64")
del frames
earn = three.load_earnings()
periods = {"first5": full[full < last - pd.DateOffset(years=5)], "last5": full[full >= last - pd.DateOffset(years=5)],
           "last2": full[full >= last - pd.DateOffset(years=2)]}
variants = {
    "Bot 2 alone, 1 slot": (bp.ranked(pull), 1), "Bot 3 alone, 1 slot": (bp.ranked(mom), 1),
    "Bot 2 alone, 2 slots": (bp.ranked(pull), 2), "Bot 3 alone, 2 slots": (bp.ranked(mom), 2),
    "Combined 1 slot, Bot 2 first": (bp.ranked(pull, mom), 1), "Combined 1 slot, Bot 3 first": (bp.ranked(mom, pull), 1),
    "Combined 2 slots, Bot 2 first": (bp.ranked(pull, mom), 2), "Combined 2 slots, Bot 3 first": (bp.ranked(mom, pull), 2),
}
rows, curves = [], {}
for name, (L, slots) in variants.items():
    tr, eq, ex = three.simulate(L, full, dfs, slots, earn)
    s = three.summarize(tr, eq, ex)
    r = {"variant": name, "trades": s["trades"], "ret": s["total_return_pct"], "cagr": s["cagr_pct"], "dd": s["max_drawdown_pct"], "win": s["win_rate_pct"], "expR": s["expectancy_r"], "pf": s["profit_factor"],
         "no_top3": s["pnl_without_top3_usd"], "bot3_share": round((tr.source == "MOMENTUM").mean() * 100)}
    for p, d in periods.items():
        t2, e2, x2 = three.simulate(L, d, dfs, slots, earn)
        r[p] = three.summarize(t2, e2, x2)["total_return_pct"]
    t3, e3, x3 = three.simulate(L, full, dfs, slots, earn, three.HIGH_SLIPPAGE_PCT)
    r["slip25"] = three.summarize(t3, e3, x3)["total_return_pct"]
    v = [three.summarize(*three.simulate(L, full[k:], dfs, slots, earn)[:2], 0)["total_return_pct"] for k in range(0, 60, 3)]
    r["med20"] = float(pd.Series(v).median()); r["min20"] = min(v); r["max20"] = max(v)
    rows.append(r); curves[name] = (eq, v)
pd.set_option("display.width", 240, "display.max_columns", 30)
print("window", full[0].date(), full[-1].date())
print(pd.DataFrame(rows).to_string(index=False))
y = pd.DataFrame({n: c[0].groupby(c[0].index.year).last().pct_change() for n, c in curves.items()})
print("yearly correlation Bot 2 vs Bot 3:", round(y["Bot 2 alone, 1 slot"].corr(y["Bot 3 alone, 1 slot"]), 2))
m = pd.DataFrame({n: c[0].resample("ME").last().pct_change() for n, c in curves.items()})
print("monthly correlation:", round(m["Bot 2 alone, 1 slot"].corr(m["Bot 3 alone, 1 slot"]), 2))
b = pd.Series(curves["Bot 2 alone, 2 slots"][1]); c = pd.Series(curves["Combined 2 slots, Bot 2 first"][1]); c3 = pd.Series(curves["Combined 2 slots, Bot 3 first"][1])
print("combined 2 slots beats Bot 2 alone 2 slots: B2 first", int((c > b).sum()), "B3 first", int((c3 > b).sum()), "of 20")
