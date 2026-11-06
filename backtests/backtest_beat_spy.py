"""Bots plus SPY parking against SPY and QQQ. Parking is approximate: a share
of idle cash earns the daily SPY return. Reuses three_bots_prices.pkl from
backtest_three_bots.py (run that first).
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
market = hv.download_many(["SPY", "QQQ", *hv.SECTOR_ETFS.values()], "market")
spy = hv.add_indicators(market["SPY"]); qqq = market["QQQ"]
sectors = {e: hv.add_indicators(market[e]) for e in hv.SECTOR_ETFS.values() if e in market}
cal = spy.index; last = cal.max()
full = cal[cal >= last - pd.DateOffset(years=10)]
raw = {t: frames[t].dropna(subset=["Close"]).astype("float64") for t in sp500 if t in frames}
del frames
dfs, panel = hv.prepare_stock_data(cons, raw)
panel = panel[panel["AVG_VOLUME20"] > 0]
a = bp.build_a_plus_candidates(panel, spy, sectors, full[:-1])
pull = bp.build_pullback_candidates(panel, set(full[:-1]))
earn = three.load_earnings()
three.TARGET_BY_SOURCE = {"A_PLUS": 3.0}
spy_ret = spy["Close"].pct_change()
periods = {"full": full, "first5": full[full < last - pd.DateOffset(years=5)],
           "last5": full[full >= last - pd.DateOffset(years=5)],
           "last2": full[full >= last - pd.DateOffset(years=2)]}

def parked(trades, equity, share):
    invested = pd.Series(0.0, index=equity.index)
    for t in trades.itertuples():
        invested.loc[t.entry_date:t.exit_date] += t.entry_price * t.shares
    base = equity.pct_change().fillna(0)
    idle = (1 - invested / equity).clip(lower=0)
    extra = idle.shift(1).fillna(1) * share * spy_ret.reindex(equity.index).fillna(0)
    return three.STARTING_EQUITY * (1 + base + extra).cumprod(), float(idle.mean())

def stats(eq):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    return ((eq.iloc[-1] / eq.iloc[0] - 1) * 100, ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100, (eq / eq.cummax() - 1).min() * -100)

def line(name, lookup, slots, risk, share):
    hv.RISK_PER_TRADE_PCT = risk
    r = {"variant": name}
    for p, days in periods.items():
        tr, eq, ex = three.simulate(lookup, days, dfs, slots, earn)
        eq, idle = parked(tr, eq, share)
        ret, cagr, dd = stats(eq)
        r[p] = round(ret, 1)
        if p == "full":
            r.update(cagr=round(cagr, 1), dd=round(dd, 1), idle=round(idle * 100))
            yearly = eq.groupby(eq.index.year).last()
            r["_y"] = yearly
    hv.RISK_PER_TRADE_PCT = 1.0
    return r

B1, MIX = bp.ranked(a), bp.ranked(a, pull)
rows = []
for name, c in (("SPY", spy["Close"]), ("QQQ", qqq["Close"])):
    r = {"variant": name + " buy and hold"}
    for p, days in periods.items():
        ret, cagr, dd = stats(c.reindex(days).ffill())
        r[p] = round(ret, 1)
        if p == "full":
            r.update(cagr=round(cagr, 1), dd=round(dd, 1))
    rows.append(r)
for lab, lk, sl in (("Bot 1 only", B1, 1), ("Bots 1+2, 2 slots", MIX, 2)):
    for risk in (1.0, 1.5):
        for share in (0.5, 0.75, 1.0):
            rows.append(line(f"{lab} risk {risk}% park {int(share*100)}%", lk, sl, risk, share))
s = spy["Close"].reindex(full).ffill()
sy = s.groupby(s.index.year).last()
sy_ret = sy.pct_change(); sy_ret.iloc[0] = sy.iloc[0] / s.iloc[0] - 1
out = []
for r in rows:
    y = r.pop("_y", None)
    if y is not None:
        yr = y.pct_change(); yr.iloc[0] = y.iloc[0] / three.STARTING_EQUITY - 1
        r["yrs_beat_spy"] = int((yr > sy_ret).sum())
    out.append(r)
pd.set_option("display.width", 220, "display.max_columns", 30)
print(pd.DataFrame(out).to_string(index=False))
