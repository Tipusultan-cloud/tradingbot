"""Current Bot 1 setup against 4 slots, all live rules plus 75% SPY parking, with
compounding and with fixed $100 risk. Run backtest_three_bots.py first.
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

import numpy as np
base = build()
L = bp.ranked(base)
PARK = 0.75
spy_ret = spy["Close"].pct_change()
close = spy["Close"]
setups = {"CURRENT 2 slots, 1/sector": (2, 1), "4 slots, 1/sector": (4, 1), "4 slots, 2/sector": (4, 2)}
periods = {"full": full, **periods, "last1": full[full >= last - pd.DateOffset(months=12)]}

def invested(tr, index):
    inv = pd.Series(0.0, index=index); n = pd.Series(0, index=index)
    for t in tr.itertuples():
        inv.loc[t.entry_date:t.exit_date] += t.entry_price * t.shares
        n.loc[t.entry_date:t.exit_date] += 1
    return inv, n

def parked(tr, eq):
    inv, _ = invested(tr, eq.index)
    idle = (1 - inv / eq).clip(lower=0)
    return three.STARTING_EQUITY * (1 + eq.pct_change().fillna(0) + idle.shift(1).fillna(1) * PARK * spy_ret.reindex(eq.index).fillna(0)).cumprod()

def stats(eq):
    y = (eq.index[-1] - eq.index[0]).days / 365.25
    return (eq.iloc[-1] / eq.iloc[0] - 1) * 100, ((eq.iloc[-1] / eq.iloc[0]) ** (1 / y) - 1) * 100, (eq / eq.cummax() - 1).min() * -100

def run(days, slots, limit, slip=hv.SLIPPAGE_PCT):
    three.SECTOR_LIMIT = limit
    return three.simulate(L, days, dfs, slots, earn, slip)

rows, yearly, starts = [], {}, {}
s = close.reindex(full).ffill()
r = {"setup": "SPY buy and hold"}
for p, d in periods.items():
    r[p] = round(stats(close.reindex(d).ffill())[0], 1)
c = stats(s); r.update(cagr=round(c[1], 1), dd=round(c[2], 1), worst_day=round(s.pct_change().min() * 100, 1))
rows.append(r)
ye = s.groupby(s.index.year).last(); yr = ye.pct_change(); yr.iloc[0] = ye.iloc[0] / s.iloc[0] - 1
yearly["SPY"] = (yr * 100).round(1)
for name, (slots, limit) in setups.items():
    tr, eq, ex = run(full, slots, limit)
    pe = parked(tr, eq)
    _, n = invested(tr, eq.index)
    st = stats(pe)
    r = {"setup": name, "trades": len(tr), "cagr": round(st[1], 1), "dd": round(st[2], 1), "worst_day": round(pe.pct_change().min() * 100, 1),
         "bots_only": round(stats(eq)[0], 1), "bots_dd": round(stats(eq)[2], 1), "bots_worst_day": round(eq.pct_change().min() * 100, 1),
         "max_open": int(n.max()), "days_3plus": int((n >= 3).sum())}
    for p, d in periods.items():
        t2, e2, _x = run(d, slots, limit)
        r[p] = round(stats(parked(t2, e2))[0], 1)
    t3, e3, _x = run(full, slots, limit, three.HIGH_SLIPPAGE_PCT)
    r["slip25_cagr"] = round(stats(parked(t3, e3))[1], 1)
    tr["same_day"] = tr.groupby("exit_date")["pnl"].transform("sum")
    r["worst_exit_day_usd"] = round(tr["same_day"].min())
    rows.append(r)
    ye = pe.groupby(pe.index.year).last(); yr = ye.pct_change(); yr.iloc[0] = ye.iloc[0] / three.STARTING_EQUITY - 1
    yearly[name] = (yr * 100).round(1)
    starts[name] = [stats(parked(*run(full[k:], slots, limit)[:2]))[1] for k in range(0, 60, 3)]
pd.set_option("display.width", 240, "display.max_columns", 40)
t = pd.DataFrame(rows)
print(t.T.to_string())
print(); print(pd.DataFrame(yearly).to_string())
y = pd.DataFrame(yearly)
print({c: int((y[c] > y["SPY"]).sum()) for c in setups}, "years beat SPY of", len(y))
sh = pd.DataFrame(starts)
print(sh.describe().loc[["min", "50%", "max"]].round(1).to_string())
print("vs CURRENT over 20 starts:", {c: int((sh[c] > sh["CURRENT 2 slots, 1/sector"]).sum()) for c in setups})
print("beat SPY 15.0% cagr count (approx):", {c: int((sh[c] > 15.0).sum()) for c in setups})

# Live-like: $10,000, fixed 1% risk, 75% of (capital - money in trades) in SPY.
three.STARTING_EQUITY = 10000.0; three.FIXED_CAPITAL = 10000.0
print("\nFIXED $100 RISK ON $10,000:")
for name, (slots, limit) in setups.items():
    tr, eq, ex = run(full, slots, limit)
    inv, _ = invested(tr, eq.index)
    parked_usd = (PARK * (10000.0 - inv).clip(lower=0)).shift(1).fillna(7500)
    spy_pnl = (parked_usd * spy_ret.reindex(eq.index).fillna(0)).cumsum()
    total = eq + spy_pnl
    a = stats(total)
    print(f"{name}: trades {len(tr)}, bot P&L ${eq.iloc[-1]-10000:,.0f}, SPY P&L ${spy_pnl.iloc[-1]:,.0f}, total {a[0]:.1f}%, per year {a[1]:.1f}%, worst drop {a[2]:.1f}%")
