"""Bots 1+2 against all three at the same number of open trades, with the sector
limit and Bot 3 in its own slot. Run backtest_three_bots.py first.
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
market = hv.download_many(["SPY", *hv.SECTOR_ETFS.values()], "market")
spy = hv.add_indicators(market["SPY"])
last_stock = max(f.index.max() for f in list(frames.values())[:50])
cal = spy.index[spy.index <= pd.Timestamp(last_stock).normalize()]; last = cal.max()
full = cal[cal >= last - pd.DateOffset(years=10)]
sig = set(full[:-1])
raw = {t: frames[t].dropna(subset=["Close"]).astype("float64") for t in sp500 if t in frames}
dfs, panel = hv.prepare_stock_data(cons, raw)
panel = panel[panel["AVG_VOLUME20"] > 0]
sectors = {e: hv.add_indicators(market[e]) for e in hv.SECTOR_ETFS.values() if e in market}
a = bp.build_a_plus_candidates(panel, spy, sectors, full[:-1])
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
import bisect
import numpy as np
from slot_selector import RECENT_EARNINGS_TRADING_DAYS
from earnings_calendar import EARNINGS_WARNING_TRADING_DAYS
three.TARGET_BY_SOURCE = {"A_PLUS": 3.0}
three.SECTOR_LIMIT = 1
PARK = 0.75
spy_ret = spy["Close"].pct_change()
periods = {"full": full, "first5": full[full < last - pd.DateOffset(years=5)], "last5": full[full >= last - pd.DateOffset(years=5)],
           "last2": full[full >= last - pd.DateOffset(years=2)]}
def since(t, d):
    x = earn.dates.get(t)
    if not x: return None
    day = pd.Timestamp(d).date(); i = bisect.bisect_right(x, day) - 1
    return None if i < 0 else int(np.busday_count(x[i], day))
def clean(r):
    u, s_ = earn.days_until(r.ticker, r.signal_date), since(r.ticker, r.signal_date)
    return not ((u is not None and u <= EARNINGS_WARNING_TRADING_DAYS) or (s_ is not None and s_ <= RECENT_EARNINGS_TRADING_DAYS))
pick = pull[[clean(r) for r in pull.itertuples()]].groupby("signal_date").head(1)
def parked(tr, eq):
    inv = pd.Series(0.0, index=eq.index)
    for t in tr.itertuples():
        inv.loc[t.entry_date:t.exit_date] += t.entry_price * t.shares
    idle = (1 - inv / eq).clip(lower=0)
    return three.STARTING_EQUITY * (1 + eq.pct_change().fillna(0) + idle.shift(1).fillna(1) * PARK * spy_ret.reindex(eq.index).fillna(0)).cumprod()
def st(eq):
    y = (eq.index[-1] - eq.index[0]).days / 365.25
    return (eq.iloc[-1] / eq.iloc[0] - 1) * 100, ((eq.iloc[-1] / eq.iloc[0]) ** (1 / y) - 1) * 100, (eq / eq.cummax() - 1).min() * -100
rows, starts = [], {}
tests = []
for label, ftr in (("top5", pull), ("plan pick", pick)):
    for total in (3, 4):
        tests.append((f"{total} open max, Bots 1+2, {label}", bp.ranked(a, ftr), total, None))
        tests.append((f"{total} open max, Bots 1+2 + Bot 3 slot, {label}", bp.ranked(a, ftr, mom), total - 1, 1))
for key, L, slots, ms in tests:
    three.MOMENTUM_SLOTS = ms
    tr, eq, ex = three.simulate(L, full, dfs, slots, earn)
    s = three.summarize(tr, eq, ex); ps = st(parked(tr, eq))
    r = {"setup": key, "trades": s["trades"], "ret": s["total_return_pct"], "dd": s["max_drawdown_pct"], "expR": s["expectancy_r"], "park_cagr": round(ps[1], 1), "park_dd": round(ps[2], 1)}
    for p, d in list(periods.items())[1:]:
        t2, e2, x2 = three.simulate(L, d, dfs, slots, earn)
        r[p] = three.summarize(t2, e2, x2)["total_return_pct"]
    t3, e3, x3 = three.simulate(L, full, dfs, slots, earn, three.HIGH_SLIPPAGE_PCT)
    r["slip25"] = three.summarize(t3, e3, x3)["total_return_pct"]
    v = [three.summarize(*three.simulate(L, full[k:], dfs, slots, earn)[:2], 0)["total_return_pct"] for k in range(0, 60, 3)]
    r["min20"], r["med20"], r["max20"] = min(v), float(pd.Series(v).median()), max(v)
    rows.append(r); starts[key] = pd.Series(v)
pd.set_option("display.width", 240, "display.max_columns", 30)
print("window", full[0].date(), full[-1].date()); print(pd.DataFrame(rows).to_string(index=False))
for label in ("top5", "plan pick"):
    for total in (3, 4):
        x = starts[f"{total} open max, Bots 1+2, {label}"]; y = starts[f"{total} open max, Bots 1+2 + Bot 3 slot, {label}"]
        print(total, label, "Bots 1+2 ahead in", int((x > y).sum()), "of 20 starts")
