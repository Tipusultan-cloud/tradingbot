"""Bot 2 clean plan pick (first stock with no earnings warning) against its top 5,
alone and with Bot 1. Bad-news and held-sector warnings are not modelled.
Reuses three_bots_prices.pkl from backtest_three_bots.py (run that first).
"""
import bisect, pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backtests")]
import backtest_three_bots as three
from slot_selector import RECENT_EARNINGS_TRADING_DAYS
from earnings_calendar import EARNINGS_WARNING_TRADING_DAYS
bp, hv = three.bp, three.hv
bp.PULLBACKS_PER_DAY = 20  # the live list keeps up to 20
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
pull = bp.build_pullback_candidates(panel, set(full[:-1]))
earn = three.load_earnings()
three.TARGET_BY_SOURCE = {"A_PLUS": 3.0}

def days_since(ticker, as_of):
    dates = earn.dates.get(ticker)
    if not dates:
        return None
    day = pd.Timestamp(as_of).date()
    i = bisect.bisect_right(dates, day) - 1
    return None if i < 0 else int(np.busday_count(dates[i], day))

def clean(row):
    until = earn.days_until(row.ticker, row.signal_date)
    since = days_since(row.ticker, row.signal_date)
    return not ((until is not None and until <= EARNINGS_WARNING_TRADING_DAYS)
                or (since is not None and since <= RECENT_EARNINGS_TRADING_DAYS))

top5 = pull.groupby("signal_date").head(5)
keep = [clean(r) for r in pull.itertuples()]
pick = pull[keep].groupby("signal_date").head(1)
print("days with a signal:", pull.signal_date.nunique(), "days with a clean plan pick:", pick.signal_date.nunique())
spy_ret = spy["Close"].pct_change()
periods = {"full": full, "first5": full[full < last - pd.DateOffset(years=5)],
           "last5": full[full >= last - pd.DateOffset(years=5)],
           "last2": full[full >= last - pd.DateOffset(years=2)]}

def parked(tr, eq, share=0.75):
    inv = pd.Series(0.0, index=eq.index)
    for t in tr.itertuples():
        inv.loc[t.entry_date:t.exit_date] += t.entry_price * t.shares
    idle = (1 - inv / eq).clip(lower=0)
    return three.STARTING_EQUITY * (1 + eq.pct_change().fillna(0) + idle.shift(1).fillna(1) * share * spy_ret.reindex(eq.index).fillna(0)).cumprod()

def stats(eq):
    y = (eq.index[-1] - eq.index[0]).days / 365.25
    return (eq.iloc[-1] / eq.iloc[0] - 1) * 100, ((eq.iloc[-1] / eq.iloc[0]) ** (1 / y) - 1) * 100, (eq / eq.cummax() - 1).min() * -100

rows = []
for name, look, slots, park in (
    ("Bot 2 alone, top 5", bp.ranked(top5), 1, False),
    ("Bot 2 alone, plan pick", bp.ranked(pick), 1, False),
    ("Bot 1 alone", bp.ranked(a), 1, False),
    ("Bot 1 alone, 2 slots", bp.ranked(a), 2, False),
    ("Bots 1+2, top 5", bp.ranked(a, top5), 2, False),
    ("Bots 1+2, plan pick", bp.ranked(a, pick), 2, False),
    ("Bot 1 alone, 2 slots + park 75%", bp.ranked(a), 2, True),
    ("Bots 1+2, top 5 + park 75%", bp.ranked(a, top5), 2, True),
    ("Bots 1+2, plan pick + park 75%", bp.ranked(a, pick), 2, True),
):
    r = {"variant": name}
    for p, d in periods.items():
        tr, eq, ex = three.simulate(look, d, dfs, slots, earn)
        if p == "full":
            b2 = tr[tr.source == "PULLBACK"]
            r.update(trades=len(tr), win=round((tr.pnl > 0).mean() * 100, 1), expR=round(tr.r_multiple.mean(), 3),
                     b2_trades=len(b2), b2_expR=round(b2.r_multiple.mean(), 3) if len(b2) else None)
        if park:
            eq = parked(tr, eq)
        ret, cagr, dd = stats(eq)
        r[p] = round(ret, 1)
        if p == "full":
            r.update(cagr=round(cagr, 1), dd=round(dd, 1))
    rows.append(r)
pd.set_option("display.width", 220, "display.max_columns", 30)
print(pd.DataFrame(rows).to_string(index=False))
res = {}
for name, look in (("top5", bp.ranked(a, top5)), ("pick", bp.ranked(a, pick)), ("bot1", bp.ranked(a))):
    res[name] = [stats(three.simulate(look, full[k:], dfs, 2, earn)[1])[0] for k in range(0, 60, 3)]
sh = pd.DataFrame(res)
print(sh.describe().loc[["min", "50%", "max"]].round(1).to_string())
print("pick beats top5:", int((sh.pick > sh.top5).sum()), "pick beats Bot 1 alone:", int((sh.pick > sh.bot1).sum()), "of", len(sh))
