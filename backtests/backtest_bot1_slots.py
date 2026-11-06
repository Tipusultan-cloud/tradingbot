"""Ways to make Bot 1 trade more (entry rules, slots, sector limit). Reuses
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
earn = three.load_earnings()
three.TARGET_BY_SOURCE = {"A_PLUS": 3.0}
periods = {"first5": full[full < last - pd.DateOffset(years=5)], "last5": full[full >= last - pd.DateOffset(years=5)],
           "last2": full[full >= last - pd.DateOffset(years=2)]}
orig_state = hv.build_sector_state

def build(**changes):
    saved = {k: getattr(hv, k) for k in changes if k != "NO_SPY"}
    for k, v in changes.items():
        if k != "NO_SPY":
            setattr(hv, k, v)
    if changes.get("NO_SPY"):
        def state(**kw):
            s = orig_state(**kw)
            if s is not None:
                s["spy_bullish"] = True
            return s
        hv.build_sector_state = state
    frame = bp.build_a_plus_candidates(panel, spy, sectors, full[:-1])
    hv.build_sector_state = orig_state
    for k, v in saved.items():
        setattr(hv, k, v)
    return frame

def line(name, frame, slots=2):
    L = bp.ranked(frame)
    tr, eq, ex = three.simulate(L, full, dfs, slots, earn)
    s = three.summarize(tr, eq, ex)
    r = {"variant": name, "signals": len(frame), "trades": s["trades"], "per_yr": round(s["trades"] / 10, 1), "ret": s["total_return_pct"],
         "dd": s["max_drawdown_pct"], "win": s["win_rate_pct"], "expR": s["expectancy_r"], "pf": s["profit_factor"], "expo": s["exposure_pct"]}
    for p, d in periods.items():
        t2, e2, x2 = three.simulate(L, d, dfs, slots, earn)
        r[p] = three.summarize(t2, e2, x2)["total_return_pct"]
    return r

base = build()
print("sector in candidates:", "sector" in base.columns, base["sector"].value_counts().head(4).to_dict())
L = bp.ranked(base)
def ret(days, slots):
    tr, eq, ex = three.simulate(L, days, dfs, slots, earn)
    return three.summarize(tr, eq, ex)
rows, starts = [], {}
for limit in (None, 1, 2):
    three.SECTOR_LIMIT = limit
    for slots in (1, 2, 3, 4, 5):
        s = ret(full, slots)
        r = {"limit": limit, "slots": slots, "trades": s["trades"], "ret": s["total_return_pct"], "dd": s["max_drawdown_pct"], "win": s["win_rate_pct"], "expR": s["expectancy_r"], "pf": s["profit_factor"]}
        for p, d in periods.items():
            r[p] = ret(d, slots)["total_return_pct"]
        v = [ret(full[k:], slots)["total_return_pct"] for k in range(0, 60, 3)]
        r.update(min20=min(v), med20=float(pd.Series(v).median()), max20=max(v))
        starts[(limit, slots)] = v
        rows.append(r)
pd.set_option("display.width", 220, "display.max_columns", 30)
print(pd.DataFrame(rows).to_string(index=False))
for lim in (None, 1, 2):
    a2 = pd.Series(starts[(lim, 2)])
    print("limit", lim, {n: int((pd.Series(starts[(lim, n)]) > a2).sum()) for n in (3, 4, 5)}, "of 20 beat 2 slots")
